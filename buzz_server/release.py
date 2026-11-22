"""Offline packaging and prefix-local releases; GitHub is the sole update authority."""
import ast
import contextlib
import fcntl
import hashlib
import io
import json
import multiprocessing
import os
from pathlib import Path
import re
import ssl
import stat
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile

from .errors import Error
from .storage import Store, agent_id, atomic, private_dir, read_json, read_private, write_json
from .supervisor import Systemd

ASSET = 'buzz-server.pyz'
REPO = 'repos/j2-ms/buzz-server'
MAX_SIZE = 8 * 1024 * 1024
DAY = 86400


def denied():
    raise Error('release_refused', 'Release operation refused: check the private prefix, managed files, stable version, public GitHub release availability and release checksums. No raw remote output is returned.')


def version(tag):
    if not isinstance(tag, str) or not re.fullmatch(r'v(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})', tag):
        denied()
    return tuple(int(x) for x in tag[1:].split('.'))


def build(source, target, number):
    version('v' + number)
    source, target = Path(source), Path(target)
    files = {str(p.relative_to(source)): p.read_bytes() for p in sorted((source / 'buzz_server').glob('*.py'))}
    files['buzz_server/__init__.py'] = ('"""Standalone headless Buzz agent management."""\n__version__ = ' + repr(number) + '\n').encode()
    files['__main__.py'] = (source / 'entry.py').read_bytes()
    files['VERSION'] = number.encode() + b'\n'
    # Fixed timestamps, modes, ordering and no compression-library variability.
    with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_STORED) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100600 << 16
            archive.writestr(info, data)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_name(target.name + '.sha256').write_text(digest + '  ' + target.name + '\n', encoding='ascii')
    return digest


def inspect_artifact(path):
    try:
        path = Path(path)
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_SIZE:
            denied()
        data = path.read_bytes()
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or len(names) > 64:
                denied()
            if any(not re.fullmatch(r'(VERSION|__main__\.py|buzz_server/[a-z_]+\.py)', n) for n in names):
                denied()
            if sum(i.file_size for i in archive.infolist()) > MAX_SIZE:
                denied()
            number = archive.read('VERSION').decode('ascii').strip()
            version('v' + number)
            tree = ast.parse(archive.read('buzz_server/__init__.py'))
            values = [ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign) and
                      any(isinstance(t, ast.Name) and t.id == '__version__' for t in n.targets)]
            if values != [number] or '__main__.py' not in names or archive.testzip() is not None:
                denied()
        return number, hashlib.sha256(data).hexdigest(), data
    except (OSError, ValueError, KeyError, SyntaxError, UnicodeError, zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError):
        denied()


def location():
    """Only immutable installed zipapps identify an installation; no env override."""
    path = Path(sys.argv[0]).absolute()
    if path.name == 'app.pyz' and path.parent.parent.name == 'payloads':
        return path.parent.parent.parent
    return None


def launcher(prefix):
    # Resolve once, before Python opens the archive. Workers always reopen the
    # same immutable path even if active changes while zipimport is in progress.
    return ("#!/usr/bin/python3 -I\nimport os\nfrom pathlib import Path\n"
            f"p = (Path({str(prefix)!r}) / 'active' / 'app.pyz').resolve(strict=True)\n"
            "os.execv('/usr/bin/python3', ['/usr/bin/python3', '-I', str(p), *__import__('sys').argv[1:]])\n").encode()


