#!/usr/bin/env python3
"""Reviewed-target cutover; run ON THE SERVER as root with --apply.

No SSH is performed. Failure restores the old application and ingress only.
The database is never restored, dropped, or otherwise rewritten by rollback.
SIGKILL/host failure cannot be caught: consult the private state file before
manual recovery. This release script refuses a second mutating run.
"""

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
from urllib.parse import urlparse


ALLOWED_TABLE_CHANGES = {"system_instances", "system_task_locks", "system_tasks"}
SNAPSHOT_HELPER = Path(__file__).with_name("db_snapshot.py")



class CutoverError(Exception):
    pass


def read_document(path, label):
    try:
        if not path.is_file() or path.is_symlink():
            raise ValueError()
        raw = path.read_bytes()
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError()
        return value, raw
    except (OSError, ValueError, TypeError):
        raise CutoverError("invalid " + label) from None


def validate_documents(target, manifest, prepared, manifest_bytes, target_bytes):
    try:
        root = Path(target["root"])
        if not root.is_absolute() or root.resolve() != root:
            raise ValueError()
        for key in ("compose_file", "nginx_file"):
            path = Path(target[key])
            if not path.is_absolute() or path.resolve() != path or not path.is_relative_to(root):
                raise ValueError()
        for key in ("app_service", "app_container", "db_container", "redis_container",
                    "nginx_container", "postgres_volume"):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", target[key]):
                raise ValueError()
        if type(target["app_port"]) is not int or not 1 <= target["app_port"] <= 65535:
            raise ValueError()
        public_url = urlparse(target["public_url"])
        if public_url.scheme not in ("http", "https") or not public_url.hostname or public_url.username or public_url.password:
            raise ValueError()
        if (type(manifest["format"]) is not int or manifest["format"] != 1
                or manifest["database_mode"] not in {"unchanged", "usage-tags-v1", "usage-tag-time-index-v1"} or manifest["platform"] != "linux/amd64"):
            raise ValueError()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", manifest["release_id"]):
            raise ValueError()
        for key in ("commit", "tree"):
            if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", manifest[key]):
                raise ValueError()
        if not isinstance(manifest["version"], str) or not manifest["version"] or any(c.isspace() for c in manifest["version"]):
            raise ValueError()
        for value in (manifest["image_tag"], manifest["runtime_base_image"], prepared["old_image"], prepared["rollback_image"]):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,255}", value):
                raise ValueError()
        for value in (manifest["binary_sha256"], manifest["package_sha256"], prepared["config_sha256"],
                      prepared["nginx_sha256"], prepared["release_manifest_sha256"], prepared["target_sha256"]):
            if not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError()
        for key in ("old_image_id", "new_image_id"):
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", prepared[key]):
                raise ValueError()
        if prepared["old_image_id"] == prepared["new_image_id"] or prepared["rehearsal_passed"] is not True:
            raise ValueError()
        if manifest["database_mode"] in {"usage-tags-v1", "usage-tag-time-index-v1"} and prepared.get("rollback_rehearsal_passed") is not True:
            raise ValueError()
        if prepared["release_manifest_sha256"] != hashlib.sha256(manifest_bytes).hexdigest():
            raise ValueError()
        if prepared["target_sha256"] != hashlib.sha256(target_bytes).hexdigest():
            raise ValueError()
        if type(prepared["env_file_present"]) is not bool:
            raise ValueError()
        if prepared["env_file_present"]:
            if not re.fullmatch(r"[0-9a-f]{64}", prepared["env_sha256"]):
                raise ValueError()
        elif prepared["env_sha256"] is not None:
            raise ValueError()
        if not isinstance(manifest["frontend_assets"], list) or not manifest["frontend_assets"]:
            raise ValueError()
        for asset in manifest["frontend_assets"]:
            path = Path(asset["path"])
            if path.is_absolute() or ".." in path.parts or not asset["path"]:
                raise ValueError()
            if not re.fullmatch(r"[0-9a-f]{64}", asset["sha256"]):
                raise ValueError()
    except (KeyError, TypeError, ValueError, OSError):
        raise CutoverError("target, release manifest, or prepare result is invalid or unbound") from None


