#!/usr/bin/env python3
"""Client for a bounded NewAPI release. Every remote mutation needs --execute."""
import argparse
import base64
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

from ssh_transport import check_host_key, load_target

HERE = Path(__file__).resolve().parent
HELPERS = ("prepare.py", "cutover.py", "db_snapshot.py")
RELEASE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}")
CHUNK_SIZE = 4 * 1024 * 1024


def digest(data):
    return hashlib.sha256(data).hexdigest()


def release_directory(target, release_id):
    if not RELEASE_ID.fullmatch(release_id):
        raise ValueError("invalid release ID")
    root = Path(target["remote"]["root"])
    if not root.is_absolute() or ".." in root.parts:
        raise ValueError("remote root must be an absolute path")
    return str(root / "releases" / release_id)


def manifest(path):
    raw = (Path(path) / "release.json").read_bytes()
    value = json.loads(raw)
    if value.get("format") != 1 or value.get("database_mode") not in {"unchanged", "usage-tags-v1", "usage-tag-time-index-v1"}:
        raise ValueError("unsupported release format or database mode")
    if value.get("platform") != "linux/amd64" or not RELEASE_ID.fullmatch(value["release_id"]):
        raise ValueError("invalid release target")
    for name in ("binary_sha256", "package_sha256"):
        if not re.fullmatch(r"[a-f0-9]{64}", value[name]):
            raise ValueError("invalid artifact digest")
    return value, raw


class Transport:
    def __init__(self, target):
        self.target = target
        check_host_key(target)

    def env(self, seconds, execute=False):
        target = self.target
        env = dict(os.environ, NEWAPI_DEPLOY_TARGET=target["profile_path"],
                   TARGET_ID=target["id"], TARGET_SSH_HOST=target["host"],
                   TARGET_SSH_USER=target["user"], TARGET_SSH_PORT=str(target.get("port", 22)),
                   TARGET_SSH_KNOWN_HOSTS=target["known_hosts"],
                   TARGET_SSH_BIN=str(HERE / "ssh_transport.py"), TARGET_TIMEOUT_SECONDS=str(seconds))
        env.pop("TARGET_CHANGE_APPROVED", None)
        env.pop("TARGET_SSH_IDENTITY", None)
        if execute:
            env["TARGET_CHANGE_APPROVED"] = "YES"
        if target.get("identity_file"):
            env["TARGET_SSH_IDENTITY"] = target["identity_file"]
        return env

    def shell(self, program, *, execute=False, seconds=60):
        result = subprocess.run(["sh", self.target["ssh_controller"], "apply" if execute else "inspect"],
                                input=program.encode(), capture_output=True,
                                env=self.env(seconds, execute), timeout=seconds + 25)
        if result.returncode:
            raise RuntimeError("remote command failed; inspect release state before any retry")
        return result.stdout

    def python(self, source, *, execute=False, seconds=60):
        return self.shell("set -eu\numask 077\npython3 - <<'NEWAPI_DEPLOY_PY'\n" + source +
                          "\nNEWAPI_DEPLOY_PY\n", execute=execute, seconds=seconds)

    def binary(self, source, data=None, *, seconds=300):
        target = self.target
        args = [str(HERE / "ssh_transport.py"), "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=" + target["known_hosts"],
                "-o", "ConnectTimeout=20", "-p", str(target.get("port", 22))]
        if target.get("identity_file"):
            args += ["-i", target["identity_file"]]
        args += [target["user"] + "@" + target["host"], "python3", "-c", shlex.quote(source)]
        result = subprocess.run(args, input=data, capture_output=True,
                                env=self.env(seconds, data is not None), timeout=seconds + 25)
        if result.returncode:
            raise RuntimeError("artifact transfer failed; inspect completed chunks before retrying stage")
        return result.stdout


def inspect(transport):
    # Deliberately exclude Env, DSNs and complete Compose/inspect output.
    config = json.dumps(transport.target["remote"])
    return transport.python("""
import json, pathlib, subprocess
c = json.loads(%r)
def inspect_one(name):
    return json.loads(subprocess.check_output(['docker','inspect',name]))[0]
result = {'root_exists': pathlib.Path(c['root']).is_dir(), 'containers': {}}
for field in ('app_container','db_container','redis_container','nginx_container'):
    v = inspect_one(c[field])
    result['containers'][field] = {'id':v['Id'],'image_id':v['Image'],
      'image':v['Config']['Image'],'running':v['State']['Running'],
      'health':v['State'].get('Health',{}).get('Status'),
      'mounts':[{'type':m['Type'],'source':m.get('Name',m['Source']),'destination':m['Destination']} for m in v['Mounts']]}
app = inspect_one(c['app_container'])
result['compose_project'] = app['Config']['Labels'].get('com.docker.compose.project')
result['runtime_version'] = subprocess.check_output(['docker','exec',c['app_container'],'wget','-qO-',
  'http://127.0.0.1:'+str(c['app_port'])+'/api/status']).decode()
print(json.dumps(result))
""" % config)