def manifest(prefix):
    m = read_json(prefix / 'installation.json')
    if (not isinstance(m, dict) or set(m) != {'schema', 'version', 'state', 'units', 'payloads', 'auto_update', 'installed'} or
            m['schema'] != 1 or type(m['auto_update']) is not bool or type(m['installed']) is not bool or
            not isinstance(m['payloads'], list) or len(m['payloads']) != len(set(m['payloads']))):
        denied()
    version('v' + m['version'])
    if any(not isinstance(p, str) or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+-[0-9a-f]{64}', p) for p in m['payloads']):
        denied()
    for key in ('state', 'units'):
        if not isinstance(m[key], str) or not Path(m[key]).is_absolute():
            denied()
    # The active link is the commit point. A process killed after writing the
    # inventory but before switching the link must still see the old version.
    if m['installed']:
        active = prefix / 'active'
        if not active.is_symlink():
            denied()
        link = str(active.readlink())
        valid = {'payloads/' + p: p.split('-', 1)[0] for p in m['payloads']}
        if link not in valid:
            denied()
        m['version'] = valid[link]
    return m


def inventory(prefix, m):
    """Preflight everything we may replace/remove, before making changes."""
    allowed = {'.release.lock', 'installation.json', 'bin', 'payloads', 'active', 'update-cache.json', '.probe.lock', '.activate'}
    if any(p.name not in allowed for p in prefix.iterdir()):
        denied()
    for name in ('.release.lock', '.probe.lock', 'installation.json', 'update-cache.json'):
        p = prefix / name
        if p.exists() or p.is_symlink():
            read_private(p)
    for name in ('bin', 'payloads'):
        p = prefix / name
        if p.exists() or p.is_symlink():
            private_dir(p)
    binary = prefix / 'bin/buzz-server'
    if (prefix / 'bin').exists() and any(p.name != 'buzz-server' for p in (prefix / 'bin').iterdir()):
        denied()
    if binary.exists() or binary.is_symlink():
        if read_private(binary) != launcher(prefix):
            denied()
    actual = set()
    if (prefix / 'payloads').exists():
        for p in (prefix / 'payloads').iterdir():
            if p.name not in m['payloads']:
                denied()
            private_dir(p)
            if {c.name for c in p.iterdir()} != {'app.pyz'}:
                denied()
            data = read_private(p / 'app.pyz', MAX_SIZE)
            if hashlib.sha256(data).hexdigest() != p.name.split('-', 1)[1]:
                denied()
            actual.add(p.name)
    if actual != set(m['payloads']):
        denied()
    staged = prefix / '.activate'
    if staged.exists() or staged.is_symlink():
        if not staged.is_symlink() or str(staged.readlink()) not in {'payloads/' + p for p in actual}:
            denied()
    active = prefix / 'active'
    if m['installed']:
        if not active.is_symlink() or str(active.readlink()) not in {'payloads/' + p for p in actual}:
            denied()
        if active.resolve().name.split('-', 1)[0] != m['version'] or not binary.exists():
            denied()
    elif active.exists() or active.is_symlink():
        denied()


@contextlib.contextmanager
def locked(prefix, *, shared=False, fresh=False, wait=10):
    prefix = Path(prefix).absolute()
    # Existing foreign prefixes are rejected before even creating a lock file.
    if prefix.exists() or prefix.is_symlink():
        private_dir(prefix)
        if not (prefix / 'installation.json').exists() and any(prefix.iterdir()):
            denied()
    elif not fresh:
        denied()
    else:
        private_dir(prefix)
    fd = os.open(prefix / '.release.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        s = os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) & 0o077:
            denied()
        end = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    raise Error('release_busy', 'Another installation or lifecycle operation is in progress; retry later.')
                time.sleep(0.05)
        yield prefix
    finally:
        os.close(fd)


def user_command(prefix, *, installing=False):
    """Return the managed standard command; never follow or replace a foreign one."""
    home = Path.home().absolute()
    if prefix != home / '.local/buzz-server-cli':
        return None
    for directory in (home, home / '.local', home / '.local/bin'):
        if installing and not directory.exists() and not directory.is_symlink():
            directory.mkdir(mode=0o700, exist_ok=True)
        try:
            info = directory.lstat()
        except FileNotFoundError:
            if not installing:
                return None
            raise
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or
                stat.S_IMODE(info.st_mode) & 0o022):
            if not installing:
                return None
            raise Error('release_refused', 'The standard user bin path must be owned by you, be a real directory and not be group- or world-writable.')
    command = home / '.local/bin/buzz-server'
    try:
        info = command.lstat()
    except FileNotFoundError:
        return command if installing else None
    if not stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid() or os.readlink(command) != str(prefix / 'bin/buzz-server'):
        if not installing:
            return None
        raise Error('release_refused', 'The standard user command already exists and is not this installation’s managed link; move it aside and retry.')
    return command


def _install(artifact, prefix, state, units):
    number, digest, data = inspect_artifact(artifact)
    state, units = str(Path(state).absolute()), str(Path(units).absolute())
    existing = (prefix / 'installation.json').exists()
    if existing:
        m = manifest(prefix)
        inventory(prefix, m)
        if (m['state'], m['units']) != (state, units):
            denied()
        # A completed uninstall permits deliberate recovery from a verified
        # older artifact. Never downgrade an active installation.
        if m['installed'] and version('v' + number) < version('v' + m['version']):
            denied()
        (prefix / '.activate').unlink(missing_ok=True)
    else:
        m = dict(schema=1, version=number, state=state, units=units, payloads=[], auto_update=True, installed=False)
    # These locations must never fall inside installation files or vice versa.
    for p in (Path(state), Path(units)):
        if p == prefix or prefix in p.parents or p in prefix.parents:
            denied()
    command = user_command(prefix, installing=True)
    name = number + '-' + digest
    payload = prefix / 'payloads' / name
    old = dict(m, payloads=list(m['payloads']))
    created = name not in m['payloads']
    command_created = False
    try:
        if command is not None and not command.is_symlink():
            command.symlink_to(prefix / 'bin/buzz-server')
            command_created = True
        if created:
            private_dir(prefix / 'payloads')
            private_dir(payload)
            atomic(payload / 'app.pyz', data)
            m['payloads'].append(name)
        private_dir(prefix / 'bin')
        binary = prefix / 'bin/buzz-server'
        if not binary.exists():
            atomic(binary, launcher(prefix))
            binary.chmod(0o700)
        # Check the installed bytes before the active-pointer commit, so a
        # failed write/verification preserves the working CLI.
        stored_number, stored_digest, stored_data = inspect_artifact(payload / 'app.pyz')
        if (stored_number, stored_digest, stored_data) != (number, digest, data):
            denied()
        m.update(version=number, installed=True)
        write_json(prefix / 'installation.json', m)
        # The single commit point. Never replace or remove an old archive.
        link = prefix / '.activate'
        if link.exists() or link.is_symlink():
            denied()
        link.symlink_to('payloads/' + name)
        os.replace(link, prefix / 'active')
    except Exception:
        if command_created and command.is_symlink() and os.readlink(command) == str(prefix / 'bin/buzz-server'):
            command.unlink()
        (prefix / '.activate').unlink(missing_ok=True)
        if existing:
            write_json(prefix / 'installation.json', old)
        else:
            # Retain a managed, reinstallable tombstone after a partial first install.
            write_json(prefix / 'installation.json', dict(old, installed=False))
        if created and payload.exists():
            (payload / 'app.pyz').unlink(missing_ok=True)
            payload.rmdir()
        raise
    return {'version': number, 'prefix': str(prefix), 'auto_update': m['auto_update'], 'workers_restarted': False}


def install(artifact, prefix, state, units):
    # Invalid artifacts must not leave even an empty installation behind.
    inspect_artifact(artifact)
    prefix = Path(prefix).absolute()
    for p in (prefix, Path(state).absolute(), Path(units).absolute()):
        if '..' in p.parts or any(part.is_symlink() for part in (p, *p.parents)):
            denied()
    for p in (Path(state).absolute(), Path(units).absolute()):
        if p == prefix or prefix in p.parents or p in prefix.parents:
            denied()
    # Create the standard parent with a safe mode even under a permissive umask.
    if prefix == Path.home().absolute() / ".local/buzz-server-cli":
        user_command(prefix, installing=True)
    with locked(prefix, fresh=True) as prefix:
        return _install(artifact, prefix, state, units)


def uninstall(prefix):
    with locked(prefix) as prefix:
        m = manifest(prefix)
        inventory(prefix, m)
        store = Store(m['state'])
        supervisor = Systemd(store, m['units'])
        with store.lock():
            units = Path(m['units'])
            paths = list(units.glob('buzz-server-*.service')) if units.exists() else []
            docs = {ident: store.load(ident) for ident in store.ids()}
            ids = set(docs)
            unit_ids = {agent_id(p.name[len('buzz-server-'):-len('.service')]) for p in paths}
            # Every known unit must pass both on-disk and loaded ownership checks
            # before any service or desired-state mutation.
            for ident in sorted(ids | unit_ids):
                if ident in unit_ids:
                    supervisor.read_owned(ident)
                    supervisor.inspect(ident)
                else:
                    s = supervisor.inspect(ident, missing=True)
                    if s.get('LoadState') != 'not-found':
                        denied()
            for ident in sorted(unit_ids):
                supervisor.stop(ident)
            for ident in sorted(ids):
                doc = docs[ident]
                doc['desired'] = 'stopped'
                store.save(doc)
            for ident in sorted(unit_ids):
                supervisor.remove(ident)
            # Stop + disable + cgroup verification happen in Systemd.stop/remove.
            if (command := user_command(prefix)) is not None:
                command.unlink()
            (prefix / '.activate').unlink(missing_ok=True)
            (prefix / 'active').unlink(missing_ok=True)
            (prefix / 'bin/buzz-server').unlink(missing_ok=True)
            for name in m['payloads']:
                p = prefix / 'payloads' / name
                (p / 'app.pyz').unlink()
                p.rmdir()
            (prefix / 'update-cache.json').unlink(missing_ok=True)
            m.update(installed=False, payloads=[], auto_update=False)
            write_json(prefix / 'installation.json', m)
        return {'uninstalled': True, 'data_retained': True, 'prefix': str(prefix)}


def candidate(metadata, current):
    if not isinstance(metadata, dict):
        denied()
    if metadata.get('draft') is True or metadata.get('prerelease') is True:
        return None
    if metadata.get('draft') is not False or metadata.get('prerelease') is not False:
        denied()
    tag = metadata.get('tag_name')
    numeric = version(tag)
    if numeric <= version('v' + current):
        return None
    assets = metadata.get('assets')
    if not isinstance(assets, list):
        denied()
    assets = [a for a in assets if isinstance(a, dict) and a.get('name') == ASSET]
    if len(assets) != 1:
        denied()
    a = assets[0]
    if (type(a.get('id')) is not int or not 0 < a['id'] < 10**18 or type(a.get('size')) is not int or
            not 0 < a['size'] <= MAX_SIZE or not isinstance(a.get('digest'), str) or
            not re.fullmatch(r'sha256:[0-9a-f]{64}', a['digest'])):
        denied()
    if 'browser_download_url' in a and a['browser_download_url'] != binary_url(tag[1:]):
        denied()
    return dict(version=tag[1:], id=a['id'], size=a['size'], digest=a['digest'][7:])


# This block is deliberately identical in the standalone bootstrap and updater.
REQUEST_SECONDS = 30
MAX_REDIRECTS = 4
API_URL = 'https://api.github.com/' + REPO + '/releases/latest'


def binary_url(number):
    version('v' + number)
    return 'https://github.com/' + REPO.removeprefix('repos/') + '/releases/download/v' + number + '/' + ASSET


def destination(url, hosts):
    try:
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != 'https' or parsed.hostname not in hosts or
                parsed.port not in (None, 443) or parsed.username is not None or
                parsed.password is not None or parsed.fragment or
                any(ord(c) <= 32 or ord(c) == 127 for c in url) or '\\' in url):
            denied()
    except ValueError:
        denied()


class Redirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, hosts, deadline):
        super().__init__()
        self.hosts, self.deadline, self.count = hosts, deadline, 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.count += 1
        if self.count > MAX_REDIRECTS or time.monotonic() >= self.deadline:
            denied()
        destination(newurl, self.hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

    def http_error_302(self, req, fp, code, msg, headers):
        # urllib's default handler drains redirect bodies with an unbounded read.
        # Close instead; each hop is validated before opening the next socket.
        try:
            location = headers.get('Location')
            if (not isinstance(location, str) or not location or
                    any(ord(c) <= 32 or ord(c) == 127 for c in location) or '\\' in location):
                denied()
            newurl = urllib.parse.urljoin(req.full_url, location)
            redirected = self.redirect_request(req, fp, code, msg, headers, newurl)
        finally:
            fp.close()
        return self.parent.open(redirected, timeout=min(10, self.deadline - time.monotonic()))

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def transfer(url, target, limit):
    # No proxy discovery, netrc, client certs, auth handler or ambient TLS roots.
    hosts = ('api.github.com',) if url == API_URL else ('github.com', 'release-assets.githubusercontent.com')
    destination(url, hosts)
    deadline = time.monotonic() + REQUEST_SECONDS
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile='/etc/ssl/certs/ca-certificates.crt')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                        Redirect(hosts, deadline),
                                        urllib.request.HTTPSHandler(context=context))
    request = urllib.request.Request(url, headers={'Accept': 'application/vnd.github+json' if url == API_URL else 'application/octet-stream',
                                                  'Cache-Control': 'no-store', 'User-Agent': 'buzz-server'})
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as output, opener.open(request, timeout=10) as response:
        destination(response.geturl(), hosts)
        if response.status != 200 or response.headers.get('Content-Encoding', 'identity') != 'identity':
            denied()
        total = 0
        while True:
            if time.monotonic() >= deadline:
                denied()
            block = response.read1(min(65536, limit + 1 - total))
            if not block:
                break
            total += len(block)
            if total > limit:
                denied()
            output.write(block)


