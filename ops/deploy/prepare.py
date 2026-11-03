#!/usr/bin/env python3
"""Prepare an immutable application image and rehearse against an isolated DB copy.

This program runs on the deployment host. It never stops production services or
restores a production database. A failed release directory must be inspected,
not reused: prepare-state.json and the backup directory are durable evidence.
"""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import time
import traceback
from urllib.parse import quote, unquote, urlsplit


class PrepareError(Exception):
    pass


MEMBERS = {"Dockerfile", "new-api", ".dockerignore", "LICENSE", "NOTICE", "THIRD-PARTY-LICENSES.md"}
OWNER_LABEL = "io.new-api.prepare-owner"
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


def require(condition, message):
    if not condition:
        raise PrepareError(message)


def regular_file(path):
    require(stat.S_ISREG(path.lstat().st_mode), "Expected a regular file")
    require(path.resolve() == path, "Symlinked input paths are not supported")
    return path


def file_hash(path):
    h = hashlib.sha256()
    with regular_file(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_json(path, value, exclusive=False):
    data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    if exclusive:
        with path.open("xb") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(data)
        return
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        with temporary.open("xb") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_inputs(release_dir):
    require(release_dir.is_absolute() and release_dir.resolve() == release_dir and release_dir.is_dir(),
            "release-dir must be an absolute, existing directory without symlinks")
    manifest = json.loads(regular_file(release_dir / "release.json").read_text())
    target = json.loads(regular_file(release_dir / "target.json").read_text())
    require(isinstance(manifest, dict) and isinstance(target, dict), "Invalid release or target manifest")
    require(type(manifest.get("format")) is int and manifest["format"] == 1, "Unsupported release format")
    require(manifest.get("database_mode") in {"unchanged", "usage-tags-v1", "usage-tag-time-index-v1"}, "Unsupported database mode")
    require(manifest.get("platform") == "linux/amd64", "Only linux/amd64 releases are supported")
    require(isinstance(manifest.get("release_id"), str) and NAME.fullmatch(manifest["release_id"]), "Invalid release ID")
    for field in ("commit", "tree"):
        require(isinstance(manifest.get(field), str) and re.fullmatch(r"[0-9a-f]{40}", manifest[field]), "Invalid source identity")
    for field in ("package_sha256", "binary_sha256"):
        require(isinstance(manifest.get(field), str) and SHA256.fullmatch(manifest[field]), "Invalid release digest")
    require(type(manifest.get("package_bytes")) is int and manifest["package_bytes"] > 0, "Invalid package size")
    require(isinstance(manifest.get("version"), str) and manifest["version"] and not any(c in manifest["version"] for c in "\r\n\0"), "Invalid version")
    base = manifest.get("runtime_base_image", "")
    require(isinstance(base, str) and re.fullmatch(r"[a-z0-9][a-z0-9./_:\-]*@sha256:[0-9a-f]{64}", base), "Runtime base must be digest-pinned")
    tag = manifest.get("image_tag", "")
    require(isinstance(tag, str) and re.fullmatch(r"[a-z0-9][a-z0-9./_:\-]*:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag), "Invalid release image tag")
    require(isinstance(manifest.get("frontend_assets"), list) and manifest["frontend_assets"], "Missing frontend asset evidence")
    for field in ("root", "compose_file", "nginx_file"):
        require(isinstance(target.get(field), str), "Missing target path")
        path = Path(target[field])
        require(path.is_absolute() and path.resolve() == path, "Target paths must be absolute and not symlinked")
    root = Path(target["root"])
    require(root.is_dir() and root != Path("/"), "Invalid target root")
    require(Path(target["compose_file"]).is_relative_to(root), "Compose must belong to the target root")
    require(Path(target["nginx_file"]).is_relative_to(root), "Nginx configuration must belong to the target root")
    for field in ("app_service", "app_container", "db_container", "redis_container", "nginx_container", "postgres_volume"):
        require(isinstance(target.get(field), str) and NAME.fullmatch(target[field]), "Invalid target resource name")
    containers = [target[field] for field in ("app_container", "db_container", "redis_container", "nginx_container")]
    require(len(set(containers)) == 4, "Production containers must be distinct")
    require(type(target.get("app_port")) is int and 1 <= target["app_port"] <= 65535, "Invalid application port")
    public = urlsplit(target.get("public_url", ""))
    require(public.scheme == "https" and public.hostname and not public.username and not public.password, "Invalid public URL")
    regular_file(Path(target["compose_file"]))
    regular_file(Path(target["nginx_file"]))
    regular_file(release_dir / "db_snapshot.py")
    return target, manifest


def verify_package(release_dir, manifest, destination=None):
    package = regular_file(release_dir / "image-context.tgz")
    require(package.stat().st_size == manifest["package_bytes"], "Package size mismatch")
    require(file_hash(package) == manifest["package_sha256"], "Package digest mismatch")
    with tarfile.open(package, "r:gz") as archive:
        members = archive.getmembers()
        require(len(members) == len(MEMBERS) and {m.name for m in members} == MEMBERS,
                "Archive members do not match the exact release allowlist")
        require(all(m.isfile() and not m.issparse() for m in members), "Archive contains a non-regular or sparse member")
        dockerfile = archive.extractfile("Dockerfile").read().decode("utf-8")
        require(not re.search(r"^\s*#\s*(?:syntax|escape)\s*=", dockerfile, re.IGNORECASE | re.MULTILINE),
                "Dockerfile parser directives are not supported")
        lines = dockerfile.replace("\\\n", " ").splitlines()
        instructions = [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]
        from_lines = [line for line in instructions if line.split()[0].upper() == "FROM"]
        require(from_lines == ["FROM " + manifest["runtime_base_image"]], "Dockerfile must use exactly the pinned runtime base")
        require(all(line.split()[0].upper() in {"FROM", "LABEL", "COPY", "EXPOSE", "WORKDIR", "ENTRYPOINT"}
                    for line in instructions), "Dockerfile contains an unsupported instruction")
        copies = [line for line in instructions if line.split()[0].upper() == "COPY"]
        require("COPY new-api /new-api" in copies and all(line in {
                    "COPY new-api /new-api", "COPY LICENSE NOTICE THIRD-PARTY-LICENSES.md /licenses/"
                } for line in copies), "Dockerfile may only copy the verified local application and licenses")
        binary_hash = hashlib.sha256()
        with archive.extractfile("new-api") as binary:
            for block in iter(lambda: binary.read(1024 * 1024), b""):
                binary_hash.update(block)
        require(binary_hash.hexdigest() == manifest["binary_sha256"], "Binary digest mismatch")
        if destination is not None:
            destination.mkdir(mode=0o700)
            # Never let tarfile apply archive paths, owners, modes or links.
            for member in members:
                with archive.extractfile(member) as source, (destination / member.name).open("xb") as output:
                    os.fchmod(output.fileno(), 0o755 if member.name == "new-api" else 0o600)
                    shutil.copyfileobj(source, output)


class Runner:
    def __init__(self, log):
        self.log = log

    def run(self, args, *, check=True, stdin=None, stdout=None, timeout=180, cwd=None):
        # Output (including inspect, database errors and application logs) stays
        # in private evidence. CalledProcessError must not expose its contents.
        self.log.write(("\nCOMMAND " + json.dumps(args) + "\n").encode())
        self.log.flush()
        result = subprocess.run(args, stdin=stdin, stdout=stdout or subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout, cwd=cwd)
        if stdout is None and result.stdout:
            self.log.write(result.stdout)
        if result.stderr:
            self.log.write(result.stderr)
        self.log.flush()
        if result.returncode:
            if check:
                raise PrepareError("Command failed; see the private preparation log")
            return None
        return result.stdout if stdout is None else b""


class RehearsalResources:
    """Only remove uniquely labelled resources created by this invocation."""

    def __init__(self, runner, protected_names, on_change=None):
        self.runner = runner
        self.owner = secrets.token_hex(16)
        self.protected_names = set(protected_names)
        self.records = []
        self.on_change = on_change or (lambda records: None)

    def inspect(self, kind, name):
        raw = self.runner.run(["docker", kind, "inspect", name], check=False)
        if raw is None:
            # An inspect failure is not proof of absence (the daemon may be
            # unavailable). Confirm through a successful listing before skip.
            listing = ["docker", kind, "ls"] + (["--all"] if kind == "container" else [])
            listing += ["--format", "{{.Names}}" if kind == "container" else "{{.Name}}"]
            names = self.runner.run(listing).decode().splitlines()
            require(name not in names, "An existing resource could not be inspected")
            return None
        values = json.loads(raw)
        require(isinstance(values, list) and len(values) == 1, "Invalid Docker inspection result")
        return values[0]

    def create(self, kind, name, args):
        require(name not in self.protected_names, "Rehearsal resource collides with production")
        require(self.inspect(kind, name) is None, "Rehearsal resource already exists")
        # Record the candidate before creation: a timed-out Docker client can
        # leave an object behind. Its unguessable label still binds ownership.
        record = {"kind": kind, "name": name, "id": None}
        self.records.append(record)
        self.on_change(self.records)
        self.runner.run(args)
        value = self.inspect(kind, name)
        require(value is not None, "New rehearsal resource is missing")
        labels = value.get("Config", {}).get("Labels", {}) if kind == "container" else value.get("Labels", {})
        require((labels or {}).get(OWNER_LABEL) == self.owner, "Rehearsal ownership label mismatch")
        record["id"] = value.get("Id") if kind != "volume" else value.get("Name")
        require(record["id"], "New rehearsal resource has no identity")
        self.on_change(self.records)
        return value

    def cleanup(self):
        failures = []
        for record in reversed(self.records):
            try:
                kind, name = record["kind"], record["name"]
                require(name not in self.protected_names, "Refusing production cleanup")
                value = self.inspect(kind, name)
                if value is None:
                    continue
                labels = value.get("Config", {}).get("Labels", {}) if kind == "container" else value.get("Labels", {})
                require((labels or {}).get(OWNER_LABEL) == self.owner, "Refusing cleanup of an unowned resource")
                identity = value.get("Name") if kind == "volume" else value.get("Id")
                require(identity and (record["id"] is None or identity == record["id"]), "Resource identity changed")
                args = ["docker", kind, "rm"] + (["-f"] if kind == "container" else []) + [identity]
                self.runner.run(args)
            except Exception:
                failures.append(record["name"])
        require(not failures, "Rehearsal cleanup incomplete; inspect recorded resources")


def snapshot_database(runner, release_dir, container, output):
    from cutover import load_snapshot

    runner.run([sys.executable, str(release_dir / "db_snapshot.py"), container, str(output)])
    return load_snapshot(output)


def require_unchanged_snapshot(before, after, database_mode="unchanged"):
    from cutover import compare_snapshots

    comparison = compare_snapshots(before, after, database_mode)
    require(not comparison["schema_changed"], "Rehearsal changed the database schema")
    require(not comparison["unexpected_tables"], "Rehearsal changed business data")
    return comparison


def container_environment(container):
    values = container.get("Config", {}).get("Env", [])
    return dict(value.split("=", 1) for value in values if "=" in value)


def production_containers(runner, target):
    names = [target[field] for field in ("app_container", "db_container", "redis_container", "nginx_container")]
    rows = json.loads(runner.run(["docker", "container", "inspect", *names]))
    by_name = {row.get("Name", "").removeprefix("/"): row for row in rows}
    require(set(by_name) == set(names), "Production inspection does not match the target")
    for row in rows:
        require(row.get("State", {}).get("Running") is True, "A production container is not running")
        require(IMAGE_ID.fullmatch(row.get("Image", "")), "Invalid production image identity")
    return by_name


def wait_rehearsal_healthy(runner, application, port, version):
    deadline = time.monotonic() + 90
    while True:
        raw = runner.run(["docker", "exec", application, "wget", "-qO-", "http://127.0.0.1:" + str(port) + "/api/status"],
                         check=False, timeout=5)
        try:
            status = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            status = {}
        if isinstance(status, dict) and status.get("success") is True and status.get("data", {}).get("version") == version:
            return
        require(time.monotonic() < deadline, "Rehearsal application did not become ready with the expected version")
        time.sleep(1)


def rehearse(runner, resources, target, manifest, original, release_dir, backup, new_image_id):
    app = original[target["app_container"]]
    database = original[target["db_container"]]
    redis = original[target["redis_container"]]
    db_env = container_environment(database)
    user = db_env.get("POSTGRES_USER", "postgres")
    db_name = db_env.get("POSTGRES_DB", user)
    require(user and db_name and not any(c in user + db_name for c in "\r\n\0"), "Invalid PostgreSQL identity")
    env = container_environment(app)
    require(not env.get("LOG_SQL_DSN"), "Separate log databases require a separate deployment plan")
    dsn = urlsplit(env.get("SQL_DSN", ""))
    aliases = {target["db_container"]}
    for network in database.get("NetworkSettings", {}).get("Networks", {}).values():
        aliases.update(network.get("Aliases") or [])
    require(dsn.scheme in {"postgres", "postgresql"} and dsn.hostname in aliases and unquote(dsn.path.lstrip("/")) == db_name,
            "Application database is not the configured PostgreSQL target")
    require(not env.get("PORT") or env["PORT"] == str(target["app_port"]), "Application port differs from the target")
    env.update(SQL_DSN="postgresql://" + quote(user, safe="") + "@postgres:5432/" + quote(db_name, safe="") + "?sslmode=disable",
               REDIS_CONN_STRING="redis://redis:6379", PORT=str(target["app_port"]))
    env.pop("LOG_SQL_DSN", None)
    require(all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) and not any(c in value for c in "\r\n\0")
                for key, value in env.items()), "Application environment cannot be safely represented in an env file")
    credential_file = release_dir / "rehearsal.env"
    with credential_file.open("x") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write("".join(key + "=" + value + "\n" for key, value in sorted(env.items())))

    prefix = "newapi-prepare-" + resources.owner[:16]
    network, volume = prefix + "-net", prefix + "-pgdata"
    pg, cache, application = prefix + "-pg", prefix + "-redis", prefix + "-app"
    rollback_application = prefix + "-rollback-app"
    rollback_version = None
    if manifest["database_mode"] in {"usage-tags-v1", "usage-tag-time-index-v1"}:
        status = json.loads(runner.run(["docker", "exec", target["app_container"], "wget", "-qO-",
                                       "http://127.0.0.1:" + str(target["app_port"]) + "/api/status"]))
        rollback_version = status.get("data", {}).get("version")
        require(status.get("success") is True and isinstance(rollback_version, str) and rollback_version,
                "Could not identify running application version for rollback rehearsal")
    label = OWNER_LABEL + "=" + resources.owner
    net = resources.create("network", network, ["docker", "network", "create", "--internal", "--label", label, network])
    require(net.get("Internal") is True, "Rehearsal network is not internal")
    resources.create("volume", volume, ["docker", "volume", "create", "--label", label, volume])
    resources.create("container", pg, ["docker", "container", "create", "--name", pg, "--label", label,
                     "--network", network, "--network-alias", "postgres", "--memory=192m", "--memory-swap=192m",
                     "--env", "POSTGRES_HOST_AUTH_METHOD=trust", "--env", "POSTGRES_USER=" + user,
                     "--env", "POSTGRES_DB=" + db_name, "--env", "PGDATA=/var/lib/postgresql/data/pgdata",
                     "--mount", "type=volume,source=" + volume + ",target=/var/lib/postgresql/data", database["Image"]])
    resources.create("container", cache, ["docker", "container", "create", "--name", cache, "--label", label,
                     "--network", network, "--network-alias", "redis", "--memory=48m", "--memory-swap=48m",
                     "--tmpfs", "/data:rw,size=32m", redis["Image"], "redis-server", "--save", "", "--appendonly", "no"])
    application_names = [(application, new_image_id)]
    if rollback_version is not None:
        application_names.append((rollback_application, app["Image"]))
    for name, image_id in application_names:
        resources.create("container", name, ["docker", "container", "create", "--name", name, "--label", label,
                     "--network", network, "--memory=256m", "--memory-swap=256m", "--cpus=1",
                     "--env-file", str(credential_file), "--tmpfs", "/data:rw,size=32m", "--tmpfs", "/app/logs:rw,size=32m",
                     image_id, *(app.get("Config", {}).get("Cmd") or [])])
    for name in (pg, cache, *(name for name, _ in application_names)):
        row = resources.inspect("container", name)
        require(set(row.get("NetworkSettings", {}).get("Networks", {})) == {network}, "Rehearsal joined an unexpected network")
        host = row.get("HostConfig", {})
        require(not host.get("PortBindings") and not host.get("PublishAllPorts") and not host.get("Binds")
                and not host.get("Privileged") and not host.get("VolumesFrom"), "Rehearsal is not isolated")
        for mount in row.get("Mounts", []):
            require(mount["Type"] == "tmpfs" or (mount["Type"] == "volume" and mount.get("Name") == volume and name == pg),
                    "Rehearsal has an unexpected persistent mount")
    runner.run(["docker", "container", "start", pg, cache])
    deadline = time.monotonic() + 90
    while True:
        ready = runner.run(["docker", "exec", pg, "psql", "-X", "-w", "-v", "ON_ERROR_STOP=1", "-U", user,
                            "-d", db_name, "-Atc", "SELECT 1"], check=False, timeout=5)
        if ready is not None:
            break
        require(time.monotonic() < deadline, "Clone PostgreSQL did not become ready")
        time.sleep(1)
    with (backup / "production-initial.dump").open("rb") as source:
        runner.run(["docker", "exec", "-i", pg, "pg_restore", "--exit-on-error", "--no-owner", "--no-acl",
                    "-U", user, "-d", db_name], stdin=source, timeout=300)
    before = snapshot_database(runner, release_dir, pg, backup / "rehearsal-before.json")
    runner.run(["docker", "container", "start", application])
    comparisons = []
    for startup in (1, 2):
        wait_rehearsal_healthy(runner, application, target["app_port"], manifest["version"])
        after = snapshot_database(runner, release_dir, pg, backup / ("rehearsal-after-" + str(startup) + ".json"))
        comparisons.append(require_unchanged_snapshot(before, after, manifest["database_mode"]))
        if startup == 1:
            runner.run(["docker", "container", "restart", "--time", "150", application], timeout=180)
    if rollback_version is not None:
        runner.run(["docker", "container", "stop", "--time", "150", application], timeout=180)
        runner.run(["docker", "container", "start", rollback_application])
        wait_rehearsal_healthy(runner, rollback_application, target["app_port"], rollback_version)
        rollback_after = snapshot_database(runner, release_dir, pg, backup / "rehearsal-rollback.json")
        comparisons.append(require_unchanged_snapshot(after, rollback_after))
    write_json(backup / "rehearsal-comparison.json", comparisons, exclusive=True)


@contextlib.contextmanager
def target_lock(root):
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(root / ".newapi-deploy.lock", flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PrepareError("Another deployment operation holds the target lock") from None
        yield
    finally:
        os.close(fd)


@contextlib.contextmanager
def termination_guard():
    previous = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGINT)}

    def interrupted(number, frame):
        # Let finally remove owned resources even if another signal arrives.
        for item in previous:
            signal.signal(item, signal.SIG_IGN)
        raise PrepareError("Preparation interrupted")

    try:
        for number in previous:
            signal.signal(number, interrupted)
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def prepare(release_dir, target, manifest):
    root = Path(target["root"])
    backup = root / "backups" / manifest["release_id"]
    with target_lock(root), termination_guard():
        for path in (backup, release_dir / "prepare.json", release_dir / "prepare-state.json",
                     release_dir / "prepare.log", release_dir / "image-context", release_dir / "rehearsal.env"):
            require(not path.exists() and not path.is_symlink(), "Preparation evidence already exists; inspect it instead of rerunning")
        os.chmod(release_dir, 0o700)
        backup.parent.mkdir(mode=0o700, exist_ok=True)
        require(backup.parent.resolve() == backup.parent, "Backup parent must not be symlinked")
        backup.mkdir(mode=0o700)
        state_path = release_dir / "prepare-state.json"
        state = {"release_id": manifest["release_id"], "status": "running", "stage": "initializing"}
        write_json(state_path, state, exclusive=True)
        with (release_dir / "prepare.log").open("xb") as log:
            os.fchmod(log.fileno(), 0o600)
            runner = Runner(log)
            protected = [target[field] for field in ("app_container", "db_container", "redis_container", "nginx_container", "postgres_volume")]
            def record_resources(records):
                state["resources"] = records
                write_json(state_path, state)

            resources = RehearsalResources(runner, protected, record_resources)
            result = None
            failure = None
            try:
                state["stage"] = "validate-and-build"
                write_json(state_path, state)
                manifest_hash = file_hash(release_dir / "release.json")
                target_hash = file_hash(release_dir / "target.json")
                require(json.loads((release_dir / "release.json").read_bytes()) == manifest
                        and json.loads((release_dir / "target.json").read_bytes()) == target,
                        "Release inputs changed after validation")
                original = production_containers(runner, target)
                db = original[target["db_container"]]
                production_volume = [m for m in db.get("Mounts", []) if m.get("Type") == "volume" and m.get("Name") == target["postgres_volume"]]
                require(len(production_volume) == 1, "Production PostgreSQL volume does not match the target")
                write_json(backup / "containers.original.json", list(original.values()), exclusive=True)
                for source, name in ((Path(target["compose_file"]), "docker-compose.yml.original"),
                                     (Path(target["nginx_file"]), "nginx.conf.original"), (root / ".env", ".env.original")):
                    if source.exists() or source.is_symlink():
                        regular_file(source)
                        with source.open("rb") as incoming, (backup / name).open("xb") as outgoing:
                            os.fchmod(outgoing.fileno(), 0o600)
                            shutil.copyfileobj(incoming, outgoing)
                config_hash = file_hash(backup / "docker-compose.yml.original")
                nginx_hash = file_hash(backup / "nginx.conf.original")
                old = original[target["app_container"]]
                rollback = "local/newapi-rollback:" + manifest["release_id"].lower()
                for tag in (rollback, manifest["image_tag"]):
                    require(runner.run(["docker", "image", "inspect", tag], check=False) is None, "A release or rollback image tag already exists")
                runner.run(["docker", "image", "tag", old["Image"], rollback])
                rollback_info = json.loads(runner.run(["docker", "image", "inspect", rollback]))[0]
                require(rollback_info["Id"] == old["Image"], "Rollback tag does not identify the running image")
                base = json.loads(runner.run(["docker", "image", "inspect", manifest["runtime_base_image"]]))[0]
                require(base.get("Os") == "linux" and base.get("Architecture") == "amd64", "Runtime base platform mismatch")
                verify_package(release_dir, manifest, release_dir / "image-context")
                runner.run(["docker", "build", "--pull=false", "--network=none", "--platform=linux/amd64",
                            "--label", "io.new-api.binary-sha256=" + manifest["binary_sha256"],
                            "--tag", manifest["image_tag"], "."], cwd=release_dir / "image-context", timeout=300)
                image = json.loads(runner.run(["docker", "image", "inspect", manifest["image_tag"]]))[0]
                require(IMAGE_ID.fullmatch(image.get("Id", "")) and image.get("Os") == "linux" and image.get("Architecture") == "amd64", "Built image identity or platform mismatch")
                state["stage"] = "initial-database-backup"
                write_json(state_path, state)
                db_env = container_environment(db)
                user = db_env.get("POSTGRES_USER", "postgres")
                db_name = db_env.get("POSTGRES_DB", user)
                with (backup / "production-initial.dump").open("xb") as dump:
                    os.fchmod(dump.fileno(), 0o600)
                    runner.run(["docker", "exec", target["db_container"], "pg_dump", "-w", "-U", user, "-d", db_name, "-Fc"], stdout=dump, timeout=300)
                require((backup / "production-initial.dump").stat().st_size > 0, "Initial database dump is empty")
                with (backup / "production-initial.dump").open("rb") as dump:
                    runner.run(["docker", "exec", "-i", target["db_container"], "pg_restore", "--list"], stdin=dump)
                with (backup / "postgres-globals.sql").open("xb") as globals_file:
                    os.fchmod(globals_file.fileno(), 0o600)
                    runner.run(["docker", "exec", target["db_container"], "pg_dumpall", "-w", "-U", user, "--globals-only"], stdout=globals_file)
                state["stage"] = "isolated-rehearsal"
                state["resource_owner"] = resources.owner
                write_json(state_path, state)
                rehearse(runner, resources, target, manifest, original, release_dir, backup, image["Id"])
                current = production_containers(runner, target)
                require(all(current[name]["Id"] == row["Id"] and current[name]["Image"] == row["Image"] for name, row in original.items()), "Production containers changed during preparation")
                require(file_hash(Path(target["compose_file"])) == config_hash and file_hash(Path(target["nginx_file"])) == nginx_hash,
                        "Production configuration changed during preparation")
                env_backup = backup / ".env.original"
                require((root / ".env").exists() == env_backup.exists(), "Production environment presence changed")
                if env_backup.exists():
                    require(file_hash(root / ".env") == file_hash(env_backup), "Production environment changed during preparation")
                require(file_hash(release_dir / "release.json") == manifest_hash, "Release manifest changed during preparation")
                require(file_hash(release_dir / "target.json") == target_hash, "Target configuration changed during preparation")
                result = {"old_image_id": old["Image"], "new_image_id": image["Id"], "rollback_image": rollback,
                          "old_image": old["Config"]["Image"], "config_sha256": config_hash, "nginx_sha256": nginx_hash,
                          "rehearsal_passed": True, "release_manifest_sha256": manifest_hash, "target_sha256": target_hash,
                          "rollback_rehearsal_passed": manifest["database_mode"] in {"usage-tags-v1", "usage-tag-time-index-v1"},
                          "env_file_present": env_backup.exists(), "env_sha256": file_hash(env_backup) if env_backup.exists() else None}
            except BaseException as error:
                failure = error
                log.write(traceback.format_exc().encode())
            finally:
                state["resources"] = resources.records
                try:
                    resources.cleanup()
                except BaseException as error:
                    failure = failure or error
                    log.write(traceback.format_exc().encode())
                (release_dir / "rehearsal.env").unlink(missing_ok=True)
                log.flush()
            if failure is not None:
                state.update(status="failed", error_type=type(failure).__name__)
                write_json(state_path, state)
                raise PrepareError("Preparation failed; inspect prepare-state.json and private prepare.log before any retry") from None
            write_json(release_dir / "prepare.json", result, exclusive=True)
            state.update(status="success", stage="prepared", rehearsal_passed=True)
            write_json(state_path, state)
            return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="Create backups, build locally and run an isolated rehearsal")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        target, manifest = load_inputs(args.release_dir)
        verify_package(args.release_dir, manifest)
        if not args.apply:
            print(json.dumps({"validated": True, "applied": False, "release_id": manifest["release_id"]}))
            return 0
        result = prepare(args.release_dir, target, manifest)
        print(json.dumps({"prepared": True, "release_id": manifest["release_id"], "new_image_id": result["new_image_id"]}))
        return 0
    except BaseException:
        # Never render raw Docker, database or environment exceptions publicly.
        print("Preparation failed. Inspect private preparation evidence; do not blindly rerun.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
