#!/usr/bin/env python3
"""Build a private, verifiable release from a committed Git ref; never deploy it."""

import argparse
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import subprocess
import sys
import tarfile
from urllib.parse import unquote, urlsplit


class BuildError(Exception):
    """An operational message safe to show without subprocess output."""


PACKAGE_FILES = (
    "Dockerfile", "new-api", ".dockerignore", "LICENSE", "NOTICE",
    "THIRD-PARTY-LICENSES.md",
)
DEPENDENCY_FILES = (
    "web/package.json", "web/bun.lock", "go.mod", "go.sum",
    "relaykit/go.mod", "relaykit/go.sum", "tokenkit/go.mod", "tokenkit/go.sum",
)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def run_command(argv, *, cwd, env, log, label, capture=False):
    """Keep all tool diagnostics private, including errors from package managers."""
    try:
        result = subprocess.run(
            argv, cwd=cwd, env=env, stdout=subprocess.PIPE if capture else log,
            stderr=log, check=False, timeout=1800,
        )
    except subprocess.TimeoutExpired:
        raise BuildError(label + " timed out; see private build.log") from None
    except OSError:
        raise BuildError(label + " could not start; see private build.log") from None
    if result.returncode:
        raise BuildError(label + " failed; see private build.log")
    return result.stdout.decode("utf-8").strip() if capture else None


def validate_runtime_base(image):
    if not re.fullmatch(r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}", image):
        raise BuildError("runtime base must be an image pinned by a full sha256 digest")
    repository = image.split("@", 1)[0].rsplit("/", 1)[-1].split(":", 1)[0]
    repository = repository.replace("new-api", "newapi", 1)
    if image.startswith("local/") and (
        repository == "newapi"
        or (repository.startswith("newapi-") and repository not in {"newapi-runtime", "newapi-runtime-base"})
    ):
        raise BuildError("runtime base cannot be a previous custom application release")
    # A fixed official new-api digest is a valid reusable runtime. Do not reject
    # it merely because its repository name contains the application's name.
    return image