def transfer_child(url, target, limit):
    try:
        os.environ.clear()
        os.environ.update(PATH='/usr/local/bin:/usr/bin:/bin', LANG='C.UTF-8')
        transfer(url, target, limit)
    except BaseException:
        # Never expose urllib errors, signed redirect queries or remote bodies.
        os._exit(1)


def fetch(url, target, limit):
    # Only product-constructed initial URLs; redirects have their own boundary.
    if url != API_URL:
        prefix = 'https://github.com/' + REPO.removeprefix('repos/') + '/releases/download/v'
        if not url.startswith(prefix) or not url.endswith('/' + ASSET):
            denied()
        if binary_url(url[len(prefix):-len('/' + ASSET)]) != url:
            denied()
    # A process watchdog also bounds DNS, TLS, header reads and redirect bodies,
    # which a socket timeout or read-loop deadline alone cannot bound in total.
    child = multiprocessing.get_context('fork').Process(target=transfer_child, args=(url, target, limit))
    try:
        child.start()
        child.join(REQUEST_SECONDS)
        if child.is_alive() or child.exitcode != 0:
            denied()
    except (OSError, ValueError):
        denied()
    finally:
        if child.pid is not None:
            if child.is_alive():
                child.kill()
            child.join()
            child.close()


def latest(directory, current):
    target = directory / 'metadata.json'
    fetch(API_URL, target, 1024 * 1024)
    try:
        return candidate(json.loads(target.read_bytes()), current)
    except (ValueError, UnicodeError):
        denied()


