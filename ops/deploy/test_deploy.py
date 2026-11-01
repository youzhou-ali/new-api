import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import deploy
import ssh_transport


class LocalTransport:
    """Execute only the generated artifact/file programs inside a temporary root."""
    def __init__(self, root):
        self.target = {"remote": {"root": str(root.resolve())}}
        self.uploads = 0

    def python(self, source, **kwargs):
        result = subprocess.run([sys.executable, "-c", source], capture_output=True)
        if result.returncode:
            raise RuntimeError("remote program rejected operation")
        return result.stdout

    def binary(self, source, data=None, **kwargs):
        self.uploads += 1
        result = subprocess.run([sys.executable, "-c", source], input=data, capture_output=True)
        if result.returncode:
            raise RuntimeError("transfer rejected")
        return result.stdout


class DeploymentTests(unittest.TestCase):
    def make_release(self, root):
        release = root / "local"
        release.mkdir()
        package = bytes(range(256)) * 32
        (release / "image-context.tgz").write_bytes(package)
        value = {"format": 1, "release_id": "test-0123456789ab", "commit": "a" * 40, "database_mode": "unchanged",
                 "platform": "linux/amd64", "binary_sha256": "b" * 64,
                 "package_sha256": hashlib.sha256(package).hexdigest(), "package_bytes": len(package)}
        (release / "release.json").write_text(json.dumps(value))
        return release, package

    def make_recovery_fixture(self, root):
        local, _ = self.make_release(root)
        remote = root.resolve() / "remote"
        remote.mkdir()
        target = LocalTransport(remote)
        release = remote / "releases" / "test-0123456789ab"
        backup = remote / "backups" / "test-0123456789ab"
        release.mkdir(parents=True)
        backup.mkdir(parents=True)
        (release / "release.json").write_bytes((local / "release.json").read_bytes())
        (release / "target.json").write_text(json.dumps(target.target["remote"]))
        (release / "deployment-tools.json").write_text("{}")
        for name in ("production-final.dump", "postgres-globals.sql", "docker-compose.yml.original",
                     "nginx.conf.original", "containers.original.json", "application-runtime-before.json",
                     "production-before.json", "production-after.json"):
            (backup / name).write_bytes(b"nonsecret test evidence")
        (backup / "cutover-state.json").write_text('{"phase":"success"}')
        (release / "prepare.json").write_text(json.dumps({"env_file_present": False,
            "config_sha256": deploy.digest((backup / "docker-compose.yml.original").read_bytes()),
            "nginx_sha256": deploy.digest((backup / "nginx.conf.original").read_bytes())}))
        with tarfile.open(backup / "application-final.tgz", "w:gz") as out:
            for name, data in (("data/example", b"application data"), ("logs/example", b"application log")):
                entry = tarfile.TarInfo(name)
                entry.size = len(data)
                out.addfile(entry, io.BytesIO(data))
        for name in ("production-final.dump", "application-final.tgz"):
            (backup / (name + ".sha256")).write_text(deploy.digest((backup / name).read_bytes()) + "  " + name + "\n")
        return local, backup, target

    def test_collect_checks_complete_recovery_contents_and_recovers_from_download_interruption(self):
        with tempfile.TemporaryDirectory() as temporary:
            local, backup, transport = self.make_recovery_fixture(Path(temporary))
            with patch.object(transport, "binary", side_effect=RuntimeError("interrupted")):
                with self.assertRaises(RuntimeError):
                    deploy.collect(transport, local)
            self.assertFalse((local / "recovery-data.tgz").exists())
            self.assertFalse((local / "recovery.json").exists())
            result = deploy.collect(transport, local)
            archive = local / "recovery-data.tgz"
            self.assertEqual(deploy.digest(archive.read_bytes()), result["sha256"])
            self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
            with tarfile.open(archive) as recovered:
                self.assertIn("production-final.dump", recovered.getnames())
                self.assertIn("data/example", recovered.getnames())
                self.assertNotIn("logs/example", recovered.getnames())

    def test_collect_rejects_missing_or_corrupt_required_backups_without_success_record(self):
        for broken in ("production-final.dump", "postgres-globals.sql", "production-after.json", "checksum"):
            with self.subTest(broken=broken), tempfile.TemporaryDirectory() as temporary:
                local, backup, transport = self.make_recovery_fixture(Path(temporary))
                if broken == "checksum":
                    (backup / "production-final.dump").write_bytes(b"changed after backup")
                else:
                    (backup / broken).unlink()
                with self.assertRaises(RuntimeError):
                    deploy.collect(transport, local)
                self.assertFalse((local / "recovery.json").exists())
                self.assertFalse((local / "recovery-data.tgz").exists())

    def test_collect_rejects_preexisting_incomplete_remote_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            local, backup, transport = self.make_recovery_fixture(Path(temporary))
            with tarfile.open(backup / "recovery-data.tgz", "w:gz") as archive:
                archive.add(backup / "postgres-globals.sql", arcname="postgres-globals.sql")
            with self.assertRaises(RuntimeError):
                deploy.collect(transport, local)
            self.assertFalse((local / "recovery.json").exists())

    def assert_additive_recovery_evidence(self, mode):
        with tempfile.TemporaryDirectory() as temporary:
            local, backup, transport = self.make_recovery_fixture(Path(temporary))
            remote_release = Path(transport.target["remote"]["root"]) / "releases" / "test-0123456789ab"
            manifest = json.loads((local / "release.json").read_text())
            manifest["database_mode"] = mode
            for directory in (local, remote_release):
                (directory / "release.json").write_text(json.dumps(manifest))
            prepared = json.loads((remote_release / "prepare.json").read_text())
            prepared["rollback_rehearsal_passed"] = True
            (remote_release / "prepare.json").write_text(json.dumps(prepared))
            names = ("production-initial.dump", "rehearsal-before.json", "rehearsal-after-1.json",
                     "rehearsal-after-2.json", "rehearsal-rollback.json", "rehearsal-comparison.json")
            for name in names[:-1]:
                (backup / name).write_bytes(b"compatibility evidence")
            with self.assertRaises(RuntimeError):
                deploy.collect(transport, local)
            self.assertFalse((local / "recovery.json").exists())
            (backup / names[-1]).write_bytes(b"compatibility evidence")
            deploy.collect(transport, local)
            with tarfile.open(local / "recovery-data.tgz") as archive:
                self.assertTrue(set(names).issubset(archive.getnames()))


    def test_usage_tags_recovery_requires_and_collects_compatibility_evidence(self):
        self.assert_additive_recovery_evidence("usage-tags-v1")

    def test_usage_time_index_recovery_requires_and_collects_compatibility_evidence(self):
        self.assert_additive_recovery_evidence("usage-tag-time-index-v1")

    def test_stage_reuses_verified_chunks_and_rejects_corruption(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release, package = self.make_release(root)
            helpers = root / "helpers"
            helpers.mkdir()
            for name in deploy.HELPERS:
                (helpers / name).write_text("# test deployment helper\n")
            remote = root / "remote"
            remote.mkdir()
            transport = LocalTransport(remote)
            with patch.object(deploy, "HERE", helpers), patch.object(deploy, "CHUNK_SIZE", 1024):
                result = deploy.stage(transport, release)
                self.assertEqual(transport.uploads, 8)
                self.assertEqual((Path(result["remote_directory"]) / "image-context.tgz").read_bytes(), package)
                deploy.stage(transport, release)
                self.assertEqual(transport.uploads, 8)
                (Path(result["remote_directory"]) / "package.part0003").write_bytes(b"corruption")
                with self.assertRaises(RuntimeError):
                    deploy.stage(transport, release)
                self.assertEqual(transport.uploads, 8)

    def test_local_package_mismatch_stops_before_remote_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release, _ = self.make_release(root)
            (release / "image-context.tgz").write_bytes(b"changed")
            transport = LocalTransport(root)
            with self.assertRaises(ValueError):
                deploy.stage(transport, release)
            self.assertEqual(transport.uploads, 0)
            self.assertFalse((root / "releases").exists())

    def test_refuses_traversal_release_id(self):
        with self.assertRaises(ValueError):
            deploy.release_directory({"remote": {"root": "/srv/newapi"}}, "../../etc")

    def test_public_verification_checks_actual_assets_and_tolerates_runtime_html_injection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release, _ = self.make_release(root)
            value = json.loads((release / "release.json").read_text())
            value.update(commit="a" * 40, version="test-version", frontend_assets=[
                {"path": "index.html", "sha256": "c" * 64},
                {"path": "static/app.js", "sha256": deploy.digest(b"expected-script")},
            ])
            (release / "release.json").write_text(json.dumps(value))
            transport = LocalTransport(root)
            transport.target["remote"]["public_url"] = "https://example.invalid"
            homepage = b'<script src="/static/app.js"></script><!-- runtime analytics -->'
            responses = [json.dumps({"success": True, "data": {"version": "test-version"}}).encode(),
                         homepage, b"expected-script"]
            runtime = json.dumps({"image_id": "sha256:" + "b" * 64, "healthy": True}).encode()
            with patch.object(transport, "python", return_value=runtime), patch.object(deploy, "get_public", side_effect=responses):
                result = deploy.verify(transport, release)
            self.assertEqual(result["frontend_assets_verified"], 1)
            self.assertEqual(result["paid_provider_requests"], 0)
            responses[-1] = b"stale-script"
            with patch.object(transport, "python", return_value=runtime), patch.object(deploy, "get_public", side_effect=responses):
                with self.assertRaises(ValueError):
                    deploy.verify(transport, release)

    def test_no_mutation_without_explicit_execute(self):
        with patch.object(sys, "argv", ["deploy.py", "cutover", "--target", "/not-read.json", "--release-id", "valid"]):
            with patch.object(deploy, "load_target") as load:
                with self.assertRaises(SystemExit):
                    deploy.main()
                load.assert_not_called()

    def test_apply_authorization_not_inherited_by_read_only_commands(self):
        target = {"profile_path": "/tmp/profile", "id": "test", "host": "example.invalid", "user": "root",
                  "known_hosts": "/tmp/known", "ssh_controller": "/tmp/controller"}
        with patch.object(deploy, "check_host_key"):
            transport = deploy.Transport(target)
        with patch.dict(os.environ, {"TARGET_CHANGE_APPROVED": "YES", "TARGET_SSH_IDENTITY": "/wrong-key"}):
            self.assertNotIn("TARGET_CHANGE_APPROVED", transport.env(30))
            self.assertNotIn("TARGET_SSH_IDENTITY", transport.env(30))
            self.assertEqual(transport.env(30, True)["TARGET_CHANGE_APPROVED"], "YES")

    def test_credential_assignment_is_read_without_shell_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = root / "shell-profile"
            sentinel = root / "must-not-exist"
            profile.write_text("echo irrelevant\nexport DEPLOY_TEST_PASSWORD='literal-value'\n" +
                               "touch " + str(sentinel) + "\n")
            with patch.dict(os.environ, {}, clear=True):
                value = ssh_transport.read_password({"password_env": "DEPLOY_TEST_PASSWORD",
                                                      "password_assignment_file": str(profile)})
            self.assertEqual(value, "literal-value")
            self.assertFalse(sentinel.exists())

    def test_ssh_rejects_target_override_before_credential_access(self):
        target = {"host": "example.invalid", "user": "root", "known_hosts": "/pinned", "port": 22}
        args = ["ssh_transport.py", "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=/pinned",
                "-o", "StrictHostKeyChecking=no", "root@example.invalid", "true"]
        with patch.dict(os.environ, {"NEWAPI_DEPLOY_TARGET": "/profile"}), patch.object(sys, "argv", args):
            with patch.object(ssh_transport, "load_target", return_value=target), patch.object(ssh_transport, "check_host_key"):
                with patch.object(ssh_transport, "read_password") as password:
                    with self.assertRaises(ValueError):
                        ssh_transport.main()
                    password.assert_not_called()

    def test_auth_header_is_not_forwarded_through_redirects(self):
        handler = deploy.NoRedirect()
        request = deploy.urllib.request.Request("https://example.invalid/v1/models", headers={"Authorization": "Bearer test"})
        self.assertIsNone(handler.redirect_request(request, None, 302, "moved", {}, "https://other.invalid/"))


if __name__ == "__main__":
    unittest.main()
