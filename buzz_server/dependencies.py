"""Pinned, optional private tools. No vendor installers or ambient executable search."""
import gzip
import hashlib
import os
from pathlib import Path, PurePosixPath
import platform
import re
import resource
import shutil
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import zlib

from .errors import Error
from .storage import private_dir

SYSTEM_PATH = '/usr/local/bin:/usr/bin:/bin'
NAMES = ('buzz', 'buzz-acp', 'goose')
ROLES = {'buzz': 'headless profile updates', 'buzz-acp': 'Buzz to ACP adapter', 'goose': 'inference harness; configure separately'}
ACP_FLAGS = ('--system-prompt-file', '--agent-command', '--agent-args', '--model', '--permission-mode', '--respond-to', '--session-title', '--config')
PACKAGES = (
    dict(name='buzz', version='desktop-v0.5.30',
         url='https://github.com/block/buzz/releases/download/desktop-v0.5.30/Buzz_0.5.30_amd64.deb',
         size=120825052, sha256='1834e4db989637a800e481d48ea2d0a743d31d62bcb9d0361ca8d253828324d6',
         members={'usr/bin/buzz': ('buzz', 18855512), 'usr/bin/buzz-acp': ('buzz-acp', 18202248)}),
    dict(name='goose', version='v1.60.2',
         url='https://github.com/aaif-goose/goose/releases/download/v1.60.2/goose-x86_64-unknown-linux-gnu.tar.gz',
         size=63566822, sha256='5f6e463621a8428617ae4870eecee7a00a253e9bbfccb6340dfaa4f0ec2aaf32',
         members={'./goose': ('goose', 213003128)}),
)
MAX_TAR = 512 * 1024 * 1024
STAGE_PREFIX = '.stage-'
FAILURE = 'Prerequisite acquisition refused. Check platform, network, pinned upstream files and private permissions; no raw remote diagnostics are returned.'


def refused():
    raise Error('dependency_refused', FAILURE)


def bin_dir(root, create=False):
    root = Path(root).absolute()
    directory = root / 'tools/bin'
    if os.pathsep in str(directory):
        refused()
    if create:
        private_dir(root)
        private_dir(root / 'tools')
        private_dir(directory)
    # lstat even dangling links; reject every component before lookup or PATH use.
    for p in (root, *root.parents, root / 'tools', directory):
        if p.is_symlink():
            refused()
    for p in (root, root / 'tools', directory):
        if p.exists():
            s = p.lstat()
            if not stat.S_ISDIR(s.st_mode) or s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) & 0o077:
                refused()
    if directory.exists():
        for p in directory.iterdir():
            s = p.lstat()
            if (p.name not in NAMES or not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid()
                    or stat.S_IMODE(s.st_mode) != 0o700):
                refused()
    return directory


def resolve(name, root=None):
    if name not in NAMES:
        refused()
    path = shutil.which(name, path=SYSTEM_PATH)
    if path:
        return str(Path(path).resolve())
    if root is not None:
        path = bin_dir(root) / name
        if path.exists():
            return str(path)
    return None


def paths(root):
    return {name: resolve(name, root) for name in NAMES}


def child_path(root):
    directory = bin_dir(root)
    return SYSTEM_PATH + (':' + str(directory) if directory.exists() else '')


def supported():
    try:
        info = platform.freedesktop_os_release()
    except OSError:
        refused()
    if sys.platform != 'linux' or platform.machine() != 'x86_64' or info.get('ID') != 'ubuntu' or info.get('VERSION_ID') != '24.04':
        raise Error('dependency_platform', 'Managed downloads support Ubuntu 24.04 amd64 only. Install compatible upstream tools manually on other Linux hosts.')
    if os.getuid() == 0:
        raise Error('root_denied', 'Run prerequisite installation as an unprivileged user.')