def download(directory, selected):
    target = directory / ASSET
    fetch(binary_url(selected['version']), target, selected['size'])
    verify_download(target, selected)
    return target


def verify_download(target, selected):
    if (target.is_symlink() or not target.is_file() or target.stat().st_size != selected['size'] or
            not 0 < selected['size'] <= MAX_SIZE or
            hashlib.sha256(target.read_bytes()).hexdigest() != selected['digest']):
        denied()
    number, digest, data = inspect_artifact(target)
    if (number, digest, len(data)) != (selected['version'], selected['digest'], selected['size']):
        denied()


def update(prefix):
    with locked(prefix) as prefix:
        m = manifest(prefix)
        inventory(prefix, m)
        if not m['installed']:
            denied()
        original = (prefix / 'active').readlink()
    with tempfile.TemporaryDirectory(prefix='buzz-update-') as temp:
        directory = Path(temp)
        selected = latest(directory, m['version'])
        if selected is None:
            return {'version': m['version'], 'updated': False}
        selected = dict(selected)
        target = download(directory, selected)
        with locked(prefix):
            current = manifest(prefix)
            inventory(prefix, current)
            if (not current['installed'] or current['version'] != m['version'] or
                    (current['state'], current['units']) != (m['state'], m['units']) or
                    (prefix / 'active').readlink() != original):
                denied()
            verify_download(target, selected)
            result = _install(target, prefix, current['state'], current['units'])
            result['updated'] = True
            return result