def stage(transport, directory):
    directory = Path(directory).resolve()
    value, release_raw = manifest(directory)
    package = directory / "image-context.tgz"
    if package.stat().st_size != value["package_bytes"] or digest(package.read_bytes()) != value["package_sha256"]:
        raise ValueError("local release package differs from manifest")
    remote = release_directory(transport.target, value["release_id"])
    small = {"release.json": release_raw,
             "target.json": (json.dumps(transport.target["remote"], sort_keys=True, indent=2) + "\n").encode()}
    for name in HELPERS:
        small[name] = (HERE / name).read_bytes()
    tool_hashes = {name: digest(small[name]) for name in HELPERS}
    small["deployment-tools.json"] = (json.dumps(tool_hashes, sort_keys=True) + "\n").encode()
    payload = json.dumps({name: base64.b64encode(data).decode() for name, data in small.items()})
    transport.python("""
import base64, json, os, pathlib
os.umask(0o077)
r=pathlib.Path(%r)
assert r.parent.parent.is_dir() and not r.is_symlink()
r.mkdir(parents=True,exist_ok=True,mode=0o700)
assert r.resolve()==r
payload=json.loads(%r)
for name,data in payload.items():
    p=r/name; raw=base64.b64decode(data)
    assert not p.is_symlink()
    if p.exists():
        assert p.read_bytes()==raw, 'existing staged release differs'
    else:
        with p.open('xb') as f: f.write(raw)
print('release metadata staged')
""" % (remote, payload), execute=True)
    # Complete chunks are immutable and verified. Interrupted partial chunks alone are retransmitted.
    with package.open("rb") as stream:
        index = 0
        while data := stream.read(CHUNK_SIZE):
            name = f"package.part{index:04d}"
            expected = digest(data)
            exists = transport.python("""
import hashlib,pathlib
p=pathlib.Path(%r)/%r
assert not p.is_symlink()
if p.exists():
    assert p.stat().st_size==%d and hashlib.sha256(p.read_bytes()).hexdigest()==%r
    print('complete')
else: print('missing')
""" % (remote, name, len(data), expected)).strip()
            if exists == b"missing":
                transport.binary("""
import hashlib,os,pathlib,sys
os.umask(0o077)
p=pathlib.Path(%r)/%r
assert not p.exists() and not p.is_symlink()
data=sys.stdin.buffer.read(%d)
assert len(data)==%d and hashlib.sha256(data).hexdigest()==%r
partial=p.with_suffix('.partial')
assert not partial.is_symlink()
with partial.open('wb') as f:
    f.write(data); f.flush(); os.fsync(f.fileno())
os.replace(partial,p)
""" % (remote, name, len(data) + 1, len(data), expected), data=data)
            elif exists != b"complete":
                raise RuntimeError("unexpected chunk status")
            index += 1
            print(json.dumps({"stage": "upload", "completed_chunks": index}), flush=True)
    transport.python("""
import hashlib,os,pathlib
r=pathlib.Path(%r); p=r/'image-context.tgz'
assert not p.is_symlink()
if not p.exists():
    tmp=r/'image-context.partial'; assert not tmp.is_symlink()
    with tmp.open('wb') as out:
        for i in range(%d):
            with (r/('package.part%%04d'%%i)).open('rb') as part:
                while chunk:=part.read(1048576): out.write(chunk)
        out.flush(); os.fsync(out.fileno())
    assert tmp.stat().st_size==%d and hashlib.sha256(tmp.read_bytes()).hexdigest()==%r
    os.replace(tmp,p)
assert p.stat().st_size==%d and hashlib.sha256(p.read_bytes()).hexdigest()==%r
print('package verified')
""" % (remote, index, value["package_bytes"], value["package_sha256"],
          value["package_bytes"], value["package_sha256"]), execute=True, seconds=120)
    return {"release_id": value["release_id"], "remote_directory": remote, "staged": True}