class Redirect(urllib.request.HTTPRedirectHandler):
    def __init__(self):
        super().__init__()
        self.count = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.count += 1
        parsed = urllib.parse.urlsplit(newurl)
        if (self.count > 4 or parsed.scheme != 'https' or parsed.hostname not in
                ('github.com', 'release-assets.githubusercontent.com') or parsed.port not in (None, 443)
                or parsed.username or parsed.password or parsed.fragment):
            refused()
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(package, target):
    # Fixed public URL, fixed OS trust roots, no proxy, netrc, auth or caller TLS overrides.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile='/etc/ssl/certs/ca-certificates.crt')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), Redirect(), urllib.request.HTTPSHandler(context=context))
    deadline = time.monotonic() + 180
    total = 0
    with opener.open(package['url'], timeout=10) as response, target.open('xb') as out:
        while True:
            if time.monotonic() > deadline:
                refused()
            block = response.read1(min(65536, package['size'] + 1 - total))
            if not block:
                break
            total += len(block)
            if total > package['size']:
                refused()
            out.write(block)
        out.flush()
        os.fsync(out.fileno())
    verify(target, package)


def verify(path, package):
    if path.stat().st_size != package['size']:
        refused()
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(65536), b''):
            digest.update(block)
    if digest.hexdigest() != package['sha256']:
        refused()


def limits(size):
    resource.setrlimit(resource.RLIMIT_FSIZE, (size, size))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))


def extract(path, members, stage):
    # Parse bounded tar records directly: no path extraction, archive link following,
    # or unbounded tarfile extended-header allocations. Ignore ordinary extra files
    # and directories; metadata extensions and links fail closed.
    stage.mkdir(mode=0o700, exist_ok=True)
    seen = set()
    count = 0
    deadline = time.monotonic() + 60
    def read(stream, size):
        nonlocal count
        if count + size > MAX_TAR or time.monotonic() > deadline:
            refused()
        value = stream.read(size)
        count += len(value)
        if len(value) != size:
            refused()
        return value
    with path.open('rb') as raw:
        with (gzip.GzipFile(fileobj=raw) if path.suffix == '.gz' else raw) as stream:
            while True:
                header = read(stream, 512)
                if header == bytes(512):
                    if read(stream, 512) != bytes(512):
                        refused()
                    break
                try:
                    member = tarfile.TarInfo.frombuf(header, 'utf-8', 'strict')
                except (tarfile.HeaderError, UnicodeError):
                    refused()
                if (member.size < 0 or member.size > MAX_TAR or member.issparse() or
                        member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE) or
                        PurePosixPath(member.name).is_absolute() or '..' in PurePosixPath(member.name).parts):
                    refused()
                selected = members.get(member.name)
                if selected:
                    name, size = selected
                    if not member.isreg() or member.size != size or name in seen:
                        refused()
                    seen.add(name)
                    with (stage / name).open('xb') as out:
                        remaining = size
                        while remaining:
                            block = read(stream, min(65536, remaining))
                            out.write(block)
                            remaining -= len(block)
                        out.flush()
                        os.fsync(out.fileno())
                    (stage / name).chmod(0o700)
                else:
                    remaining = member.size
                    while remaining:
                        block = read(stream, min(65536, remaining))
                        remaining -= len(block)
                read(stream, (-member.size) % 512)
    if seen != {name for name, _ in members.values()}:
        refused()


def probe(path, argv, flags, home):
    with tempfile.TemporaryFile(dir=home) as out:
        try:
            result = subprocess.run([str(path), *argv], stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.DEVNULL, timeout=10, cwd=home, umask=0o077,
                                    env={'HOME': str(home), 'PATH': SYSTEM_PATH, 'LANG': 'C.UTF-8',
                                         'GOOSE_DISABLE_KEYRING': '1', 'XDG_CONFIG_HOME': str(home / '.config'),
                                         'XDG_DATA_HOME': str(home / '.local/share')},
                                    preexec_fn=lambda: limits(262144))
        except subprocess.SubprocessError:
            raise Error('dependency_refused', FAILURE) from None
        out.seek(0)
        data = out.read(262144)
    if result.returncode or any(flag.encode() not in data for flag in flags):
        refused()


