"""Release contracts exercised with real Git archives and synthetic build tools."""

import argparse
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import build_release as release


RUNTIME = "calciumion/new-api@sha256:" + "9" * 64
DOCKERFILE = """FROM oven/bun:1.4.4@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa AS builder
FROM golang:1.26.10-alpine@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb AS builder2
ENV GOEXPERIMENT=greenteagc
RUN go build -pgo="$(go list -m -f '{{.Dir}}' github.com/Calcium-Ion/moejs)/default.pgo"
"""
FAKE_TOOL = r'''#!PYTHON
import json,os,pathlib,struct,sys
name=pathlib.Path(sys.argv[0]).name
args=sys.argv[1:]
observed=pathlib.Path(os.environ['TEST_TOOL_OBSERVATIONS'])
with observed.open('a') as output:
    output.write(json.dumps({'tool':name,'args':args,'cwd':str(pathlib.Path.cwd()),'env':{key:os.environ.get(key) for key in ['GOWORK','GOENV','GOTOOLCHAIN','GOFLAGS','GOOS','GOARCH','CGO_ENABLED','GOEXPERIMENT','BUN_INSTALL_CACHE_DIR','GOMODCACHE','GOCACHE','VITE_REACT_APP_VERSION']}})+'\n')
if name=='bun' and args==['--version']:
    print(os.environ.get('TEST_BUN_VERSION','1.4.4'))
elif name=='go' and args==['version']:
    print('go version go1.26.10 synthetic/amd64')
elif name=='bun' and args==['install','--frozen-lockfile']:
    if os.environ.get('TEST_LINK_NODE_MODULES'):
        pathlib.Path('node_modules').symlink_to(os.environ['TEST_LINK_NODE_MODULES'])
    else:
        pathlib.Path('node_modules').mkdir()
elif name=='bun' and args==['run','build']:
    if os.environ.get('TEST_TOOL_FAILURE'):
        print('sensitive synthetic diagnostic',file=sys.stderr);sys.exit(7)
    root=pathlib.Path('dist');(root/'static').mkdir(parents=True)
    (root/'index.html').write_text('<html><script src="/static/index.current.js"></script><link rel="stylesheet" href="/static/current.css"></html>')
    (root/'static/index.current.js').write_text('console.log("committed frontend");')
    (root/'static/current.css').write_text('body { color: black; }')
elif name=='go' and args in (['mod','download'],['mod','verify']):
    pass
elif name=='go' and args[:2]==['list','-m']:
    print(pathlib.Path(sys.argv[0]).parent/'pgo')
elif name=='go' and args[0]=='build':
    assert pathlib.Path('web/dist/index.html').is_file()
    output=pathlib.Path(args[args.index('-o')+1])
    header=struct.pack('<16sHHIQQQIHHHHHH',b'\x7fELF\x02\x01\x01'+b'\0'*9,2,62,1,0,64,0,0,64,56,1,0,0,0)
    output.write_bytes(header+struct.pack('<IIQQQQQQ',1,5,0,0,0,120,120,4096))
    output.chmod(0o755)
else:
    raise SystemExit('unexpected synthetic tool command')
'''


class ReleaseBuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Release test")
        self.git("config", "user.email", "release@example.invalid")
        files = {
            "Dockerfile": DOCKERFILE, "VERSION": "v1.2.3\n",
            "web/package.json": '{"packageManager":"bun@1.4.4"}',
            "web/bun.lock": "committed lock\n", "web/src.txt": "committed source\n",
            "go.mod": "module github.com/QuantumNous/new-api\n\ngo 1.26.10\n",
            "go.sum": "root sum\n", "relaykit/go.mod": "module relaykit\n",
            "relaykit/go.sum": "relay sum\n", "tokenkit/go.mod": "module tokenkit\n",
            "tokenkit/go.sum": "token sum\n", "LICENSE": "license\n",
            "NOTICE": "notice\n", "THIRD-PARTY-LICENSES.md": "third-party notices\n",
        }
        for name, text in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self.git("add", ".")
        self.git("commit", "-qm", "committed fixture")
        self.commit = self.git("rev-parse", "HEAD")
        self.tree = self.git("rev-parse", "HEAD^{tree}")
        self.git("tag", "v2.0.0")
        self.tools = self.root / "tools"
        self.tools.mkdir()
        for name in ("go", "bun"):
            tool = self.tools / name
            tool.write_text(FAKE_TOOL.replace("#!PYTHON", "#!" + sys.executable))
            tool.chmod(0o700)
        (self.tools / "pgo").mkdir()
        (self.tools / "pgo/default.pgo").write_text("synthetic profile")
        self.observations = self.root / "observations.jsonl"
        self.env = patch.dict(os.environ, {
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "TEST_TOOL_OBSERVATIONS": str(self.observations),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.args = argparse.Namespace(
            repo=str(self.repo), ref="v2.0.0", out=str(self.root / "release"),
            runtime_base=RUNTIME, go=str(self.tools / "go"), bun=str(self.tools / "bun"),
        )
        self.now = datetime(2026, 10, 30, 5, 25, 58, 123456, timezone.utc)

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.repo, stderr=subprocess.DEVNULL, text=True).strip()

    def test_build_uses_committed_ref_and_packages_verified_frontend_and_licenses(self):
        (self.repo / "web/src.txt").write_text("uncommitted replacement")
        (self.repo / "web/bun.lock").write_text("uncommitted lock")
        (self.repo / "untracked.txt").write_text("must not be archived")
        manifest = release.build_release(self.args, self.now)
        out = Path(self.args.out)
        self.assertEqual((out / "source/web/src.txt").read_text(), "committed source\n")
        self.assertEqual((out / "source/web/bun.lock").read_text(), "committed lock\n")
        self.assertFalse((out / "source/untracked.txt").exists())
        self.assertEqual(manifest["commit"], self.commit)
        self.assertEqual(manifest["tree"], self.tree)
        self.assertEqual(manifest["release_id"], "20261030T052558123456Z-" + self.commit[:12])
        self.assertTrue(manifest["version"].startswith("v2.0.0-deploy."))
        self.assertEqual((out / "source/VERSION").read_text().strip(), manifest["version"])
        self.assertEqual(manifest["format"], 1)
        self.assertEqual(manifest["platform"], "linux/amd64")
        self.assertEqual(manifest["database_mode"], "unchanged")
        self.assertEqual(manifest["runtime_base_image"], RUNTIME)
        self.assertEqual(manifest["image_tag"], "local/newapi:" + manifest["release_id"].lower())
        self.assertEqual(manifest["binary_sha256"], release.sha256(out / "new-api"))
        self.assertEqual(manifest["package_sha256"], release.sha256(out / "image-context.tgz"))
        self.assertEqual(manifest["package_bytes"], (out / "image-context.tgz").stat().st_size)
        self.assertEqual(json.loads((out / "release.json").read_text()), manifest)
        with tarfile.open(out / "image-context.tgz") as archive:
            self.assertEqual(set(archive.getnames()), set(release.PACKAGE_FILES))
            self.assertTrue(all(member.isfile() for member in archive.getmembers()))
            self.assertEqual(archive.extractfile("LICENSE").read(), b"license\n")
            self.assertEqual(archive.extractfile("NOTICE").read(), b"notice\n")
            self.assertEqual(archive.extractfile("THIRD-PARTY-LICENSES.md").read(), b"third-party notices\n")
            dockerfile = archive.extractfile("Dockerfile").read().decode()
        self.assertEqual([line for line in dockerfile.splitlines() if line.startswith("FROM ")], ["FROM " + RUNTIME])
        self.assertFalse(any(line.startswith(("RUN ", "ARG ", "ADD ")) for line in dockerfile.splitlines()))
        self.assertEqual({asset["path"] for asset in manifest["frontend_assets"]}, {"index.html", "static/index.current.js", "static/current.css"})
        for asset in manifest["frontend_assets"]:
            self.assertEqual(asset["sha256"], release.sha256(out / "source/web/dist" / asset["path"]))
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((out / "build.log").stat().st_mode), 0o600)
        calls = [json.loads(line) for line in self.observations.read_text().splitlines()]
        build = next(call for call in calls if call["tool"] == "go" and call["args"][0] == "build")
        self.assertEqual(build["env"]["GOOS"], "linux")
        self.assertEqual(build["env"]["GOARCH"], "amd64")
        self.assertEqual(build["env"]["CGO_ENABLED"], "0")
        self.assertEqual(build["env"]["GOWORK"], "off")
        self.assertEqual(build["env"]["GOFLAGS"], "-mod=readonly")
        self.assertIn("-trimpath", build["args"])
        self.assertTrue(any(arg.startswith("-pgo=") for arg in build["args"]))
        self.assertFalse((out / "source/web/node_modules").is_symlink())

    def test_dependency_cache_identity_changes_only_with_toolchain_or_dependency_manifests(self):
        toolchain = release.toolchain_from_snapshot(self.repo)
        original = release.dependency_identity(self.repo, toolchain)
        (self.repo / "web/src.txt").write_text("different app source")
        self.assertEqual(release.dependency_identity(self.repo, toolchain), original)
        (self.repo / "web/bun.lock").write_text("different dependency lock")
        self.assertNotEqual(release.dependency_identity(self.repo, toolchain)["sha256"], original["sha256"])
        (self.repo / "web/bun.lock").write_text("committed lock\n")
        self.assertNotEqual(release.dependency_identity(self.repo, dict(toolchain, bun="1.4.5"))["sha256"], original["sha256"])

    def test_explicit_usage_tags_database_mode_is_recorded(self):
        self.args.database_mode = "usage-tags-v1"
        manifest = release.build_release(self.args, self.now)
        self.assertEqual(manifest["database_mode"], "usage-tags-v1")
        self.args.database_mode = "migrate"
        with self.assertRaises(release.BuildError):
            release.build_release(self.args, self.now)

    def test_explicit_usage_time_index_database_mode_is_recorded(self):
        self.args.database_mode = "usage-tag-time-index-v1"
        manifest = release.build_release(self.args, self.now)
        self.assertEqual(manifest["database_mode"], "usage-tag-time-index-v1")

    def test_same_committed_dependencies_reuse_download_cache_without_sharing_node_modules(self):
        first_source = Path(self.args.out) / "source/web/node_modules"
        existing_caches = {name: str(self.root / name) for name in ("BUN_INSTALL_CACHE_DIR", "GOMODCACHE", "GOCACHE")}
        with patch.dict(os.environ, existing_caches):
            first = release.build_release(self.args, self.now)
            self.args.out = str(self.root / "second-release")
            second = release.build_release(self.args, self.now.replace(microsecond=123457))
        self.assertEqual(first["dependency_identity"], second["dependency_identity"])
        self.assertNotEqual(first["release_id"], second["release_id"])
        installs = [json.loads(line) for line in self.observations.read_text().splitlines() if 'frozen-lockfile' in line]
        self.assertEqual(len(installs), 2)
        self.assertEqual(installs[0]["env"]["BUN_INSTALL_CACHE_DIR"], installs[1]["env"]["BUN_INSTALL_CACHE_DIR"])
        for name, path in existing_caches.items():
            self.assertEqual(installs[0]["env"][name], str(Path(path).resolve()))
        self.assertNotEqual(first_source.resolve(), (Path(self.args.out) / "source/web/node_modules").resolve())

    def test_floating_or_previous_custom_runtime_is_rejected_before_creating_output(self):
        for base in ("calciumion/new-api:latest", "debian@sha256:1234", "local/newapi@sha256:" + "a" * 64, "local/newapi-search:old@sha256:" + "a" * 64):
            with self.subTest(base=base):
                self.args.runtime_base = base
                with self.assertRaises(release.BuildError):
                    release.build_release(self.args, self.now)
                self.assertFalse(Path(self.args.out).exists())
        self.assertEqual(release.validate_runtime_base(RUNTIME), RUNTIME)

    def test_existing_release_is_never_overwritten(self):
        out = Path(self.args.out)
        out.mkdir()
        (out / "preserve").write_text("existing release")
        with self.assertRaises(release.BuildError):
            release.build_release(self.args, self.now)
        self.assertEqual((out / "preserve").read_text(), "existing release")

    def test_wrong_toolchain_and_linked_node_modules_cannot_produce_release_manifest(self):
        external = self.root / "existing-node-modules"
        external.mkdir()
        cases = ({"TEST_BUN_VERSION": "1.4.5"}, {"TEST_LINK_NODE_MODULES": str(external)})
        for index, environment in enumerate(cases):
            self.args.out = str(self.root / ("invalid-" + str(index)))
            with self.subTest(environment=list(environment)), patch.dict(os.environ, environment):
                with self.assertRaises(release.BuildError):
                    release.build_release(self.args, self.now)
                self.assertFalse((Path(self.args.out) / "release.json").exists())

    def test_committed_symlink_is_rejected_without_following_it(self):
        (self.repo / "external-link").symlink_to(self.root)
        self.git("add", "external-link")
        self.git("commit", "-qm", "unsafe link")
        self.args.ref = "HEAD"
        with self.assertRaises(release.BuildError):
            release.build_release(self.args, self.now)
        self.assertFalse((Path(self.args.out) / "release.json").exists())

    def test_cli_keeps_tool_failure_diagnostics_out_of_console(self):
        arguments = ["--repo", self.args.repo, "--ref", self.args.ref, "--out", self.args.out, "--runtime-base", RUNTIME, "--go", self.args.go, "--bun", self.args.bun]
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"TEST_TOOL_FAILURE": "yes"}), patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            self.assertEqual(release.main(arguments), 1)
        self.assertNotIn("sensitive synthetic diagnostic", stdout.getvalue() + stderr.getvalue())
        self.assertIn("sensitive synthetic diagnostic", (Path(self.args.out) / "build.log").read_text())
        self.assertFalse((Path(self.args.out) / "release.json").exists())

    def test_frontend_manifest_rejects_missing_entry_and_missing_referenced_asset(self):
        dist = self.root / "dist"
        dist.mkdir()
        for html in ("<html></html>", '<html><script src="/missing.js"></script></html>', '<script src="../../outside.js"></script>'):
            with self.subTest(html=html):
                (dist / "index.html").write_text(html)
                with self.assertRaises(release.BuildError):
                    release.frontend_assets(dist)

    def test_binary_validation_rejects_dynamic_loader_and_wrong_architecture(self):
        binary = self.root / "binary"
        for machine, program_type in ((183, 1), (62, 3)):
            with self.subTest(machine=machine, program_type=program_type):
                header = struct.pack("<16sHHIQQQIHHHHHH", b"\x7fELF\x02\x01\x01" + b"\0" * 9, 2, machine, 1, 0, 64, 0, 0, 64, 56, 1, 0, 0, 0)
                binary.write_bytes(header + struct.pack("<IIQQQQQQ", program_type, 5, 0, 0, 0, 120, 120, 4096))
                with self.assertRaises(release.BuildError):
                    release.validate_static_amd64(binary)


if __name__ == "__main__":
    unittest.main()