def archive_snapshot(repo, ref, destination, env, log):
    if not ref or ref.startswith("-"):
        raise BuildError("ref must identify a committed Git revision")
    commit = run_command(
        ["git", "rev-parse", "--verify", "--end-of-options", ref + "^{commit}"],
        cwd=repo, env=env, log=log, label="resolve Git ref", capture=True,
    )
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise BuildError("Git returned an invalid commit identity")
    tree = run_command(
        ["git", "rev-parse", commit + "^{tree}"], cwd=repo, env=env, log=log,
        label="resolve Git tree", capture=True,
    )
    entries = run_command(
        ["git", "ls-tree", "-r", commit], cwd=repo, env=env, log=log,
        label="inspect Git tree", capture=True,
    )
    if any(line.startswith("160000 ") for line in entries.splitlines()):
        raise BuildError("Git submodules require an explicit archival strategy")
    archive = destination.parent / "source.tar"
    run_command(
        ["git", "archive", "--format=tar", "--output=" + str(archive), commit],
        cwd=repo, env=env, log=log, label="archive committed source",
    )
    destination.mkdir(mode=0o700)
    with tarfile.open(archive) as source:
        for member in source.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not (member.isfile() or member.isdir()):
                raise BuildError("source archive contains an unsafe path or link")
            target = destination.joinpath(*path.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as content, target.open("xb") as output:
                    shutil.copyfileobj(content, output)
                target.chmod(0o755 if member.mode & 0o111 else 0o600)
    archive.unlink()
    if (destination / "web/node_modules").exists() or (destination / "web/dist").exists():
        raise BuildError("committed source must not contain node_modules or web/dist")
    return commit, tree


def toolchain_from_snapshot(source):
    dockerfile = (source / "Dockerfile").read_text()
    bun = re.search(r"(?m)^FROM\s+oven/bun:(\d+\.\d+\.\d+)@sha256:[a-f0-9]{64}\b", dockerfile)
    go = re.search(r"(?m)^FROM\s+golang:(\d+\.\d+\.\d+)(?:-[\w.-]+)?@sha256:[a-f0-9]{64}\b", dockerfile)
    pgo = re.search(r"go list -m -f ['\"]\{\{\.Dir\}\}['\"] ([\w./-]+)", dockerfile)
    experiment = re.search(r"(?m)^ENV GOEXPERIMENT=([\w,]+)$", dockerfile)
    if not all((bun, go, pgo, experiment)):
        raise BuildError("Dockerfile toolchain or PGO configuration requires review")
    package = json.loads((source / "web/package.json").read_text())
    go_directive = re.search(r"(?m)^go (\S+)$", (source / "go.mod").read_text())
    if package.get("packageManager") != "bun@" + bun[1] or not go_directive or go_directive[1] != go[1]:
        raise BuildError("Dockerfile and dependency manifests disagree on toolchain versions")
    return {"go": go[1], "bun": bun[1], "pgo_module": pgo[1], "go_experiment": experiment[1]}


def dependency_identity(source, toolchain):
    files = {name: sha256(source / name) for name in DEPENDENCY_FILES}
    identity = {"platform": "linux/amd64", "toolchain": toolchain, "files": files}
    identity["sha256"] = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return identity


class FrontendReferences(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = set()
        self.scripts = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        reference = None
        if tag == "script":
            reference = attrs.get("src")
        elif tag == "link":
            reference = attrs.get("href")
        if not reference:
            return
        url = urlsplit(reference)
        if url.scheme or url.netloc:
            return
        path = PurePosixPath(unquote(url.path).lstrip("/"))
        if path.suffix not in {".js", ".css"}:
            return
        if ".." in path.parts:
            raise BuildError("frontend HTML references a path outside its asset directory")
        self.paths.add(path.as_posix())
        if tag == "script" and path.suffix == ".js":
            self.scripts += 1


def frontend_assets(dist):
    """Record the actual HTML entry and its JS/CSS, without provider-specific names."""
    parser = FrontendReferences()
    parser.feed((dist / "index.html").read_text())
    if not parser.scripts:
        raise BuildError("frontend build has no local JavaScript entry")
    assets = []
    for name in ["index.html", *sorted(parser.paths)]:
        path = dist / name
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(dist.resolve()):
            raise BuildError("frontend HTML references a missing or unsafe asset")
        assets.append({"path": name, "sha256": sha256(path)})
    return assets


def validate_static_amd64(binary):
    with binary.open("rb") as source:
        header = source.read(64)
        if len(header) != 64 or header[:7] != b"\x7fELF\x02\x01\x01":
            raise BuildError("build output is not a 64-bit little-endian ELF executable")
        fields = struct.unpack("<16sHHIQQQIHHHHHH", header)
        kind, machine, phoff, phsize, phcount = fields[1], fields[2], fields[5], fields[9], fields[10]
        if kind not in {2, 3} or machine != 62 or phsize != 56 or not phcount:
            raise BuildError("build output is not a Linux amd64 executable")
        if phoff < 64 or phoff + phsize * phcount > binary.stat().st_size:
            raise BuildError("build output has an invalid ELF program header")
        source.seek(phoff)
        for _ in range(phcount):
            if struct.unpack("<I", source.read(phsize)[:4])[0] == 3:  # PT_INTERP
                raise BuildError("build output requires a dynamic loader")


def build_release(args, now=None):
    database_mode = getattr(args, "database_mode", "unchanged")
    if database_mode not in {"unchanged", "usage-tags-v1", "usage-tag-time-index-v1"}:
        raise BuildError("unsupported database mode")
    runtime_base = validate_runtime_base(args.runtime_base)
    repo = Path(args.repo).expanduser().resolve(strict=True)
    out = Path(args.out).expanduser().absolute()
    if out.exists() or out.is_symlink():
        raise BuildError("output directory must be new; existing artifacts are never overwritten")
    go = shutil.which(str(Path(args.go).expanduser()))
    bun = shutil.which(str(Path(args.bun).expanduser()))
    if not go or not bun:
        raise BuildError("requested Go or Bun executable is unavailable")
    out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    out.mkdir(mode=0o700)
    env = dict(os.environ)
    for name in list(env):
        if name.startswith("VITE_") or name in {"NODE_OPTIONS", "NODE_PATH", "GOFLAGS", "GOWORK", "GOTOOLCHAIN"}:
            env.pop(name)
    env.update(GO111MODULE="on", GOWORK="off", GOENV="off", GOTOOLCHAIN="local", GOFLAGS="-mod=readonly")
    env["PATH"] = os.pathsep.join([str(Path(bun).parent), str(Path(go).parent), env.get("PATH", "")])
    source = out / "source"
    with (out / "build.log").open("xb") as log:
        (out / "build.log").chmod(0o600)
        commit, tree = archive_snapshot(repo, args.ref, source, env, log)
        toolchain = toolchain_from_snapshot(source)
        actual_go = run_command([go, "version"], cwd=source, env=env, log=log, label="check Go version", capture=True)
        actual_bun = run_command([bun, "--version"], cwd=source, env=env, log=log, label="check Bun version", capture=True)
        if not actual_go.startswith("go version go" + toolchain["go"] + " ") or actual_bun != toolchain["bun"]:
            raise BuildError("installed Go/Bun versions do not match the committed Dockerfile")
        now = now or datetime.now(timezone.utc)
        release_id = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + commit[:12]
        tags = run_command(["git", "tag", "--points-at", commit], cwd=repo, env=env, log=log, label="read release tags", capture=True)
        base_version = next((tag for tag in sorted(tags.splitlines()) if re.fullmatch(r"v?\d[\w.+-]{0,63}", tag)), "")
        if not base_version:
            declared = (source / "VERSION").read_text().strip()
            base_version = declared if re.fullmatch(r"v?\d[\w.+-]{0,63}", declared) else "commit"
        version = base_version + "-deploy." + release_id.lower()
        (source / "VERSION").write_text(version + "\n")
        identity = dependency_identity(source, toolchain)
        cache = Path(env.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))).expanduser() / "newapi-deploy-build"
        cache_paths = {
            "BUN_INSTALL_CACHE_DIR": cache / ("bun-" + toolchain["bun"] + "-" + identity["files"]["web/bun.lock"][:16]),
            "GOMODCACHE": cache / "go-modules",
            "GOCACHE": cache / ("go-" + toolchain["go"] + "-linux-amd64"),
        }
        for name, path in cache_paths.items():
            # Download/build caches are content-addressed. Explicit local cache
            # locations can be reused; installed node_modules never is. Go's
            # module cache is verified below before compiling any application.
            path = Path(env.get(name) or path).expanduser()
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            env[name] = str(path.resolve())
        env.update(GOOS="linux", GOARCH="amd64", CGO_ENABLED="0", GOEXPERIMENT=toolchain["go_experiment"])
        env.update(VITE_REACT_APP_VERSION=version, DISABLE_ESLINT_PLUGIN="true")
        run_command([bun, "install", "--frozen-lockfile"], cwd=source / "web", env=env, log=log, label="install locked frontend dependencies")
        if (source / "web/node_modules").is_symlink():
            raise BuildError("node_modules must belong to the isolated snapshot, not be a symlink")
        run_command([bun, "run", "build"], cwd=source / "web", env=env, log=log, label="build frontend")
        assets = frontend_assets(source / "web/dist")
        run_command([go, "mod", "download"], cwd=source, env=env, log=log, label="download locked Go modules")
        run_command([go, "mod", "verify"], cwd=source, env=env, log=log, label="verify cached Go modules")
        module = run_command([go, "list", "-m", "-f", "{{.Dir}}", toolchain["pgo_module"]], cwd=source, env=env, log=log, label="resolve PGO module", capture=True)
        pgo = Path(module) / "default.pgo"
        if not pgo.is_file():
            raise BuildError("the committed PGO module has no default.pgo")
        run_command([
            go, "build", "-trimpath", "-pgo=" + str(pgo), "-ldflags",
            "-s -w -X github.com/QuantumNous/new-api/common.Version=" + version,
            "-o", str(out / "new-api"), ".",
        ], cwd=source, env=env, log=log, label="build static Linux application")
        validate_static_amd64(out / "new-api")
        if dependency_identity(source, toolchain) != identity:
            raise BuildError("build changed dependency manifests; release requires review")
        for name in ("LICENSE", "NOTICE", "THIRD-PARTY-LICENSES.md"):
            shutil.copyfile(source / name, out / name)
        dockerfile = (
            f"FROM {runtime_base}\n"
            f'LABEL org.opencontainers.image.revision="{commit}" \\\n'
            f'      org.opencontainers.image.version="{version}"\n'
            "COPY new-api /new-api\n"
            "COPY LICENSE NOTICE THIRD-PARTY-LICENSES.md /licenses/\n"
            "EXPOSE 3000\nWORKDIR /data\nENTRYPOINT [\"/new-api\"]\n"
        )
        (out / "Dockerfile").write_text(dockerfile)
        (out / ".dockerignore").write_text("*\n" + "".join("!" + name + "\n" for name in PACKAGE_FILES))
        package = out / "image-context.tgz"
        with tarfile.open(package, "w:gz") as archive:
            for name in PACKAGE_FILES:
                archive.add(out / name, arcname=name, recursive=False)
        package.chmod(0o600)
        manifest = {
            "format": 1, "release_id": release_id, "commit": commit, "tree": tree,
            "version": version, "platform": "linux/amd64", "database_mode": database_mode,
            "runtime_base_image": runtime_base, "image_tag": "local/newapi:" + release_id.lower(),
            "binary_sha256": sha256(out / "new-api"), "package_sha256": sha256(package),
            "package_bytes": package.stat().st_size, "frontend_assets": assets,
            "toolchain": toolchain, "dependency_identity": identity,
            "pgo_sha256": sha256(pgo),
        }
        (out / "release.json").write_text(json.dumps(manifest, indent=2) + "\n")
        (out / "release.json").chmod(0o600)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--runtime-base", required=True)
    parser.add_argument("--database-mode", choices=("unchanged", "usage-tags-v1", "usage-tag-time-index-v1"), default="unchanged")
    parser.add_argument("--go", default="go")
    parser.add_argument("--bun", default="bun")
    args = parser.parse_args(argv)
    old_umask = os.umask(0o077)
    try:
        manifest = build_release(args)
        print(json.dumps({"event": "release_built", "release_id": manifest["release_id"], "commit": manifest["commit"], "package_sha256": manifest["package_sha256"]}))
        return 0
    except BuildError as error:
        print(json.dumps({"event": "build_failed", "reason": str(error)}), file=sys.stderr)
        return 1
    except Exception:
        print(json.dumps({"event": "build_failed", "reason": "build failed; inspect the private release directory"}), file=sys.stderr)
        return 1
    finally:
        os.umask(old_umask)


if __name__ == "__main__":
    sys.exit(main())