def acquire(package, stage):
    asset = stage / ('asset.deb' if package['name'] == 'buzz' else 'asset.tar.gz')
    download(package, asset)
    # Defense in depth before invoking even the fixed OS archive reader.
    verify(asset, package)
    archive = asset
    if package['name'] == 'buzz':
        archive = stage / 'payload.tar'
        with archive.open('xb') as out:
            result = subprocess.run(['/usr/bin/dpkg-deb', '--fsys-tarfile', str(asset)],
                                    stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.DEVNULL,
                                    timeout=60, env={'PATH': SYSTEM_PATH, 'LANG': 'C.UTF-8'},
                                    preexec_fn=lambda: limits(MAX_TAR))
        if result.returncode:
            refused()
    extract(archive, package['members'], stage)
    home = private_dir(stage / 'probe-home')
    if package['name'] == 'buzz':
        probe(stage / 'buzz', ['users', 'set-profile', '--help'], ['--name', '--about', '--avatar'], home)
        probe(stage / 'buzz-acp', ['--help'], ACP_FLAGS, home)
    else:
        probe(stage / 'goose', ['acp', '--help'], [], home)
        probe(stage / 'goose', ['configure', '--help'], [], home)
    for name, size in package['members'].values():
        if (stage / name).stat().st_size != size:
            refused()


def interactive():
    return all(s.isatty() for s in (sys.stdin, sys.stdout, sys.stderr))


def clear_abandoned_stages(tools):
    # Only called inside the Store lock. Every live stage is created and used
    # under that same lock. Restrict cleanup to reserved private stage dirs.
    if not shutil.rmtree.avoids_symlink_attacks:
        refused()
    fd = os.open(tools, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in os.listdir(fd):
            if not re.fullmatch(re.escape(STAGE_PREFIX) + r'[a-z0-9_]{8}', name):
                continue
            entry = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(entry.st_mode) and entry.st_uid == os.getuid() and stat.S_IMODE(entry.st_mode) == 0o700:
                shutil.rmtree(name, dir_fd=fd)
    finally:
        os.close(fd)


def install(store, *, opted, skip, structured):
    installed = []
    failure = None
    # Called only after release.install has released its prefix lock. Never take
    # a prefix lock while holding Store.lock (ordinary CLI uses the reverse order).
    with store.lock():
        current = paths(store.root)
    approved = NAMES if opted else tuple(n for n in NAMES if current[n] is None)
    if approved and not skip and not opted and not structured and interactive():
        print('Runtime tools (presence does not prove readiness):', file=sys.stderr)
        for name in NAMES:
            package = next(p for p in PACKAGES if any(n == name for n, _ in p['members'].values()))
            print(f"  {name}: {current[name] or 'missing'}; {ROLES[name]}", file=sys.stderr)
            if not current[name]:
                print(f"    planned: {package['url']} ({package['version']}) -> {store.root / 'tools/bin' / name}", file=sys.stderr)
        print('Private downloads only; no sudo, desktop installation, services or agent changes. Managed downloads require Ubuntu 24.04 amd64.', file=sys.stderr)
        try:
            opted = input('Install missing tools? [y/N] ').strip().lower() in ('y', 'yes')
        except EOFError:
            opted = False
    if opted and not skip:
        with store.lock():
            # Nothing new becomes authorized while we wait on the prompt.
            current = paths(store.root)
            missing = [n for n in approved if current[n] is None]
            try:
                directory = bin_dir(store.root)
                if directory.parent.is_dir():
                    clear_abandoned_stages(directory.parent)
                if missing:
                    supported()
                    directory = bin_dir(store.root, create=True)
                    # Validate every necessary package before any binary becomes visible.
                    with tempfile.TemporaryDirectory(prefix=STAGE_PREFIX, dir=directory.parent) as temp:
                        staged = Path(temp)
                        for package in PACKAGES:
                            if any(name in missing for name, _ in package['members'].values()):
                                sub = private_dir(staged / package['name'])
                                acquire(package, sub)
                        for name in missing:
                            package = next(p for p in PACKAGES if any(n == name for n, _ in p['members'].values()))
                            source = staged / package['name'] / name
                            # link is atomic and never replaces an existing executable.
                            os.link(source, directory / name)
                            source.unlink()
                            installed.append(name)
            except (Error, OSError, ValueError, EOFError, tarfile.TarError, zlib.error, subprocess.SubprocessError):
                failure = FAILURE + ' Buzz Server remains installed. Retry install --install-dependencies with the same prefix/state/unit directories; valid tools are retained.'
            current = paths(store.root)
    return {'runtime_tool_paths': current, 'missing_runtime_tools': [n for n in NAMES if not current[n]],
            'dependencies_installed': installed, 'dependency_error': failure}