def run_phase(transport, release_id, phase):
    remote = release_directory(transport.target, release_id)
    script = {"prepare": "prepare.py", "cutover": "cutover.py"}[phase]
    command = shlex.join(["python3", remote + "/" + script, "--release-dir", remote, "--apply"])
    # Verify uploaded helpers before executing; don't run mutable code from a different release.
    verify_tools = """
import hashlib,json,pathlib
r=pathlib.Path(%r)
for name,expected in json.loads((r/'deployment-tools.json').read_text()).items():
    assert name in %r and hashlib.sha256((r/name).read_bytes()).hexdigest()==expected
""" % (remote, HELPERS)
    transport.python(verify_tools)
    return transport.shell("set -eu\numask 077\n" + command + "\n", execute=True, seconds=1800)


def status(transport, release_id):
    remote = release_directory(transport.target, release_id)
    return transport.python("""
import json,pathlib,subprocess
r=pathlib.Path(%r); c=json.loads((r/'target.json').read_text()); m=json.loads((r/'release.json').read_text())
b=pathlib.Path(c['root'])/'backups'/m['release_id']
out={'release_id':m['release_id'],'commit':m['commit'],'version':m['version']}
for label,p in [('prepare',r/'prepare-state.json'),('cutover',b/'cutover-state.json')]:
    if p.exists():
        state=json.loads(p.read_text())
        out[label]={k:state[k] for k in ('phase','status','stage','release','release_id','failed_recovery_steps','ingress_kept_closed','database_restored') if k in state}
v=json.loads(subprocess.check_output(['docker','inspect',c['app_container']]))[0]
out['application']={'image_id':v['Image'],'running':v['State']['Running'],'health':v['State'].get('Health',{}).get('Status')}
print(json.dumps(out))
""" % remote)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def get_public(url, *, api_key=None):
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError("public verification requires HTTPS")
    headers = {"User-Agent": "Mozilla/5.0 NewAPI-Deploy-Verification", "Accept-Encoding": "gzip"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.build_opener(NoRedirect).open(request, timeout=60) as response:
        content = response.read(16 * 1024 * 1024 + 1)
        if len(content) > 16 * 1024 * 1024:
            raise ValueError("verification response exceeds limit")
        if response.headers.get("Content-Encoding") == "gzip":
            with gzip.GzipFile(fileobj=io.BytesIO(content)) as compressed:
                content = compressed.read(16 * 1024 * 1024 + 1)
            if len(content) > 16 * 1024 * 1024:
                raise ValueError("decompressed verification response exceeds limit")
        return content


def verify(transport, directory, api_key_file=None):
    value, raw = manifest(directory)
    remote = release_directory(transport.target, value["release_id"])
    runtime = json.loads(transport.python("""
import hashlib,json,pathlib,subprocess
r=pathlib.Path(%r)
assert hashlib.sha256((r/'release.json').read_bytes()).hexdigest()==%r
p=json.loads((r/'prepare.json').read_text()); c=json.loads((r/'target.json').read_text())
v=json.loads(subprocess.check_output(['docker','inspect',c['app_container']]))[0]
assert v['Image']==p['new_image_id'] and v['State']['Running'] and v['State'].get('Health',{}).get('Status')=='healthy'
b=pathlib.Path(c['root'])/'backups'/json.loads((r/'release.json').read_text())['release_id']
assert json.loads((b/'cutover-state.json').read_text())['phase']=='success'
print(json.dumps({'image_id':v['Image'],'healthy':True}))
""" % (remote, digest(raw))))
    base = transport.target["remote"]["public_url"].rstrip("/")
    public = json.loads(get_public(base + "/api/status"))
    if not public.get("success") or public.get("data", {}).get("version") != value["version"]:
        raise ValueError("public runtime version differs from release")
    homepage_bytes = get_public(base + "/")
    homepage = homepage_bytes.decode()
    verified_assets = 0
    for asset in value["frontend_assets"]:
        path = asset["path"]
        if path.startswith("/") or ".." in Path(path).parts or ":" in path:
            raise ValueError("invalid asset path")
        if path == "index.html":
            # The server may inject analytics into HTML (main.go). Verify the
            # referenced immutable JS/CSS, not a runtime-modified HTML hash.
            continue
        elif path in homepage:
            observed = digest(get_public(base + "/" + path))
        else:
            raise ValueError("public homepage does not reference the expected asset")
        if observed != asset["sha256"]:
            raise ValueError("public frontend differs from release")
        verified_assets += 1
    if not verified_assets:
        raise ValueError("release has no immutable frontend entry assets")
    models = None
    if api_key_file:
        key_path = Path(api_key_file).expanduser()
        if key_path.stat().st_mode & 0o077:
            raise ValueError("verification key file must be private")
        key = key_path.read_text().strip()
        if not key or any(c.isspace() for c in key):
            raise ValueError("invalid verification key file")
        result = json.loads(get_public(base + "/v1/models", api_key=key))
        if not isinstance(result.get("data"), list):
            raise ValueError("existing-key model catalog verification failed")
        models = len(result["data"])
    result = {"release_id": value["release_id"], "commit": value["commit"], "version": value["version"],
              **runtime, "public_version_verified": True, "frontend_assets_verified": verified_assets,
              "existing_key_model_count": models, "paid_provider_requests": 0}
    output = Path(directory) / "verification.json"
    output.write_text(json.dumps(result, indent=2) + "\n")
    output.chmod(0o600)
    return result


def collect(transport, directory):
    value, raw = manifest(directory)
    remote = release_directory(transport.target, value["release_id"])
    info = json.loads(transport.python("""
import hashlib,json,os,pathlib,tarfile
os.umask(0o077)
r=pathlib.Path(%r); c=json.loads((r/'target.json').read_text()); m=json.loads((r/'release.json').read_text())
b=pathlib.Path(c['root'])/'backups'/m['release_id']; state=json.loads((b/'cutover-state.json').read_text())
assert state['phase'] in ('success','rolled_back')
prepared=json.loads((r/'prepare.json').read_text())
names=['production-final.dump','production-final.dump.sha256','postgres-globals.sql',
       'docker-compose.yml.original','nginx.conf.original','containers.original.json',
       'application-runtime-before.json','production-before.json','cutover-state.json',
       'application-final.tgz.sha256']
if state['phase']=='success' or (b/'production-after.json').exists(): names.append('production-after.json')
if prepared['env_file_present']: names.append('.env.original')
if m['database_mode'] in ('usage-tags-v1','usage-tag-time-index-v1'):
    assert prepared.get('rollback_rehearsal_passed') is True, 'rollback rehearsal evidence missing'
    names.extend(['production-initial.dump','rehearsal-before.json','rehearsal-after-1.json',
                  'rehearsal-after-2.json','rehearsal-rollback.json','rehearsal-comparison.json'])
required={name:b/name for name in names}
required.update({name:r/name for name in ('release.json','prepare.json','target.json','deployment-tools.json')})
for source in [*required.values(),b/'application-final.tgz']:
    assert source.is_file() and not source.is_symlink() and source.stat().st_size>0, 'required recovery evidence missing'
for name in ('production-final.dump','application-final.tgz'):
    recorded=(b/(name+'.sha256')).read_text().split()
    assert len(recorded)==2 and recorded[1]==name
    assert hashlib.sha256((b/name).read_bytes()).hexdigest()==recorded[0], 'backup checksum mismatch'
assert hashlib.sha256((b/'docker-compose.yml.original').read_bytes()).hexdigest()==prepared['config_sha256']
assert hashlib.sha256((b/'nginx.conf.original').read_bytes()).hexdigest()==prepared['nginx_sha256']
if prepared['env_file_present']:
    assert hashlib.sha256((b/'.env.original').read_bytes()).hexdigest()==prepared['env_sha256']
data_names=[]
with tarfile.open(b/'application-final.tgz') as app:
    for entry in app:
        if entry.name=='data' or entry.name.startswith('data/'):
            assert '..' not in pathlib.PurePosixPath(entry.name).parts and not entry.name.startswith('/')
            data_names.append(entry.name)
assert data_names and len(data_names)==len(set(data_names)), 'application data absent or ambiguous'
p=b/'recovery-data.tgz'; assert not p.is_symlink()
if not p.exists():
    tmp=b/'recovery-data.partial'; assert not tmp.is_symlink()
    with tarfile.open(tmp,'w:gz') as out:
        for name,source in required.items(): out.add(source,arcname=name,recursive=False)
        with tarfile.open(b/'application-final.tgz') as app:
            for entry in app:
                if entry.name in data_names:
                    out.addfile(entry,app.extractfile(entry) if entry.isfile() else None)
    os.replace(tmp,p)
with tarfile.open(p) as archive:
    members=archive.getmembers(); actual=[entry.name for entry in members]
    assert len(actual)==len(set(actual)) and set(actual)==set(required)|set(data_names), 'incomplete recovery archive'
    for name,source in required.items():
        entry=archive.getmember(name)
        assert entry.isfile() and hashlib.sha256(archive.extractfile(entry).read()).hexdigest()==hashlib.sha256(source.read_bytes()).hexdigest()
    with tarfile.open(b/'application-final.tgz') as app:
        for name in data_names:
            old,new=app.getmember(name),archive.getmember(name)
            assert old.type==new.type and old.linkname==new.linkname and old.size==new.size
            if old.isfile(): assert hashlib.sha256(app.extractfile(old).read()).digest()==hashlib.sha256(archive.extractfile(new).read()).digest()
print(json.dumps({'path':str(p),'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}))
""" % remote, execute=True, seconds=180))
    destination = Path(directory) / "recovery-data.tgz"
    if destination.exists():
        if digest(destination.read_bytes()) != info["sha256"]:
            raise ValueError("local recovery archive differs")
    else:
        partial = destination.with_suffix(".partial")
        if partial.is_symlink():
            raise ValueError("invalid local recovery path")
        with partial.open("wb") as output:
            os.fchmod(output.fileno(), 0o600)
            for offset in range(0, info["bytes"], CHUNK_SIZE):
                size = min(CHUNK_SIZE, info["bytes"] - offset)
                data = transport.binary("import sys\nwith open(%r,'rb') as f:\n f.seek(%d); sys.stdout.buffer.write(f.read(%d))" %
                                        (info["path"], offset, size))
                if len(data) != size:
                    raise ValueError("recovery archive download incomplete")
                output.write(data)
            output.flush()
            os.fsync(output.fileno())
        if digest(partial.read_bytes()) != info["sha256"]:
            raise ValueError("recovery archive checksum mismatch")
        os.replace(partial, destination)
    summary = {"release_id": value["release_id"], "commit": value["commit"],
               "recovery_archive": str(destination.resolve()), "sha256": info["sha256"],
               "remote_backup": str(Path(transport.target["remote"]["root"]) / "backups" / value["release_id"]),
               "logs": "retained in remote application-final.tgz"}
    (Path(directory) / "recovery.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("inspect", "stage", "prepare", "cutover", "status", "verify", "collect"))
    parser.add_argument("--target", required=True, help="private local target JSON")
    parser.add_argument("--release", help="local build release directory")
    parser.add_argument("--release-id", help="remote release identifier for status/prepare/cutover")
    parser.add_argument("--api-key-file", help="optional private existing API key for nonbillable /v1/models verification")
    parser.add_argument("--execute", action="store_true", help="execute mutations covered by the user's deployment request")
    args = parser.parse_args()
    if args.command in ("stage", "prepare", "cutover", "collect") and not args.execute:
        parser.error("remote mutation requires --execute under an authorized deployment request")
    if args.command in ("stage", "verify", "collect") and not args.release:
        parser.error("this command requires --release")
    release_id = args.release_id
    if args.release:
        value, _ = manifest(args.release)
        if release_id and release_id != value["release_id"]:
            parser.error("release IDs differ")
        release_id = value["release_id"]
    if args.command in ("prepare", "cutover", "status") and not release_id:
        parser.error("this command requires --release or --release-id")
    target = load_target(args.target)
    transport = Transport(target)
    if args.command == "inspect": result = inspect(transport)
    elif args.command == "stage": result = stage(transport, args.release)
    elif args.command in ("prepare", "cutover"): result = run_phase(transport, release_id, args.command)
    elif args.command == "status": result = status(transport, release_id)
    elif args.command == "verify": result = verify(transport, args.release, args.api_key_file)
    else: result = collect(transport, args.release)
    print(result.decode() if isinstance(result, bytes) else json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    os.umask(0o077)
    try:
        main()
    except (ValueError, RuntimeError, OSError, KeyError, subprocess.SubprocessError, urllib.error.URLError) as error:
        # Only our own bounded errors are printable; network/system exception text can contain URLs or credentials.
        safe = str(error) if type(error) in (ValueError, RuntimeError) else type(error).__name__
        print("Deployment stopped: " + safe, file=sys.stderr)
        sys.exit(1)
