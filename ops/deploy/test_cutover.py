import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import cutover
import db_snapshot


OLD_ID = "sha256:" + "1" * 64
NEW_ID = "sha256:" + "2" * 64


class CutoverTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name).resolve()
        self.root = base / "app"
        self.root.mkdir()
        self.release = base / "release"
        self.release.mkdir()
        self.backup = self.root / "backups" / "release-1"
        self.backup.mkdir(parents=True)
        self.compose = b"services:\n  new-api:\n    image: local/old:1\n  redis:\n    image: redis:7\n"
        self.nginx = b"location / { proxy_pass http://new-api:3000; }\n"
        (self.root / "compose.yml").write_bytes(self.compose)
        (self.root / "nginx.conf").write_bytes(self.nginx)
        (self.backup / "docker-compose.yml.original").write_bytes(self.compose)
        (self.backup / "nginx.conf.original").write_bytes(self.nginx)
        (self.backup / "production-initial.dump").write_bytes(b"prepared fixture")
        self.target = {
            "root": str(self.root), "compose_file": str(self.root / "compose.yml"),
            "nginx_file": str(self.root / "nginx.conf"), "app_service": "new-api",
            "app_container": "new-api", "db_container": "postgres", "redis_container": "redis",
            "nginx_container": "ingress", "app_port": 3000, "postgres_volume": "pg-data",
            "public_url": "https://example.test",
        }
        self.manifest = {
            "format": 1, "release_id": "release-1", "commit": "a" * 40, "tree": "b" * 40,
            "version": "v-test", "platform": "linux/amd64", "database_mode": "unchanged",
            "image_tag": "local/new:1", "binary_sha256": "c" * 64, "package_sha256": "d" * 64,
            "runtime_base_image": "local/old:1", "frontend_assets": [{"path": "static/app.js", "sha256": "e" * 64}],
        }
        raw = json.dumps(self.manifest).encode()
        self.prepared = {
            "old_image_id": OLD_ID, "new_image_id": NEW_ID, "rollback_image": "local/rollback:1",
            "old_image": "local/old:1", "config_sha256": hashlib.sha256(self.compose).hexdigest(),
            "nginx_sha256": hashlib.sha256(self.nginx).hexdigest(), "rehearsal_passed": True,
            "release_manifest_sha256": hashlib.sha256(raw).hexdigest(),
            "target_sha256": hashlib.sha256(json.dumps(self.target).encode()).hexdigest(),
            "env_file_present": False, "env_sha256": None,
        }
        (self.release / "release.json").write_bytes(raw)
        (self.release / "target.json").write_text(json.dumps(self.target))
        self.write_prepare()
        self.app = {
            "Id": "app-id", "Name": "/new-api", "Image": OLD_ID,
            "State": {"Running": True, "Health": {"Status": "healthy"}, "Pid": 42},
            "Config": {"Env": ["BATCH_UPDATE_INTERVAL=5", "PRIVATE_VALUE=not-for-output"],
                       "Cmd": ["--log-dir", "/app/logs"], "Entrypoint": ["/new-api"],
                       "User": "", "WorkingDir": "/data", "Labels": {"com.docker.compose.project": "example"}},
            "HostConfig": {"NetworkMode": "example_default", "PortBindings": {"3000/tcp": [{"HostPort": "3000"}]}},
            "Mounts": [{"Source": str(self.root / "data"), "Destination": "/data", "Type": "bind", "RW": True}],
            "NetworkSettings": {"Ports": {"3000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "3000"}]}},
        }
        self.database = {"Id": "pg-id", "Name": "/postgres", "State": {"Running": True},
                         "Mounts": [{"Destination": "/var/lib/postgresql/data", "Type": "volume", "Name": "pg-data"}],
                         "Config": {"Env": ["POSTGRES_USER=test", "POSTGRES_DB=example"]}}
        self.redis = {"Id": "redis-id", "Name": "/redis", "State": {"Running": True}}
        (self.backup / "containers.original.json").write_text(json.dumps([self.app, self.database, self.redis]))
        self.images = {"local/old:1": OLD_ID, "local/rollback:1": OLD_ID, "local/new:1": NEW_ID}
        self.addCleanup(patch.stopall)
        patch.object(cutover, "emit").start()

    def write_prepare(self):
        (self.release / "prepare.json").write_text(json.dumps(self.prepared))

    def fake_command(self, argv, label, **kwargs):
        if label == "verify Compose application":
            output = b"app-id\n"
        elif label == "validate Compose configuration":
            output = json.dumps({"services": {"new-api": {"image": "local/old:1"}}}).encode()
        elif label == "preflight nginx config test":
            output = b""
        else:
            raise AssertionError("unexpected external command: " + label)
        return subprocess.CompletedProcess(argv, 0, output, b"")

    def prepared_cutover(self):
        instance = cutover.Cutover(self.release)
        containers = {"new-api": self.app, "postgres": self.database, "redis": self.redis}
        with patch.object(cutover, "inspect_container", side_effect=lambda name: containers[name]), \
                patch.object(cutover, "image_id", side_effect=lambda name: self.images[name]), \
                patch.object(cutover, "command", side_effect=self.fake_command), \
                patch.object(cutover, "firewall_plan", return_value=[]):
            instance.preflight()
        return instance

    def test_preflight_binds_images_and_only_replaces_application_image(self):
        instance = self.prepared_cutover()
        self.assertEqual(instance.new_compose, self.compose.replace(b"local/old:1", b"local/new:1"))
        self.assertEqual(instance.old_runtime_identity, cutover.app_runtime_identity(self.app))
        self.assertEqual(instance.postgres_id, "pg-id")
        self.assertEqual(instance.redis_id, "redis-id")
        self.assertEqual(instance.postgres_volume, "pg-data")

    def test_image_mismatch_is_rejected_before_mutation(self):
        for name in self.images:
            with self.subTest(image=name):
                original = self.images[name]
                self.images[name] = "sha256:" + "f" * 64
                with self.assertRaisesRegex(cutover.CutoverError, "image identity"):
                    self.prepared_cutover()
                self.images[name] = original
                self.assertEqual((self.root / "compose.yml").read_bytes(), self.compose)
                self.assertFalse((self.backup / "cutover-state.json").exists())
        self.app["Image"] = NEW_ID
        with self.assertRaisesRegex(cutover.CutoverError, "image identity"):
            self.prepared_cutover()

    def test_unbound_manifest_and_unrehearsed_release_are_rejected(self):
        for field, value in [("release_manifest_sha256", "0" * 64), ("target_sha256", "0" * 64),
                             ("rehearsal_passed", False), ("new_image_id", OLD_ID)]:
            with self.subTest(field=field):
                original = self.prepared[field]
                self.prepared[field] = value
                self.write_prepare()
                with self.assertRaises(cutover.CutoverError):
                    cutover.Cutover(self.release)
                self.prepared[field] = original
        self.write_prepare()

    def test_environment_file_drift_is_rejected_before_stopping_application(self):
        env = self.root / ".env"
        env.write_text("PRIVATE_TEST=changed")
        with self.assertRaisesRegex(cutover.CutoverError, "environment file presence"):
            self.prepared_cutover()
        original = b"PRIVATE_TEST=original"
        self.prepared.update(env_file_present=True, env_sha256=hashlib.sha256(original).hexdigest())
        self.write_prepare()
        (self.backup / ".env.original").write_bytes(original)
        with self.assertRaisesRegex(cutover.CutoverError, "environment file differs"):
            self.prepared_cutover()
        env.write_bytes(original)
        self.prepared_cutover()

    def test_prepared_container_replacement_is_rejected(self):
        for container in (self.app, self.database, self.redis):
            with self.subTest(container=container["Name"]):
                original = container["Id"]
                container["Id"] = "unexpected-replacement"
                with self.assertRaisesRegex(cutover.CutoverError, "after prepare"):
                    self.prepared_cutover()
                container["Id"] = original

    def test_runtime_identity_detects_configuration_drift_but_not_container_recreation(self):
        identity = cutover.app_runtime_identity(self.app)
        recreated = copy.deepcopy(self.app)
        recreated.update(Id="replacement-id", Image=NEW_ID)
        recreated["Config"]["Env"].reverse()
        self.assertEqual(identity, cutover.app_runtime_identity(recreated))
        changes = [("Config", "Env", ["DIFFERENT=1"]), ("Config", "Cmd", ["--other"]),
                   ("Config", "Entrypoint", ["/other"]), ("HostConfig", "NetworkMode", "host")]
        for section, field, value in changes:
            with self.subTest(field=field):
                changed = copy.deepcopy(self.app)
                changed[section][field] = value
                self.assertNotEqual(identity, cutover.app_runtime_identity(changed))
        changed = copy.deepcopy(self.app)
        changed["Mounts"][0]["Source"] = "/different"
        self.assertNotEqual(identity, cutover.app_runtime_identity(changed))

    def test_target_lock_blocks_different_release_and_releases_after_exit(self):
        second_release = self.root / "backups" / "release-2"
        second_release.mkdir()
        # The caller's batch directory has no effect on the shared target lock.
        with cutover.target_lock(self.root):
            with self.assertRaisesRegex(cutover.CutoverError, "target lock"):
                with cutover.target_lock(second_release.parent.parent):
                    self.fail("a second release acquired the target lock")
        with cutover.target_lock(self.root):
            pass

    def test_existing_state_is_diagnostic_only_and_refuses_reapply(self):
        state = self.backup / "cutover-state.json"
        state.write_text(json.dumps({"phase": "rollback_incomplete"}))
        with patch.object(cutover.Cutover, "rollback") as rollback, \
                patch.object(cutover, "command") as command:
            self.assertEqual(cutover.main(["--release-dir", str(self.release), "--status"]), 0)
            with self.assertRaisesRegex(cutover.CutoverError, "already has cutover state"):
                cutover.Cutover(self.release).preflight()
            rollback.assert_not_called()
            command.assert_not_called()

    def test_failed_cutover_rolls_back_only_application_and_keeps_ingress_closed_if_unhealthy(self):
        for healthy in (True, False):
            with self.subTest(old_app_healthy=healthy):
                instance = self.prepared_cutover()
                instance.app_touched = instance.ingress_touched = True
                (self.root / "nginx.conf").write_bytes(instance.maintenance_nginx)
                (self.root / "compose.yml").write_bytes(instance.new_compose)
                health_error = None if healthy else cutover.CutoverError("old app unhealthy")
                with patch.object(cutover, "command") as command, \
                        patch.object(cutover, "nginx_reload"), \
                        patch.object(instance, "wait_healthy", side_effect=health_error), \
                        patch.object(instance, "remove_firewall") as remove_firewall, \
                        patch.object(instance, "save_state") as save_state:
                    self.assertEqual(instance.rollback(), healthy)
                    calls = [call.args[0] for call in command.call_args_list]
                    self.assertEqual(len(calls), 2)
                    self.assertTrue(all(argv[:2] == ["docker", "compose"] for argv in calls))
                    self.assertTrue(all(argv[-1] == "new-api" for argv in calls))
                    self.assertTrue(all("pg_restore" not in argv and "down" not in argv and "rm" not in argv for argv in calls))
                    self.assertEqual(save_state.call_args.kwargs["database_restored"], False)
                    self.assertEqual(remove_firewall.call_count, int(healthy))
                    expected = self.nginx if healthy else instance.maintenance_nginx
                    self.assertEqual((self.root / "nginx.conf").read_bytes(), expected)
                # Reset fixture originals for the next preflight.
                (self.root / "compose.yml").write_bytes(self.compose)
                (self.root / "nginx.conf").write_bytes(self.nginx)

    def test_snapshot_comparison_does_not_hide_business_or_authorization_changes(self):
        tables = {name: {"count": 1, "sha": "1" * 32} for name in
                  ("users", "tokens", "channels", "logs", "casbin_rule", "system_instances")}
        before = {"tables": tables, "schema_sha256": "a" * 64, "casbin_policy_sha256": "b" * 64}
        after = copy.deepcopy(before)
        after["tables"]["system_instances"]["sha"] = "2" * 32
        after["tables"]["casbin_rule"]["sha"] = "3" * 32
        self.assertEqual(cutover.compare_snapshots(before, after)["unexpected_tables"], [])
        after["casbin_policy_sha256"] = "c" * 64
        after["tables"]["users"]["sha"] = "4" * 32
        self.assertEqual(cutover.compare_snapshots(before, after)["unexpected_tables"], ["casbin_rule", "users"])
        after["schema_sha256"] = "d" * 64
        self.assertTrue(cutover.compare_snapshots(before, after)["schema_changed"])

    def test_usage_tags_schema_accepts_only_exact_additions(self):
        original = "CREATE TABLE public.logs (\n    id bigint NOT NULL,\n    quota bigint\n);\n"
        columns = ("    usage_tag character varying(64) DEFAULT ''::character varying,\n"
                   "    usage_ref character varying(64) DEFAULT ''::character varying")
        index = "CREATE INDEX idx_logs_usage_tag ON public.logs USING btree (usage_tag);\n"
        upgraded = original.replace("    quota bigint", "    quota bigint,\n" + columns) + "\n" + index
        before, after = db_snapshot.usage_tags_schema(original), db_snapshot.usage_tags_schema(upgraded)
        self.assertTrue(before["valid"])
        self.assertFalse(before["present"])
        self.assertTrue(after["valid"])
        self.assertTrue(after["present"])
        self.assertEqual(before["base_schema_sha256"], after["base_schema_sha256"])
        for changed in (upgraded.replace("varying(64)", "varying(65)"),
                        upgraded.replace("DEFAULT ''", "DEFAULT 'x'"),
                        upgraded.replace("DEFAULT ''::character varying,", "DEFAULT ''::character varying NOT NULL,"),
                        upgraded.replace(index, ""),
                        upgraded.replace("USING btree (usage_tag)", "USING hash (usage_tag)"),
                        upgraded.replace("CREATE INDEX idx_", "CREATE UNIQUE INDEX idx_")):
            with self.subTest(changed=changed):
                self.assertFalse(db_snapshot.usage_tags_schema(changed)["valid"])
        for changed in (upgraded.replace("quota bigint", "quota integer"),
                        upgraded + "CREATE INDEX extra ON public.logs USING btree (id);\n",
                        upgraded.replace("    quota bigint,", "    quota bigint,\n    extra text,")):
            self.assertNotEqual(before["base_schema_sha256"], db_snapshot.usage_tags_schema(changed)["base_schema_sha256"])

    def test_usage_schema_does_not_hide_multiline_sql_literal_changes(self):
        original = "CREATE TABLE public.logs (\n    id bigint,\n    content text DEFAULT 'first\n\nsecond'::text\n);\n"
        addition = (",\n    usage_tag character varying(64) DEFAULT ''::character varying,\n"
                    "    usage_ref character varying(64) DEFAULT ''::character varying")
        index = "\nCREATE INDEX idx_logs_usage_tag ON public.logs USING btree (usage_tag);\n"
        upgraded = original.replace("::text\n);", "::text" + addition + "\n);") + index
        baseline = db_snapshot.usage_tags_schema(original)["base_schema_sha256"]
        self.assertEqual(baseline, db_snapshot.usage_tags_schema(upgraded)["base_schema_sha256"])
        self.assertNotEqual(baseline, db_snapshot.usage_tags_schema(upgraded.replace("first\n\nsecond", "first\nsecond"))["base_schema_sha256"])
        for literal in ("'first\n-- literal text\n\nsecond'", "E'first\\'part\n\nsecond'",
                        "$body$first\n-- function body\n\nsecond$body$", '"first\n\nsecond"'):
            dump = "-- dump header\n\nSELECT " + literal + ";\n\n"
            normalized = db_snapshot.normalize_dump(dump, compact=True)
            self.assertEqual(normalized, "SELECT " + literal + ";")
            self.assertNotEqual(normalized, db_snapshot.normalize_dump(dump.replace("\n\nsecond", "\nsecond"), compact=True))
        self.assertNotEqual(db_snapshot.normalize_dump("SELECT 'one\rsecond';", compact=True),
                            db_snapshot.normalize_dump("SELECT 'one\nsecond';", compact=True))

    def test_usage_tags_comparison_preserves_existing_data_and_default_mode(self):
        before = {"schema_sha256": "a" * 64,
                  "tables": {name: {"count": 1, "sha": "a" * 32} for name in ("users", "tokens", "channels", "logs")},
                  "usage_tags_v1": {"valid": True, "present": False, "base_schema_sha256": "a" * 64,
                                    "base_logs": {"count": 1, "sha": "a" * 32}, "empty_values": False}}
        after = copy.deepcopy(before)
        after["schema_sha256"] = "b" * 64
        after["tables"]["logs"]["sha"] = "b" * 32
        after["usage_tags_v1"].update(present=True, empty_values=True)
        default = cutover.compare_snapshots(before, after)
        self.assertTrue(default["schema_changed"])
        self.assertEqual(default["unexpected_tables"], ["logs"])
        accepted = cutover.compare_snapshots(before, after, "usage-tags-v1")
        self.assertFalse(accepted["schema_changed"])
        self.assertFalse(accepted["unexpected_tables"])
        for key, value in (("base_schema_sha256", "c" * 64), ("valid", False), ("present", False),
                           ("empty_values", False), ("base_logs", {"count": 1, "sha": "c" * 32})):
            changed = copy.deepcopy(after)
            changed["usage_tags_v1"][key] = value
            with self.subTest(key=key), self.assertRaises(cutover.CutoverError):
                cutover.compare_snapshots(before, changed, "usage-tags-v1")
        for table in before["tables"]:
            changed = copy.deepcopy(after)
            changed["tables"][table]["sha"] = "c" * 32
            # Once upgraded (including rollback rehearsal), even the new
            # column values are protected by the original full-row hash.
            comparison = cutover.compare_snapshots(after, changed, "usage-tags-v1")
            self.assertEqual(comparison["unexpected_tables"], [table])
        changed = copy.deepcopy(after)
        changed["tables"]["users"]["sha"] = "c" * 32
        self.assertEqual(cutover.compare_snapshots(before, changed, "usage-tags-v1")["unexpected_tables"], ["users"])

    def test_usage_tags_cutover_requires_successful_rollback_rehearsal(self):
        for mode in ("usage-tags-v1", "usage-tag-time-index-v1"):
            with self.subTest(mode=mode):
                self.manifest["database_mode"] = mode
                raw = json.dumps(self.manifest).encode()
                (self.release / "release.json").write_bytes(raw)
                self.prepared["release_manifest_sha256"] = hashlib.sha256(raw).hexdigest()
                self.prepared["rollback_rehearsal_passed"] = False
                self.write_prepare()
                with self.assertRaises(cutover.CutoverError):
                    cutover.Cutover(self.release)
                self.prepared["rollback_rehearsal_passed"] = True
                self.write_prepare()
                self.assertEqual(cutover.Cutover(self.release).manifest["database_mode"], mode)

    def test_usage_time_index_schema_accepts_only_exact_index_on_tagged_logs(self):
        original = ("CREATE TABLE public.logs (\n    id bigint NOT NULL,\n    created_at bigint,\n"
                    "    quota bigint,\n    usage_tag character varying(64) DEFAULT ''::character varying,\n"
                    "    usage_ref character varying(64) DEFAULT ''::character varying\n);\n"
                    "CREATE INDEX idx_logs_usage_tag ON public.logs USING btree (usage_tag);\n")
        index = "CREATE INDEX idx_logs_usage_tag_created_at ON public.logs USING btree (usage_tag, created_at);\n"
        before = db_snapshot.usage_tag_time_index_schema(original)
        after = db_snapshot.usage_tag_time_index_schema(original + index)
        self.assertTrue(before["valid"] and after["valid"])
        self.assertFalse(before["present"])
        self.assertTrue(after["present"])
        self.assertEqual(before["base_schema_sha256"], after["base_schema_sha256"])
        for changed in (index.replace("CREATE INDEX", "CREATE UNIQUE INDEX"),
                        index.replace("USING btree", "USING hash"),
                        index.replace("(usage_tag, created_at)", "(created_at, usage_tag)"),
                        index.replace(";", " WHERE usage_tag <> '';"),
                        index.replace(";", " INCLUDE (quota);"), index + index):
            with self.subTest(index=changed):
                self.assertFalse(db_snapshot.usage_tag_time_index_schema(original + changed)["valid"])
        self.assertFalse(db_snapshot.usage_tag_time_index_schema("CREATE TABLE public.logs (\n    id bigint\n);\n" + index)["valid"])
        for changed in (original.replace("quota bigint", "quota integer"),
                        original.replace("DEFAULT ''", "DEFAULT 'changed'"),
                        original + "CREATE INDEX unexpected ON public.logs USING btree (quota);\n"):
            result = db_snapshot.usage_tag_time_index_schema(changed + index)
            self.assertTrue(not result["valid"] or result["base_schema_sha256"] != before["base_schema_sha256"])
        # Matching DDL-looking text inside a default is data, not an allowed index.
        literal = original.replace("quota bigint", "quota bigint,\n    content text DEFAULT 'first\n\nsecond'::text")
        embedded = literal.replace("first\n\nsecond", "first\n" + index + "second")
        changed = db_snapshot.usage_tag_time_index_schema(embedded)
        self.assertFalse(changed["present"])
        self.assertNotEqual(db_snapshot.usage_tag_time_index_schema(literal)["base_schema_sha256"], changed["base_schema_sha256"])

    def test_usage_time_index_comparison_preserves_all_log_and_business_fields(self):
        before = {"schema_sha256": "a" * 64,
                  "tables": {name: {"count": 1, "sha": "a" * 32} for name in ("users", "tokens", "channels", "logs")},
                  "usage_tags_v1": {"valid": True, "present": True, "base_schema_sha256": "a" * 64,
                                    "base_logs": {"count": 1, "sha": "a" * 32}, "empty_values": False},
                  "usage_tag_time_index_v1": {"valid": True, "present": False, "base_schema_sha256": "d" * 64}}
        after = copy.deepcopy(before)
        after["schema_sha256"] = "b" * 64
        after["usage_tags_v1"]["base_schema_sha256"] = "c" * 64
        after["usage_tag_time_index_v1"]["present"] = True
        self.assertTrue(cutover.compare_snapshots(before, after)["schema_changed"])
        with self.assertRaises(cutover.CutoverError):
            cutover.compare_snapshots(before, after, "usage-tags-v1")
        accepted = cutover.compare_snapshots(before, after, "usage-tag-time-index-v1")
        self.assertTrue(accepted["additive_schema_verified"])
        self.assertFalse(accepted["schema_changed"] or accepted["unexpected_tables"])
        for key, value in (("valid", False), ("present", False), ("base_schema_sha256", "e" * 64)):
            changed = copy.deepcopy(after)
            changed["usage_tag_time_index_v1"][key] = value
            with self.subTest(key=key), self.assertRaises(cutover.CutoverError):
                cutover.compare_snapshots(before, changed, "usage-tag-time-index-v1")
        for baseline in (before, after):
            for table in before["tables"]:
                changed = copy.deepcopy(after)
                changed["tables"][table]["sha"] = "c" * 32
                comparison = cutover.compare_snapshots(baseline, changed, "usage-tag-time-index-v1")
                self.assertEqual(comparison["unexpected_tables"], [table])
        repeated = cutover.compare_snapshots(after, after, "usage-tag-time-index-v1")
        self.assertFalse(repeated["schema_changed"] or repeated["additive_schema_verified"])

    def test_rollback_recloses_partially_open_firewall_before_stopping_application(self):
        instance = self.prepared_cutover()
        instance.app_touched = instance.ingress_touched = True
        instance.rules = [{"tool": "iptables", "chain": "INPUT", "args": ["-p", "tcp"]}]
        with patch.object(cutover, "rule_present", side_effect=[False, True]), \
                patch.object(cutover, "command") as command, patch.object(cutover, "nginx_reload"), \
                patch.object(instance, "wait_healthy"), patch.object(instance, "remove_firewall"), \
                patch.object(instance, "save_state"):
            self.assertTrue(instance.rollback())
            self.assertEqual(command.call_args_list[0].args[1], "restore maintenance rule")
            self.assertEqual(command.call_args_list[1].args[1], "stop application before rollback")

    def test_failed_maintenance_restoration_keeps_running_application_for_manual_recovery(self):
        instance = self.prepared_cutover()
        instance.app_touched = instance.ingress_touched = True
        instance.rules = [{"tool": "iptables", "chain": "INPUT", "args": ["-p", "tcp"]}]
        with patch.object(cutover, "rule_present", return_value=False), \
                patch.object(cutover, "command", side_effect=cutover.CutoverError("rule failed")) as command, \
                patch.object(cutover, "nginx_reload"), patch.object(instance, "remove_firewall") as remove, \
                patch.object(instance, "save_state") as save:
            self.assertFalse(instance.rollback())
            self.assertEqual(command.call_count, 1)
            self.assertEqual(command.call_args.args[1], "restore maintenance rule")
            remove.assert_not_called()
            self.assertFalse(save.call_args.kwargs["database_restored"])
            self.assertEqual((self.root / "compose.yml").read_bytes(), self.compose)

    def test_snapshot_helper_rejects_existing_output_without_running_docker(self):
        target = self.backup / "existing.json"
        target.write_text("preserve me")
        with patch.object(db_snapshot, "database_output") as command, patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(db_snapshot.main(["postgres", str(target)]), 1)
            command.assert_not_called()
        self.assertEqual(target.read_text(), "preserve me")


if __name__ == "__main__":
    unittest.main()