@contextmanager
def target_lock(root):
    # One lock per target, shared by every release and every deployment entry point.
    try:
        descriptor = os.open(root / ".newapi-deploy.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise CutoverError("cannot open target deployment lock") from None
    with os.fdopen(descriptor, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CutoverError("another deployment holds the target lock") from None
        yield


def emit(event, **safe_fields):
    # Never print command output, environment, inspect data, or configuration.
    print(json.dumps({"event": event, **safe_fields}, ensure_ascii=True), flush=True)


def command(argv, label, timeout=30, *, input_bytes=None, check=True, stdout_file=None):
    try:
        result = subprocess.run(
            argv, input=input_bytes, stdout=stdout_file or subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        raise CutoverError(label + ": timed out") from None
    except OSError:
        raise CutoverError(label + ": could not start") from None
    if check and result.returncode != 0:
        raise CutoverError(label + ": exit " + str(result.returncode))
    return result


def inspect_container(name):
    result = command(["docker", "inspect", name], "inspect container")
    try:
        objects = json.loads(result.stdout)
        if len(objects) != 1:
            raise ValueError()
        return objects[0]
    except (ValueError, TypeError, KeyError):
        raise CutoverError("invalid container inspection result") from None


def image_id(name):
    value = command(["docker", "image", "inspect", "--format", "{{.Id}}", name],
                    "inspect image").stdout.decode().strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise CutoverError("invalid image identity")
    return value


def app_runtime_identity(container):
    mounts = [{field: mount[field] for field in ("Source", "Destination", "Type", "RW")}
              for mount in container["Mounts"]]
    mounts.sort(key=lambda mount: (mount["Destination"], mount["Source"]))
    config = container["Config"]
    identity = {field: config.get(field) for field in
                ("Cmd", "Entrypoint", "User", "WorkingDir", "Healthcheck", "ExposedPorts", "StopSignal")}
    identity.update(Env=sorted(config["Env"]), Mounts=mounts)
    identity["HostConfig"] = {field: container["HostConfig"].get(field) for field in
                              ("PortBindings", "NetworkMode", "RestartPolicy", "Privileged",
                               "ReadonlyRootfs", "CapAdd", "CapDrop", "SecurityOpt", "LogConfig")}
    return identity


def postgres_volume_name(container):
    volumes = [mount.get("Name") for mount in container["Mounts"]
               if mount["Destination"] == "/var/lib/postgresql/data" and mount["Type"] == "volume"]
    if len(volumes) != 1 or not volumes[0]:
        raise CutoverError("expected one named PostgreSQL data volume")
    return volumes[0]


def save_archive_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    with path.with_name(path.name + ".sha256").open("x", encoding="ascii") as output:
        output.write(digest.hexdigest() + "  " + path.name + "\n")
        output.flush()
        os.fsync(output.fileno())
    return digest.hexdigest()


def write_existing(path, data):
    # Preserve the inode: nginx.conf is bind-mounted into a running container.
    with path.open("r+b") as output:
        output.seek(0)
        output.write(data)
        output.truncate()
        output.flush()
        os.fsync(output.fileno())


def replace_app_image(raw, service, expected, replacement):
    text = raw.decode("utf-8")
    lines = text.splitlines(keepends=True)
    starts = [index for index, line in enumerate(lines)
              if re.fullmatch(r"  " + re.escape(service) + r":[ \t]*(?:#[^\r\n]*)?\r?\n?", line)]
    if len(starts) != 1:
        raise CutoverError("expected one two-space-indented application service")
    start = starts[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if re.match(r"^(?:[^\s#]|  [^\s#])", lines[index]):
            end = index
            break
    matches = []
    pattern = re.compile(r"^(    image:[ \t]*)(['\"]?)([^'\"\s#]+)\2([ \t]*(?:#[^\r\n]*)?)(\r?\n?)$")
    for index in range(start + 1, end):
        match = pattern.fullmatch(lines[index])
        if match:
            matches.append((index, match))
    if len(matches) != 1 or matches[0][1].group(3) != expected:
        raise CutoverError("application image differs from prepared compose image")
    index, match = matches[0]
    lines[index] = (match.group(1) + match.group(2) + replacement + match.group(2)
                    + match.group(4) + match.group(5))
    return "".join(lines).encode("utf-8")


def nginx_reload(container):
    command(["docker", "exec", container, "nginx", "-t"], "nginx config test")
    command(["docker", "exec", container, "nginx", "-s", "reload"], "nginx reload")


def rule_command(rule, operation):
    return [rule["tool"], "-w", "5", operation, rule["chain"], *rule["args"]]


def rule_present(rule):
    result = command(rule_command(rule, "-C"), "check maintenance rule", check=False)
    if result.returncode not in (0, 1):
        raise CutoverError("could not determine maintenance rule state")
    return result.returncode == 0


def firewall_plan(port, comment):
    rules = []
    for tool in ("iptables", "ip6tables"):
        if shutil.which(tool) is None:
            raise CutoverError("required firewall tool is unavailable")
        all_rules = command([tool, "-w", "5", "-S"], "read firewall rules").stdout
        if comment.encode() in all_rules:
            raise CutoverError("maintenance comment already exists; manual review required")
        rules.append({"tool": tool, "chain": "INPUT", "args": [
            "-p", "tcp", "--dport", str(port), "-m", "conntrack", "--ctstate", "NEW",
            "-m", "comment", "--comment", comment, "-j", "REJECT", "--reject-with", "tcp-reset",
        ]})
        chain = command([tool, "-w", "5", "-S", "DOCKER-USER"],
                        "inspect Docker firewall chain", check=False)
        if chain.returncode != 0:
            if tool == "iptables":
                raise CutoverError("IPv4 DOCKER-USER chain is unavailable")
            emit("ipv6_docker_user_absent", input_rule_covers_userland_proxy=True)
            continue
        rules.append({"tool": tool, "chain": "DOCKER-USER", "args": [
            "-p", "tcp", "-m", "conntrack", "--ctstate", "NEW", "--ctorigdstport", str(port),
            "-m", "comment", "--comment", comment, "-j", "REJECT", "--reject-with", "tcp-reset",
        ]})
    return rules


def established_connections(pid, port):
    count = 0
    for name in ("tcp", "tcp6"):
        path = Path("/proc") / str(pid) / "net" / name
        if name == "tcp6" and not path.exists():
            continue
        try:
            rows = path.read_text().splitlines()[1:]
        except OSError:
            raise CutoverError("could not inspect application connections") from None
        for row in rows:
            fields = row.split()
            if len(fields) < 4:
                raise CutoverError("invalid application connection table")
            if fields[1].rsplit(":", 1)[-1].upper() == f"{port:04X}" and fields[3] == "01":
                count += 1
    return count


def load_snapshot(path):
    try:
        value = json.loads(path.read_bytes())
        if not re.fullmatch(r"[0-9a-f]{64}", value["schema_sha256"]):
            raise ValueError()
        if "casbin_policy_sha256" in value and not re.fullmatch(r"[0-9a-f]{64}", value["casbin_policy_sha256"]):
            raise ValueError()
        tables = value["tables"]
        if not isinstance(tables, dict) or not {"users", "tokens", "channels", "logs"} <= tables.keys():
            raise ValueError()
        for name, facts in tables.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError()
            if type(facts["count"]) is not int or facts["count"] < 0:
                raise ValueError()
            if not re.fullmatch(r"[0-9a-f]{32}", facts["sha"]):
                raise ValueError()
        usage = value.get("usage_tags_v1")
        if usage is not None:
            if type(usage["valid"]) is not bool or type(usage["empty_values"]) is not bool:
                raise ValueError()
            if usage["valid"] and (type(usage["present"]) is not bool
                    or not re.fullmatch(r"[0-9a-f]{64}", usage["base_schema_sha256"])):
                raise ValueError()
            if (type(usage["base_logs"]["count"]) is not int or usage["base_logs"]["count"] < 0
                    or not re.fullmatch(r"[0-9a-f]{32}", usage["base_logs"]["sha"])):
                raise ValueError()
        index = value.get("usage_tag_time_index_v1")
        if index is not None:
            if type(index["valid"]) is not bool:
                raise ValueError()
            if index["valid"] and (type(index["present"]) is not bool
                    or not re.fullmatch(r"[0-9a-f]{64}", index["base_schema_sha256"])):
                raise ValueError()
        return value
    except (OSError, ValueError, TypeError, KeyError):
        raise CutoverError("invalid database snapshot") from None


def compare_snapshots(before, after, database_mode="unchanged"):
    if database_mode not in {"unchanged", "usage-tags-v1", "usage-tag-time-index-v1"}:
        raise CutoverError("unsupported database comparison mode")
    if before["tables"].keys() != after["tables"].keys():
        raise CutoverError("database table set changed")
    changed = sorted(name for name in before["tables"]
                     if before["tables"][name] != after["tables"][name])
    permitted = set(ALLOWED_TABLE_CHANGES)
    casbin_equal = (
        bool(before.get("casbin_policy_sha256"))
        and before.get("casbin_policy_sha256") == after.get("casbin_policy_sha256")
        and before["tables"].get("casbin_rule", {}).get("count") is not None
        and before["tables"].get("casbin_rule", {}).get("count")
            == after["tables"].get("casbin_rule", {}).get("count")
    )
    if casbin_equal:
        permitted.add("casbin_rule")
    schema_changed = before["schema_sha256"] != after["schema_sha256"]
    additive_verified = False
    if database_mode == "usage-tags-v1":
        old, new = before.get("usage_tags_v1", {}), after.get("usage_tags_v1", {})
        if (old.get("valid") is not True or new.get("valid") is not True
                or new.get("present") is not True
                or old.get("base_schema_sha256") != new.get("base_schema_sha256")):
            raise CutoverError("database schema differs from usage-tags-v1 additions")
        if not old.get("present"):
            if new.get("empty_values") is not True or old.get("base_logs") != new.get("base_logs"):
                raise CutoverError("usage-tags-v1 changed existing log data")
            permitted.add("logs")
            additive_verified = True
            schema_changed = False
        # If already migrated, keep the full row and schema checks: values in
        # existing usage columns are business data and must not be ignored.
    elif database_mode == "usage-tag-time-index-v1":
        old, new = before.get("usage_tag_time_index_v1", {}), after.get("usage_tag_time_index_v1", {})
        if (old.get("valid") is not True or new.get("valid") is not True
                or new.get("present") is not True
                or old.get("base_schema_sha256") != new.get("base_schema_sha256")):
            raise CutoverError("database schema differs from usage-tag-time-index-v1 addition")
        if not old.get("present"):
            additive_verified = True
            schema_changed = False
        # An index cannot change any log field, including existing usage tags.
        # Full-row hashes remain authoritative in both first and repeated runs.
    return {"changed_tables": changed, "unexpected_tables": sorted(set(changed) - permitted),
            "schema_changed": schema_changed, "additive_schema_verified": additive_verified,
            "casbin_policy_equal": casbin_equal}


class Cutover:
    def __init__(self, release_dir):
        self.release_dir = Path(release_dir)
        if not self.release_dir.is_absolute() or not self.release_dir.is_dir():
            raise CutoverError("release directory must be an existing absolute directory")
        self.manifest, manifest_bytes = read_document(self.release_dir / "release.json", "release manifest")
        self.target, target_bytes = read_document(self.release_dir / "target.json", "target configuration")
        self.prepare, _ = read_document(self.release_dir / "prepare.json", "prepare result")
        validate_documents(self.target, self.manifest, self.prepare, manifest_bytes, target_bytes)
        self.root = Path(self.target["root"])
        self.backup = self.root / "backups" / self.manifest["release_id"]
        self.compose_file = Path(self.target["compose_file"])
        self.nginx_file = Path(self.target["nginx_file"])
        self.state_file = self.backup / "cutover-state.json"
        self.comment = "newapi-" + self.manifest["release_id"] + "-cutover"
        self.state = {"release": self.manifest["release_id"], "phase": "preflight", "events": []}
        self.rules = []
        self.ingress_touched = False
        self.app_touched = False
        self.original_compose = b""
        self.original_nginx = b""
        self.compose_command = []

    def save_state(self, phase, **safe_fields):
        self.state.update(phase=phase, **safe_fields)
        self.state["events"].append({"phase": phase, "time": int(time.time())})
        temporary = self.state_file.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as output:
            json.dump(self.state, output, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, self.state_file)
        emit(phase, **safe_fields)

    def preflight(self):
        if self.state_file.exists():
            raise CutoverError("release already has cutover state; inspect it before manual recovery")
        if not self.backup.is_dir() or self.backup.is_symlink():
            raise CutoverError("prepared backup directory is missing or is a symlink")
        initial_dump = self.backup / "production-initial.dump"
        if not initial_dump.is_file() or initial_dump.is_symlink() or initial_dump.stat().st_size == 0:
            raise CutoverError("prepared initial database backup is missing or empty")
        for path in (self.compose_file, self.nginx_file, SNAPSHOT_HELPER, self.backup / "docker-compose.yml.original",
                     self.backup / "nginx.conf.original", self.backup / "containers.original.json"):
            if not path.is_file() or path.is_symlink():
                raise CutoverError("required reviewed file is missing or is a symlink")
        # Compose expands .env at startup. Reject drift before stopping the old app.
        for path in (self.root / ".env", self.backup / ".env.original"):
            if path.is_symlink() or path.exists() != self.prepare["env_file_present"]:
                raise CutoverError("Compose environment file presence differs from prepare")
            if path.exists() and (not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != self.prepare["env_sha256"]):
                raise CutoverError("Compose environment file differs from prepared bytes")
        try:
            saved_containers = json.loads((self.backup / "containers.original.json").read_bytes())
            if not isinstance(saved_containers, list):
                raise ValueError()
            saved_containers = {item["Name"].lstrip("/"): item for item in saved_containers}
            for key in ("app_container", "db_container", "redis_container"):
                if self.target[key] not in saved_containers:
                    raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise CutoverError("invalid prepared container identities") from None
        self.original_compose = (self.backup / "docker-compose.yml.original").read_bytes()
        self.original_nginx = (self.backup / "nginx.conf.original").read_bytes()
        if self.compose_file.read_bytes() != self.original_compose or self.nginx_file.read_bytes() != self.original_nginx:
            raise CutoverError("live configuration differs from saved originals")
        if (hashlib.sha256(self.original_compose).hexdigest() != self.prepare["config_sha256"]
                or hashlib.sha256(self.original_nginx).hexdigest() != self.prepare["nginx_sha256"]):
            raise CutoverError("configuration differs from prepared hashes")
        self.new_compose = replace_app_image(self.original_compose, self.target["app_service"], self.prepare["old_image"], self.manifest["image_tag"])
        self.rollback_compose = replace_app_image(self.original_compose, self.target["app_service"], self.prepare["old_image"], self.prepare["rollback_image"])
        needle = f'proxy_pass http://{self.target["app_service"]}:{self.target["app_port"]};'.encode()
        if self.original_nginx.count(needle) != 1:
            raise CutoverError("expected exactly one reviewed nginx new-api proxy directive")
        self.maintenance_nginx = self.original_nginx.replace(needle, b"return 503;", 1)
        live = inspect_container(self.target["app_container"])
        if not live["State"]["Running"] or live["State"].get("Health", {}).get("Status") != "healthy":
            raise CutoverError("old application is not running and healthy")
        if live["HostConfig"]["NetworkMode"] == "host" or live["HostConfig"]["NetworkMode"].startswith("container:"):
            raise CutoverError("unexpected application network namespace")
        self.old_container_id = live["Id"]
        self.app_pid = live["State"]["Pid"]
        self.old_runtime_identity = app_runtime_identity(live)
        saved_app = saved_containers[self.target["app_container"]]
        if live["Id"] != saved_app["Id"] or self.old_runtime_identity != app_runtime_identity(saved_app):
            raise CutoverError("application identity or runtime changed after prepare")
        self.old_id, self.new_id = self.prepare["old_image_id"], self.prepare["new_image_id"]
        if (live["Image"] != self.old_id
                or image_id(self.prepare["old_image"]) != self.old_id
                or image_id(self.prepare["rollback_image"]) != self.old_id
                or image_id(self.manifest["image_tag"]) != self.new_id):
            raise CutoverError("application, prepared image, or rollback image identity does not match")
        env = dict(entry.split("=", 1) for entry in live["Config"]["Env"] if "=" in entry)
        if int(env.get("BATCH_UPDATE_INTERVAL", "5")) != 5:
            raise CutoverError("batch interval differs from reviewed five seconds")
        if int(env.get("SHUTDOWN_TIMEOUT_SECONDS", "120")) > 150:
            raise CutoverError("application shutdown timeout exceeds reviewed stop timeout")
        ports = live["NetworkSettings"]["Ports"].get(str(self.target["app_port"]) + "/tcp") or []
        if not ports or any(binding["HostPort"] != str(self.target["app_port"]) for binding in ports):
            raise CutoverError("application published port differs from reviewed port")
        project = live["Config"]["Labels"].get("com.docker.compose.project")
        if not project or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project):
            raise CutoverError("cannot identify current Compose project")
        self.compose_command = ["docker", "compose", "--project-directory", str(self.root),
                                "-p", project, "-f", str(self.compose_file)]
        selected = command([*self.compose_command, "ps", "-q", self.target["app_service"]],
                           "verify Compose application").stdout.decode().strip()
        if selected != self.old_container_id:
            raise CutoverError("Compose resolves a different application container")
        config = json.loads(command([*self.compose_command, "config", "--format", "json"],
                                    "validate Compose configuration").stdout)
        if config["services"][self.target["app_service"]]["image"] != self.prepare["old_image"]:
            raise CutoverError("resolved Compose image differs from reviewed image")
        database = inspect_container(self.target["db_container"])
        if not database["State"]["Running"]:
            raise CutoverError("database is not running")
        redis = inspect_container(self.target["redis_container"])
        if not redis["State"]["Running"]:
            raise CutoverError("Redis is not running")
        self.postgres_id, self.redis_id = database["Id"], redis["Id"]
        if (self.postgres_id != saved_containers[self.target["db_container"]]["Id"]
                or self.redis_id != saved_containers[self.target["redis_container"]]["Id"]):
            raise CutoverError("database or Redis container changed after prepare")
        self.postgres_volume = postgres_volume_name(database)
        if self.postgres_volume != self.target["postgres_volume"]:
            raise CutoverError("PostgreSQL data volume differs from prepared target")
        db_env = dict(entry.split("=", 1) for entry in database["Config"]["Env"] if "=" in entry)
        self.db_user, self.db_name = db_env.get("POSTGRES_USER"), db_env.get("POSTGRES_DB")
        if not self.db_user or not self.db_name:
            raise CutoverError("database identity differs from reviewed production database")
        nginx_reload_test = ["docker", "exec", self.target["nginx_container"], "nginx", "-t"]
        command(nginx_reload_test, "preflight nginx config test")
        for filename in ("production-final.dump", "production-final.dump.partial",
                         "production-final.dump.sha256", "application-final.tgz",
                         "application-final.tgz.partial", "application-final.tgz.sha256",
                         "application-runtime-before.json", "production-before.json", "production-after.json"):
            if (self.backup / filename).exists():
                raise CutoverError("final cutover artifact already exists; refusing overwrite")
        self.rules = firewall_plan(self.target["app_port"], self.comment)

    def remove_firewall(self):
        failed = False
        for rule in reversed(self.rules):
            try:
                if rule_present(rule):
                    command(rule_command(rule, "-D"), "remove maintenance rule")
                if rule_present(rule):
                    raise CutoverError("maintenance rule remains installed")
            except Exception:
                failed = True
                emit("firewall_cleanup_failed", family=rule["tool"], chain=rule["chain"])
        if failed:
            raise CutoverError("some exact maintenance firewall rules could not be removed")

    def wait_healthy(self, expected_image):
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            live = inspect_container(self.target["app_container"])
            if live["Image"] != expected_image:
                raise CutoverError("running application image identity differs from intended image")
            state = live["State"]
            if state.get("Running") and state.get("Health", {}).get("Status") == "healthy":
                return
            if state.get("Status") in ("dead", "exited"):
                raise CutoverError("application exited before becoming healthy")
            time.sleep(2)
        raise CutoverError("application did not become healthy within ninety seconds")

    def take_snapshot(self, filename):
        path = self.backup / filename
        command([sys.executable, str(SNAPSHOT_HELPER), self.target["db_container"], str(path)],
                "capture database snapshot", timeout=180)
        return load_snapshot(path)

    def run(self):
        self.preflight()
        self.save_state("preflight_passed", old_image_id=self.old_id, new_image_id=self.new_id)
        # This intentionally contains secrets and is private (umask 077). Never
        # emit it or attach its contents to an exception or progress message.
        with (self.backup / "application-runtime-before.json").open("x", encoding="utf-8") as output:
            json.dump({"application": self.old_runtime_identity,
                       "postgres_container_id": self.postgres_id,
                       "redis_container_id": self.redis_id,
                       "postgres_volume_name": self.postgres_volume}, output, indent=2)
            output.flush()
            os.fsync(output.fileno())
        # Persist all intended exact rules before the first insertion so timeout
        # or a signal after insertion still leaves enough information to undo it.
        self.state["firewall_rules"] = self.rules
        self.save_state("closing_ingress")
        for rule in self.rules:
            argv = [rule["tool"], "-w", "5", "-I", rule["chain"], "1", *rule["args"]]
            command(argv, "install maintenance rule")
            if not rule_present(rule):
                raise CutoverError("maintenance rule was not installed")
        self.ingress_touched = True
        write_existing(self.nginx_file, self.maintenance_nginx)
        nginx_reload(self.target["nginx_container"])
        self.save_state("ingress_closed")
        deadline = time.monotonic() + 180
        while established_connections(self.app_pid, self.target["app_port"]):
            if time.monotonic() >= deadline:
                raise CutoverError("application connections did not drain within 180 seconds")
            time.sleep(2)
        time.sleep(10)
        if established_connections(self.app_pid, self.target["app_port"]) != 0:
            raise CutoverError("application connections appeared during batch drain")
        if inspect_container(self.target["app_container"])["Id"] != self.old_container_id:
            raise CutoverError("application container changed during drain")
        self.save_state("drained")
        self.app_touched = True
        command([*self.compose_command, "stop", "-t", "150", self.target["app_service"]],
                "stop old application", timeout=180)
        if inspect_container(self.target["app_container"])["State"]["Running"]:
            raise CutoverError("old application remains running")
        self.save_state("old_application_stopped")
        partial = self.backup / "production-final.dump.partial"
        with partial.open("xb") as output:
            command(["docker", "exec", self.target["db_container"], "pg_dump", "-U", self.db_user,
                     "-d", self.db_name, "-Fc"], "final database backup", timeout=300,
                    stdout_file=output)
            output.flush()
            os.fsync(output.fileno())
        if partial.stat().st_size == 0:
            raise CutoverError("final database backup is empty")
        with partial.open("rb") as backup_input:
            # List the archive without restoring it; read via stdin, never print.
            result = subprocess.run(["docker", "exec", "-i", self.target["db_container"], "pg_restore", "--list"],
                                    stdin=backup_input, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=120, check=False)
        if result.returncode != 0:
            raise CutoverError("final database backup archive validation failed")
        os.replace(partial, self.backup / "production-final.dump")
        dump_hash = save_archive_hash(self.backup / "production-final.dump")
        application_partial = self.backup / "application-final.tgz.partial"
        command(["tar", "-czf", str(application_partial), "-C", str(self.root), "data", "logs"],
                "final application data and logs backup", timeout=300)
        with application_partial.open("rb") as archive:
            os.fsync(archive.fileno())
        command(["tar", "-tzf", str(application_partial)],
                "validate application backup archive", timeout=120)
        os.replace(application_partial, self.backup / "application-final.tgz")
        application_hash = save_archive_hash(self.backup / "application-final.tgz")
        before = self.take_snapshot("production-before.json")
        self.save_state("final_backup_verified", database_archive_sha256=dump_hash,
                        application_archive_sha256=application_hash)
        write_existing(self.compose_file, self.new_compose)
        command([*self.compose_command, "up", "-d", "--no-deps", "--pull", "never", self.target["app_service"]],
                "start new application", timeout=120)
        self.wait_healthy(self.new_id)
        current_app = inspect_container(self.target["app_container"])
        current_postgres, current_redis = inspect_container(self.target["db_container"]), inspect_container(self.target["redis_container"])
        if app_runtime_identity(current_app) != self.old_runtime_identity:
            raise CutoverError("application runtime configuration or mounts changed")
        if current_postgres["Id"] != self.postgres_id or current_redis["Id"] != self.redis_id:
            raise CutoverError("PostgreSQL or Redis container identity changed")
        if postgres_volume_name(current_postgres) != self.postgres_volume:
            raise CutoverError("PostgreSQL data volume identity changed")
        self.save_state("runtime_identity_verified", app_env_cmd_mounts_equal=True,
                        postgres_container_unchanged=True, redis_container_unchanged=True,
                        postgres_volume_unchanged=True)
        after = self.take_snapshot("production-after.json")
        comparison = compare_snapshots(before, after, self.manifest["database_mode"])
        self.save_state("database_compared", **comparison)
        if comparison["schema_changed"] or comparison["unexpected_tables"]:
            raise CutoverError("database comparison differs outside the explicit system-table allowance")
        write_existing(self.nginx_file, self.original_nginx)
        nginx_reload(self.target["nginx_container"])
        self.remove_firewall()
        self.save_state("success", changed_tables=comparison["changed_tables"])

    def rollback(self):
        errors = []
        # Keep ingress closed while replacing the application. Do not restore DB.
        # Cleanup may have removed some rules before failing; close those paths again.
        if self.app_touched:
            for rule in self.rules:
                try:
                    if not rule_present(rule):
                        command([rule["tool"], "-w", "5", "-I", rule["chain"], "1", *rule["args"]],
                                "restore maintenance rule")
                    if not rule_present(rule):
                        raise CutoverError("maintenance rule was not restored")
                except Exception:
                    errors.append("maintenance_firewall")
        if self.ingress_touched:
            try:
                write_existing(self.nginx_file, self.maintenance_nginx)
                nginx_reload(self.target["nginx_container"])
            except Exception:
                errors.append("maintenance_ingress")
        if errors:
            self.save_state("rollback_incomplete", failed_recovery_steps=sorted(set(errors)),
                            ingress_kept_closed=False, database_restored=False)
            return False
        if self.app_touched:
            try:
                command([*self.compose_command, "stop", "-t", "150", self.target["app_service"]],
                        "stop application before rollback", timeout=180)
                write_existing(self.compose_file, self.rollback_compose)
                command([*self.compose_command, "up", "-d", "--no-deps", "--pull", "never", self.target["app_service"]],
                        "restart rollback application", timeout=180)
                self.wait_healthy(self.old_id)
            except Exception:
                errors.append("old_application")
        else:
            try:
                self.wait_healthy(self.old_id)
            except Exception:
                errors.append("old_application")
        if "old_application" in errors:
            self.save_state("rollback_incomplete", failed_recovery_steps=errors,
                            ingress_kept_closed=True, database_restored=False)
            return False
        if self.ingress_touched:
            try:
                write_existing(self.nginx_file, self.original_nginx)
                nginx_reload(self.target["nginx_container"])
            except Exception:
                errors.append("restore_nginx")
        try:
            self.remove_firewall()
        except Exception:
            errors.append("remove_firewall")
        # Rollback intentionally pins the saved old image tag instead of latest.
        # All other Compose bytes come from the original saved configuration.
        self.save_state("rollback_incomplete" if errors else "rolled_back",
                        failed_recovery_steps=errors, database_restored=False)
        return not errors


def interrupted(_signum, _frame):
    raise CutoverError("interrupted; reverting application and ingress")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-dir", required=True, help="absolute prepared release directory on the server")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--apply", action="store_true", help="execute the authorized cutover")
    action.add_argument("--status", action="store_true", help="read the current phase without changing or recovering anything")
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        cutover = Cutover(args.release_dir)
        if args.status:
            if not cutover.state_file.exists():
                emit("status", started=False)
            else:
                state, _ = read_document(cutover.state_file, "cutover state")
                phase = state.get("phase")
                known = {"preflight_passed", "closing_ingress", "ingress_closed", "drained",
                         "old_application_stopped", "final_backup_verified", "runtime_identity_verified",
                         "database_compared", "success", "rollback_incomplete", "rolled_back"}
                if phase not in known:
                    raise CutoverError("unknown saved cutover phase; manual inspection required")
                emit("status", started=True, phase=phase)
            return 0
        if os.geteuid() != 0 or not cutover.root.is_dir():
            raise CutoverError("apply must run as root on the configured server")
        with target_lock(cutover.root):
            signal.signal(signal.SIGINT, interrupted)
            signal.signal(signal.SIGTERM, interrupted)
            try:
                cutover.run()
                return 0
            except BaseException as error:
                # Never expose inspect, environment, configuration, or raw subprocess output.
                message = str(error) if isinstance(error, CutoverError) else type(error).__name__
                emit("failed", reason=message)
                signal.signal(signal.SIGINT, signal.SIG_IGN)
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                if cutover.state["phase"] == "preflight":
                    return 2
                try:
                    return 1 if cutover.rollback() else 3
                except BaseException:
                    emit("rollback_incomplete", reason="manual recovery required", database_restored=False)
                    return 3
    except Exception as error:
        message = str(error) if isinstance(error, CutoverError) else type(error).__name__
        emit("failed", reason=message)
        return 2


if __name__ == "__main__":
    sys.exit(main())