def set_auto(prefix, enabled):
    with locked(prefix) as prefix:
        m = manifest(prefix)
        inventory(prefix, m)
        m['auto_update'] = enabled
        write_json(prefix / 'installation.json', m)
        return {'auto_update': enabled}


def cache(prefix):
    p = prefix / 'update-cache.json'
    if not p.exists():
        return {'checked': 0, 'latest': '', 'notified': 0, 'notified_version': ''}
    c = read_json(p)
    if (not isinstance(c, dict) or set(c) != {'checked', 'latest', 'notified', 'notified_version'} or
            any(type(c[k]) is not int or c[k] < 0 for k in ('checked', 'notified'))):
        denied()
    for k in ('latest', 'notified_version'):
        if c[k] != '':
            version('v' + c[k])
    return c


def probe(prefix):
    """Compatibility no-op for old detached invocations; never acquire or activate."""
    return


def binding(m, state, units):
    if not m['installed'] or (m['state'], m['units']) != (str(Path(state).absolute()), str(Path(units).absolute())):
        raise Error('installation_binding', "Use this installation's original state and unit directories; separate installations need separate prefixes.")


def before_command(prefix, operation, structured, tty, state, units, argv, now=None):
    """Offer one exact release before dispatch, with no lock across human/network I/O."""
    if (prefix is None or structured or not tty or os.getuid() == 0 or
            operation in ('update', 'install', 'uninstall', '_worker', 'account-plan', 'version')):
        return
    path = None
    committed = False
    approved_from = None
    try:
        with locked(prefix, wait=0) as prefix:
            m = manifest(prefix)
            binding(m, state, units)
            # A stale immutable invocation cannot approve an update for a new active CLI.
            original = (prefix / 'active/app.pyz').resolve(strict=True)
            if original != Path(sys.argv[0]).absolute() or not m['auto_update']:
                return
            c = cache(prefix)
            now = int(time.time()) if now is None else now
            if c['checked'] and now - c['checked'] < DAY:
                return
            inventory(prefix, m)
            c['checked'] = now
            write_json(prefix / 'update-cache.json', c)
        with tempfile.TemporaryDirectory(prefix='buzz-consent-') as temp:
            directory = Path(temp)
            selected = latest(directory, m['version'])
            if selected is None:
                return
            # Keep the vetted identity independent of mutable metadata/caller objects.
            selected = dict(selected)
            print('Buzz Server ' + selected['version'] + ' is available. Update before this command? [y/N] ', end='', file=sys.stderr, flush=True)
            if sys.stdin.readline().strip().lower() not in ('y', 'yes'):
                return
            approved_from = m['version']
            target = download(directory, selected)
            with locked(prefix, wait=0):
                current = manifest(prefix)
                inventory(prefix, current)
                binding(current, state, units)
                if (not current['auto_update'] or current['version'] != m['version'] or
                        (prefix / 'active/app.pyz').resolve(strict=True) != original):
                    denied()
                verify_download(target, selected)
                # Consent/download may have crossed the daily boundary. Suppress
                # another offer when the accepted zipapp resumes this command.
                c = cache(prefix)
                c['checked'] = int(time.time())
                write_json(prefix / 'update-cache.json', c)
                _install(target, prefix, current['state'], current['units'])
                committed = True
                path = prefix / 'payloads' / (selected['version'] + '-' + selected['digest']) / 'app.pyz'
                # Verify the installed immutable bytes too, before executing them.
                verify_download(path, selected)
    except (Error, OSError, ValueError, TypeError, KeyError):
        if committed:
            raise
        if approved_from is not None:
            print('Update not applied; continuing with Buzz Server ' + approved_from +
                  '. Run buzz-server update to retry.', file=sys.stderr)
        return
    if path is not None:
        # Keep exec errors outside best-effort update handling: never dispatch old
        # code after committing activation and failing to hand off the command.
        os.execv('/usr/bin/python3', ['/usr/bin/python3', '-I', str(path), *argv])
