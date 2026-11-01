#!/usr/bin/env python3
"""Bound SSH transport; credentials never appear in argv or deployment files."""
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys


def load_target(path):
    path = Path(path).expanduser().resolve()
    if path.stat().st_mode & 0o077:
        raise ValueError("target profile must be private (chmod 600)")
    value = json.loads(path.read_text())
    for field in ("id", "host", "user"):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", value[field]):
            raise ValueError("invalid target binding")
    if not 1 <= int(value.get("port", 22)) <= 65535:
        raise ValueError("invalid SSH port")
    if not Path(value["known_hosts"]).is_file():
        raise ValueError("known_hosts is missing")
    if not Path(value["ssh_controller"]).is_file():
        raise ValueError("target-ssh-controller is missing")
    value["profile_path"] = str(path)
    return value


def check_host_key(target):
    port = int(target.get("port", 22))
    lookup = target["host"] if port == 22 else f'[{target["host"]}]:{port}'
    found = subprocess.run(["ssh-keygen", "-F", lookup, "-f", target["known_hosts"]],
                           capture_output=True, check=False)
    if found.returncode or not found.stdout:
        raise ValueError("target host key is not pinned")
    expected = target.get("host_key_sha256")
    if expected:
        fingerprints = subprocess.run(["ssh-keygen", "-l", "-E", "sha256", "-f", "/dev/stdin"],
                                      input=found.stdout, capture_output=True, check=False)
        observed = [line.split()[1] for line in fingerprints.stdout.decode().splitlines()
                    if len(line.split()) > 1]
        if fingerprints.returncode or expected not in observed:
            raise ValueError("pinned host fingerprint differs from target profile")


def read_password(target):
    name = target.get("password_env")
    if not name:
        return None
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError("invalid credential variable name")
    password = os.environ.get(name)
    source = target.get("password_assignment_file")
    if not password and source:
        # Read one literal assignment, never source or execute a shell profile.
        for line in Path(source).expanduser().read_text().splitlines():
            if re.match(r"^\s*(?:export\s+)?" + re.escape(name) + "=", line):
                words = shlex.split(line, comments=True)
                if words and words[0] == "export":
                    words.pop(0)
                if len(words) != 1 or not words[0].startswith(name + "="):
                    raise ValueError("credential must be a literal assignment")
                password = words[0].split("=", 1)[1]
                if "$" in password or "`" in password:
                    raise ValueError("shell expansion in credentials is unsupported")
    if not password or "\n" in password or "\r" in password:
        raise ValueError("SSH credential unavailable")
    return password


def main():
    target = load_target(os.environ["NEWAPI_DEPLOY_TARGET"])
    check_host_key(target)
    args = sys.argv[1:]
    destination = target["user"] + "@" + target["host"]
    if destination not in args:
        raise ValueError("SSH destination differs from profile")
    if "StrictHostKeyChecking=yes" not in args or "UserKnownHostsFile=" + target["known_hosts"] not in args:
        raise ValueError("strict host binding is required")
    # Reject any later option that could override binding or route through another target.
    allowed = {"BatchMode=yes", "IdentitiesOnly=yes", "StrictHostKeyChecking=yes",
               "UserKnownHostsFile=" + target["known_hosts"]}
    index = 0
    while index < args.index(destination):
        flag = args[index]
        if flag not in ("-o", "-p", "-i") or index + 1 >= len(args):
            raise ValueError("unexpected SSH option")
        value = args[index + 1]
        if flag == "-o" and value not in allowed and not re.fullmatch(r"ConnectTimeout=\d+", value):
            raise ValueError("unexpected SSH option value")
        if flag == "-p" and value != str(target.get("port", 22)):
            raise ValueError("SSH port differs from profile")
        if flag == "-i" and value != target.get("identity_file"):
            raise ValueError("SSH identity differs from profile")
        index += 2
    limit = int(os.environ.get("TARGET_TIMEOUT_SECONDS", "60"))
    if not 1 <= limit <= 3600:
        raise ValueError("invalid SSH timeout")
    remote = args.index(destination) + 1
    args[remote:remote] = ["timeout", str(limit)]
    extra = ["-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "ClearAllForwardings=yes",
             "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3"]
    password = read_password(target)
    child_env = dict(os.environ)
    child_env.pop(target.get("password_env", ""), None)
    if password is None:
        return subprocess.run(["ssh", *extra, *args], env=child_env, timeout=limit + 15).returncode
    sshpass = shutil.which("sshpass")
    if not sshpass:
        raise ValueError("sshpass is required for this target's configured credential source")
    args = ["BatchMode=no" if item == "BatchMode=yes" else item for item in args]
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, password.encode() + b"\n")
        os.close(write_fd)
        write_fd = None
        return subprocess.run([sshpass, "-d", str(read_fd), "ssh", *extra,
                               "-o", "PubkeyAuthentication=no", "-o", "NumberOfPasswordPrompts=1", *args],
                              pass_fds=(read_fd,), env=child_env, timeout=limit + 15).returncode
    finally:
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, KeyError, subprocess.SubprocessError):
        # Do not echo exceptions: they can contain source lines or credentials.
        print("Bound SSH failed; inspect target binding, credential source and connection status", file=sys.stderr)
        sys.exit(2)
