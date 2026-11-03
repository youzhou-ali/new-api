import copy
import hashlib
import io
import json
from pathlib import Path
import signal
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

import prepare


def digest(value):
    return hashlib.sha256(value).hexdigest()


def snapshot():
    return {"schema_sha256": "a" * 64, "casbin_policy_sha256": "b" * 64,
            "tables": {name: {"count": 1, "sha": "c" * 32} for name in
                       ("users", "tokens", "channels", "logs", "casbin_rule", "system_instances")}}


class FakeDocker:
    """Exercise orchestration without a Docker socket, network or production DB."""

    def __init__(self, target, manifest, schema_change=False):
        self.target, self.manifest = target, manifest
        self.schema_change = schema_change
        self.commands, self.removed = [], []
        self.objects = {}
        self.images = {manifest["runtime_base_image"]: {"Id": "sha256:" + "5" * 64, "Os": "linux", "Architecture": "amd64"}}
        for index, field in enumerate(("app_container", "db_container", "redis_container", "nginx_container"), 1):
            name = target[field]
            row = {"Id": str(index) * 64, "Name": "/" + name, "Image": "sha256:" + str(index) * 64,
                   "State": {"Running": True}, "Config": {"Image": "production/" + name + ":old", "Cmd": [], "Env": []},
                   "Mounts": [], "NetworkSettings": {"Networks": {"production": {"Aliases": [name]}}}}
            if field == "app_container":
                row["Config"]["Env"] = ["SQL_DSN=postgresql://root:PRIVATE@postgres:5432/new-api?sslmode=disable", "SESSION_SECRET=PRIVATE"]
            if field == "db_container":
                row["Config"]["Env"] = ["POSTGRES_USER=root", "POSTGRES_DB=new-api"]
                row["Mounts"] = [{"Type": "volume", "Name": target["postgres_volume"], "Destination": "/var/lib/postgresql/data"}]
            self.objects[("container", name)] = row
            self.images[row["Config"]["Image"]] = {"Id": row["Image"], "Os": "linux", "Architecture": "amd64"}

    def run(self, args, *, check=True, stdin=None, stdout=None, **kwargs):
        self.commands.append(list(args))
        if args[0] != "docker":
            value = snapshot()
            if self.manifest["database_mode"] == "usage-tags-v1":
                upgraded = "before" not in args[-1]
                value["usage_tags_v1"] = {"valid": True, "present": upgraded,
                    "base_schema_sha256": "a" * 64, "base_logs": value["tables"]["logs"].copy(), "empty_values": True}
                if upgraded:
                    value["schema_sha256"] = "e" * 64
                    value["tables"]["logs"]["sha"] = "e" * 32
            if self.manifest["database_mode"] == "usage-tag-time-index-v1":
                upgraded = "before" not in args[-1]
                value["usage_tag_time_index_v1"] = {"valid": True, "present": upgraded,
                                                    "base_schema_sha256": "a" * 64}
                if upgraded:
                    value["schema_sha256"] = "e" * 64
            if self.schema_change and "after" in args[-1]:
                value["schema_sha256"] = "d" * 64
            Path(args[-1]).write_text(json.dumps(value))
            return b"snapshot captured"
        kind = args[1]
        if kind == "build":
            tag = args[args.index("--tag") + 1]
            self.images[tag] = {"Id": "sha256:" + "6" * 64, "Os": "linux", "Architecture": "amd64"}
            return b"built"
        if kind == "exec":
            if "pg_dump" in args or "pg_dumpall" in args:
                stdout.write(b"PRIVATE DATABASE BACKUP")
                return b""
            if "wget" in args:
                return json.dumps({"success": True, "data": {"version": self.manifest["version"]}}).encode()
            return b"1"
        operation = args[2]
        if kind == "image":
            if operation == "tag":
                self.images[args[4]] = {"Id": args[3]}
                return b""
            if args[3] in self.images:
                return json.dumps([self.images[args[3]]]).encode()
            if check:
                raise prepare.PrepareError("No such image")
            return None
        if operation == "ls":
            return "\n".join(name for object_kind, name in self.objects if object_kind == kind).encode()
        if operation == "inspect":
            values = [self.objects.get((kind, name)) for name in args[3:]]
            if all(value is not None for value in values):
                return json.dumps(values).encode()
            if check:
                raise prepare.PrepareError("No such resource")
            return None
        if operation == "create":
            name = args[args.index("--name") + 1] if kind == "container" else args[-1]
            label = args[args.index("--label") + 1]
            key, value = label.split("=", 1)
            if kind == "container":
                network = args[args.index("--network") + 1]
                mounts = []
                if "--mount" in args:
                    fields = dict(part.split("=", 1) for part in args[args.index("--mount") + 1].split(","))
                    mounts.append({"Type": fields["type"], "Name": fields["source"], "Destination": fields["target"]})
                row = {"Id": digest(name.encode()), "Name": "/" + name, "Config": {"Labels": {key: value}},
                       "NetworkSettings": {"Networks": {network: {}}}, "HostConfig": {}, "Mounts": mounts}
            else:
                row = {"Id": digest(name.encode()), "Name": name, "Labels": {key: value}, "Internal": "--internal" in args}
            self.objects[(kind, name)] = row
            return row.get("Id", name).encode()
        if operation == "rm":
            identity = args[-1]
            for key, value in list(self.objects.items()):
                if key[0] == kind and (value.get("Id") == identity or (kind == "volume" and value.get("Name") == identity)):
                    self.removed.append(key)
                    del self.objects[key]
                    return b"removed"
            raise prepare.PrepareError("Unknown removal")
        if operation in {"start", "restart", "stop"}:
            return b"started"
        raise AssertionError(args)


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.release = self.root / "release"
        self.release.mkdir()
        (self.root / "docker-compose.yml").write_text("services: {}\n")
        (self.root / "nginx.conf").write_text("server {}\n")
        (self.release / "db_snapshot.py").write_text("# mocked\n")
        self.target = {"root": str(self.root), "compose_file": str(self.root / "docker-compose.yml"),
                       "nginx_file": str(self.root / "nginx.conf"), "app_service": "new-api", "app_container": "new-api",
                       "db_container": "postgres", "redis_container": "redis", "nginx_container": "nginx",
                       "app_port": 3000, "postgres_volume": "production_pg_data", "public_url": "https://example.test"}
        self.manifest = {"format": 1, "release_id": "20261030T120000123456Z-" + "a" * 12,
                         "commit": "a" * 40, "tree": "b" * 40, "version": "v-test", "platform": "linux/amd64",
                         "database_mode": "unchanged", "runtime_base_image": "debian@sha256:" + "c" * 64,
                         "image_tag": "local/newapi:test", "binary_sha256": digest(b"binary"),
                         "frontend_assets": [{"path": "index.html", "sha256": "d" * 64}]}
        self.files = {name: b"license" for name in prepare.MEMBERS}
        self.files.update({"new-api": b"binary", "Dockerfile": ("FROM " + self.manifest["runtime_base_image"] +
                          '\nCOPY new-api /new-api\nENTRYPOINT ["/new-api"]\n').encode(), ".dockerignore": b""})
        self.archive()
        self.write_inputs()

    def write_inputs(self):
        (self.release / "release.json").write_text(json.dumps(self.manifest))
        (self.release / "target.json").write_text(json.dumps(self.target))

    def archive(self, extra=None, link=None, duplicate=False):
        path = self.release / "image-context.tgz"
        with tarfile.open(path, "w:gz") as archive:
            for name, data in self.files.items():
                member = tarfile.TarInfo(name)
                member.size = len(data)
                if name == link:
                    member.type, member.linkname, member.size = tarfile.SYMTYPE, "/production", 0
                archive.addfile(member, io.BytesIO(data))
            if extra:
                archive.addfile(tarfile.TarInfo(extra), io.BytesIO())
            if duplicate:
                archive.addfile(tarfile.TarInfo("LICENSE"), io.BytesIO())
        self.manifest.update(package_bytes=path.stat().st_size, package_sha256=digest(path.read_bytes()))

    def test_package_exact_files_and_binary_mode(self):
        target, manifest = prepare.load_inputs(self.release)
        self.assertEqual(target, self.target)
        output = self.release / "context"
        prepare.verify_package(self.release, manifest, output)
        self.assertEqual({path.name for path in output.iterdir()}, prepare.MEMBERS)
        self.assertEqual((output / "new-api").stat().st_mode & 0o777, 0o755)
        self.assertEqual((output / "LICENSE").stat().st_mode & 0o777, 0o600)

    def test_reject_unsafe_or_duplicate_archive_members(self):
        for options in ({"extra": "../escape"}, {"extra": "/absolute"}, {"extra": "unknown"},
                        {"duplicate": True}, {"link": "LICENSE"}):
            with self.subTest(options=options):
                self.archive(**options)
                with self.assertRaises(prepare.PrepareError):
                    prepare.verify_package(self.release, self.manifest)
        self.assertFalse((self.root / "escape").exists())

    def test_reject_wrong_hash_size_binary_or_runtime(self):
        for key, value in (("package_sha256", "0" * 64), ("package_bytes", 1),
                           ("binary_sha256", "0" * 64), ("runtime_base_image", "debian@sha256:" + "0" * 64)):
            with self.subTest(key=key), self.assertRaises(prepare.PrepareError):
                prepare.verify_package(self.release, {**self.manifest, key: value})
        self.files["Dockerfile"] += b"RUN echo forbidden\n"
        self.archive()
        with self.assertRaises(prepare.PrepareError):
            prepare.verify_package(self.release, self.manifest)
        for dockerfile in ("FROM " + self.manifest["runtime_base_image"] + "\nCOPY --from=remote/image /new-api /new-api\n",
                           "# syntax=remote/frontend\nFROM " + self.manifest["runtime_base_image"] + "\nCOPY new-api /new-api\n"):
            self.files["Dockerfile"] = dockerfile.encode()
            self.archive()
            with self.assertRaises(prepare.PrepareError):
                prepare.verify_package(self.release, self.manifest)

    def test_reject_database_change_before_docker(self):
        self.manifest["database_mode"] = "migrate"
        self.write_inputs()
        with self.assertRaises(prepare.PrepareError):
            prepare.load_inputs(self.release)

    def test_snapshot_schema_and_business_changes_block(self):
        original = snapshot()
        for changed in ({**original, "schema_sha256": "d" * 64}, copy.deepcopy(original)):
            if changed["schema_sha256"] == original["schema_sha256"]:
                changed["tables"]["users"]["count"] = 0
            with self.assertRaises(prepare.PrepareError):
                prepare.require_unchanged_snapshot(original, changed)
        changed = copy.deepcopy(original)
        changed["tables"]["system_instances"]["count"] = 2
        changed["tables"]["casbin_rule"]["sha"] = "d" * 32
        self.assertFalse(prepare.require_unchanged_snapshot(original, changed)["unexpected_tables"])

    def test_prepare_two_startups_and_only_owned_cleanup(self):
        fake = FakeDocker(self.target, self.manifest)
        with mock.patch.object(prepare, "Runner", return_value=fake):
            result = prepare.prepare(self.release, self.target, self.manifest)
        self.assertTrue(result["rehearsal_passed"])
        self.assertEqual(result["target_sha256"], digest((self.release / "target.json").read_bytes()))
        self.assertFalse(result["env_file_present"])
        self.assertEqual(set(fake.objects), {("container", name) for name in ("new-api", "postgres", "redis", "nginx")})
        self.assertEqual(len(fake.removed), 5)
        self.assertFalse((self.release / "rehearsal.env").exists())
        self.assertEqual(json.loads((self.release / "prepare-state.json").read_text())["status"], "success")
        commands = fake.commands
        self.assertEqual(sum("db_snapshot.py" in " ".join(cmd) for cmd in commands), 3)
        self.assertEqual(sum(cmd[:3] == ["docker", "container", "restart"] for cmd in commands), 1)
        build = next(cmd for cmd in commands if cmd[:2] == ["docker", "build"])
        self.assertIn("--network=none", build)
        self.assertIn("--pull=false", build)
        network = next(cmd for cmd in commands if cmd[:3] == ["docker", "network", "create"])
        self.assertIn("--internal", network)
        for cmd in commands:
            if cmd[:3] == ["docker", "container", "create"]:
                self.assertNotIn("--publish", cmd)
                self.assertNotIn("-p", cmd)
                self.assertNotIn("-v", cmd)
                self.assertNotIn(self.target["postgres_volume"], " ".join(cmd))
        restores = [cmd for cmd in commands if "pg_restore" in cmd and "--list" not in cmd]
        self.assertEqual(len(restores), 1)
        self.assertNotEqual(restores[0][3], self.target["db_container"])
        for cmd in commands:
            if cmd[:3] == ["docker", "container", "create"] and "POSTGRES_USER=root" in cmd:
                self.assertIn("sha256:" + "2" * 64, cmd)
            if cmd[:3] == ["docker", "container", "create"] and "redis-server" in cmd:
                self.assertIn("sha256:" + "3" * 64, cmd)
        backup = self.root / "backups" / self.manifest["release_id"]
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        self.assertEqual((backup / "containers.original.json").stat().st_mode & 0o777, 0o600)
        with mock.patch.object(prepare, "Runner", side_effect=AssertionError("must not run Docker")):
            with self.assertRaises(prepare.PrepareError):
                prepare.prepare(self.release, self.target, self.manifest)

    def test_schema_failure_preserves_evidence_and_cleans_resources(self):
        fake = FakeDocker(self.target, self.manifest, schema_change=True)
        with mock.patch.object(prepare, "Runner", return_value=fake), self.assertRaises(prepare.PrepareError):
            prepare.prepare(self.release, self.target, self.manifest)
        self.assertFalse((self.release / "prepare.json").exists())
        self.assertFalse((self.release / "rehearsal.env").exists())
        self.assertEqual(len(fake.removed), 5)
        state = json.loads((self.release / "prepare-state.json").read_text())
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["stage"], "isolated-rehearsal")
        self.assertEqual(len(state["resources"]), 5)
        self.assertTrue((self.root / "backups" / self.manifest["release_id"] / "production-initial.dump").exists())

    def assert_additive_rehearsal(self, mode):
        self.manifest["database_mode"] = mode
        self.write_inputs()
        fake = FakeDocker(self.target, self.manifest)
        with mock.patch.object(prepare, "Runner", return_value=fake):
            result = prepare.prepare(self.release, self.target, self.manifest)
        self.assertTrue(result["rollback_rehearsal_passed"])
        self.assertEqual(len(fake.removed), 6)
        commands = fake.commands
        rollback = next(cmd for cmd in commands if cmd[:3] == ["docker", "container", "create"]
                        and cmd[4].endswith("-rollback-app"))
        self.assertIn("sha256:" + "1" * 64, rollback)
        self.assertIn("--network", rollback)
        stop = next(i for i, cmd in enumerate(commands) if cmd[:3] == ["docker", "container", "stop"])
        start = next(i for i, cmd in enumerate(commands) if cmd[:3] == ["docker", "container", "start"]
                     and cmd[-1].endswith("-rollback-app"))
        self.assertLess(stop, start)
        self.assertEqual(sum("db_snapshot.py" in " ".join(cmd) for cmd in commands), 4)
        backup = self.root / "backups" / self.manifest["release_id"]
        comparisons = json.loads((backup / "rehearsal-comparison.json").read_text())
        self.assertTrue(comparisons[0]["additive_schema_verified"])
        self.assertFalse(comparisons[2]["schema_changed"])
        self.assertFalse(comparisons[2]["unexpected_tables"])

    def test_additive_rehearsal_verifies_old_image_on_upgraded_clone(self):
        self.assert_additive_rehearsal("usage-tags-v1")

    def test_index_rehearsal_verifies_old_image_on_upgraded_clone(self):
        self.assert_additive_rehearsal("usage-tag-time-index-v1")

    def test_failed_old_image_compatibility_cannot_authorize_cutover(self):
        self.manifest["database_mode"] = "usage-tags-v1"
        self.write_inputs()
        fake = FakeDocker(self.target, self.manifest)
        def health(runner, application, port, version):
            if application.endswith("-rollback-app"):
                raise prepare.PrepareError("old application incompatible")
        with mock.patch.object(prepare, "Runner", return_value=fake), \
                mock.patch.object(prepare, "wait_rehearsal_healthy", side_effect=health), \
                self.assertRaises(prepare.PrepareError):
            prepare.prepare(self.release, self.target, self.manifest)
        self.assertFalse((self.release / "prepare.json").exists())
        self.assertEqual(len(fake.removed), 6)
        self.assertEqual(json.loads((self.release / "prepare-state.json").read_text())["status"], "failed")

    def test_cleanup_refuses_changed_identity_or_label_and_production(self):
        for mutation in ("identity", "label"):
            with self.subTest(mutation=mutation):
                fake = FakeDocker(self.target, self.manifest)
                resources = prepare.RehearsalResources(fake, ["postgres", self.target["postgres_volume"]])
                name = "isolated-test"
                resources.create("network", name, ["docker", "network", "create", "--internal", "--label", prepare.OWNER_LABEL + "=" + resources.owner, name])
                row = fake.objects[("network", name)]
                if mutation == "identity":
                    row["Id"] = "replacement"
                else:
                    row["Labels"][prepare.OWNER_LABEL] = "another-owner"
                with self.assertRaises(prepare.PrepareError):
                    resources.cleanup()
                self.assertFalse(fake.removed)
                with self.assertRaises(prepare.PrepareError):
                    resources.create("volume", self.target["postgres_volume"], [])

    def test_resource_evidence_precedes_creation_and_records_identity(self):
        fake = FakeDocker(self.target, self.manifest)
        saved = []
        resources = prepare.RehearsalResources(fake, [], lambda records: saved.append(copy.deepcopy(records)))
        resources.create("network", "isolated", ["docker", "network", "create", "--internal", "--label", prepare.OWNER_LABEL + "=" + resources.owner, "isolated"])
        self.assertIsNone(saved[0][0]["id"])
        self.assertTrue(saved[1][0]["id"])
        resources.cleanup()

    def test_failure_output_stays_private_and_sigterm_is_catchable(self):
        log = io.BytesIO()
        runner = prepare.Runner(log)
        process = subprocess.CompletedProcess(["docker"], 1, b"PRIVATE STDOUT", b"PRIVATE SECRET")
        with mock.patch.object(prepare.subprocess, "run", return_value=process):
            with self.assertRaises(prepare.PrepareError) as caught:
                runner.run(["docker", "test"])
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertIn(b"PRIVATE SECRET", log.getvalue())
        previous = signal.getsignal(signal.SIGTERM)
        with self.assertRaises(prepare.PrepareError):
            with prepare.termination_guard():
                signal.raise_signal(signal.SIGTERM)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)


if __name__ == "__main__":
    unittest.main()
