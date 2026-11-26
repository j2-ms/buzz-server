#!/usr/bin/python3 -I
"""Independent first installer. Publish unchanged as install-buzz-server.py."""
import argparse
import ast
import contextlib
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import re
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile

__version__ = '0.3.0'
REPO = 'repos/j2-ms/buzz-server'
ASSET = 'buzz-server.pyz'
MAX_SIZE = 8 * 1024 * 1024
SYSTEM_PATH = '/usr/local/bin:/usr/bin:/bin'
MESSAGE = ('First installation refused. Check public stable release availability, '
           'GitHub metadata/checksums and connectivity. See docs/releases.md. '
           'No raw remote diagnostics are returned.')
INCOMPLETE = ('Installation did not finish. Buzz Server may already be installed. '
              'Retry the same command to finish; keep the same prefix and directories.')


class Refused(Exception):
    pass


def version(tag):
    if not isinstance(tag, str) or not re.fullmatch(r'v(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})', tag):
        raise Refused(MESSAGE)
    return tag[1:]


def candidate(metadata):
    # Keep this small independent copy in parity with release.candidate. No
    # installed package or unverified downloaded Python is imported here.
    if not isinstance(metadata, dict) or metadata.get('draft') is not False or metadata.get('prerelease') is not False:
        raise Refused(MESSAGE)
    number = version(metadata.get('tag_name'))
    assets = metadata.get('assets')
    if not isinstance(assets, list):
        raise Refused(MESSAGE)
    matches = [a for a in assets if isinstance(a, dict) and a.get('name') == ASSET]
    if len(matches) != 1:
        raise Refused(MESSAGE)
    a = matches[0]
    if (type(a.get('id')) is not int or not 0 < a['id'] < 10**18 or
            type(a.get('size')) is not int or not 0 < a['size'] <= MAX_SIZE or
            not isinstance(a.get('digest'), str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', a['digest'])):
        raise Refused(MESSAGE)
    if 'browser_download_url' in a and a['browser_download_url'] != binary_url(number):
        denied()
    return dict(version=number, id=a['id'], size=a['size'], digest=a['digest'][7:])


def environment(home):
    # Payload execution inherits no credentials, proxies, loader or config overrides.
    return dict(HOME=str(home), PATH=SYSTEM_PATH, LANG='C.UTF-8')


def denied():
    raise Refused(MESSAGE)


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


def verify(path, selected):
    # Hash and exact size precede even ZIP parsing, let alone execution/import.
    s = path.lstat()
    if not stat.S_ISREG(s.st_mode) or s.st_size != selected['size'] or not 0 < s.st_size <= MAX_SIZE:
        raise Refused(MESSAGE)
    if hashlib.sha256(path.read_bytes()).hexdigest() != selected['digest']:
        raise Refused(MESSAGE)
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if (len(names) != len(set(names)) or len(names) > 64 or
                    any(not re.fullmatch(r'(VERSION|__main__\.py|buzz_server/[a-z_]+\.py)', n) for n in names) or
                    sum(i.file_size for i in archive.infolist()) > MAX_SIZE):
                raise Refused(MESSAGE)
            number = version('v' + archive.read('VERSION').decode('ascii').strip())
            tree = ast.parse(archive.read('buzz_server/__init__.py'))
            values = [ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign) and
                      any(isinstance(t, ast.Name) and t.id == '__version__' for t in n.targets)]
            if number != selected['version'] or values != [number] or '__main__.py' not in names or archive.testzip() is not None:
                raise Refused(MESSAGE)
    except (ValueError, KeyError, SyntaxError, UnicodeError, zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError):
        raise Refused(MESSAGE) from None


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Refused('Invalid installer arguments. Run install-buzz-server.py --help. Supply only nonsecret directory paths.')


def parser():
    p = Parser(description='Download and verify the public stable Buzz Server release anonymously. Optional private runtime tools; no accounts or services.')
    p.add_argument('--version', action='version', version=__version__)
    p.add_argument('--json', action='store_true', help='Use the versioned CLI JSON contract')
    choice = p.add_mutually_exclusive_group()
    choice.add_argument('--install-dependencies', action='store_true')
    choice.add_argument('--skip-dependencies', action='store_true')
    p.add_argument('--prefix', default=str(Path.home() / '.local/buzz-server-cli'))
    p.add_argument('--state-dir', default=str(Path.home() / '.local/share/buzz-server'))
    p.add_argument('--unit-dir', default=str(Path.home() / '.config/systemd/user'))
    return p


@contextlib.contextmanager
def terminal(args):
    # A curl pipe is source input, never consent input. Reopen only the controlling
    # terminal, and only when output is also terminal-bound. Batch flags and JSON
    # must not access it. The verified installer receives an actual terminal fd.
    if not args.json and all(s.isatty() for s in (sys.stdout, sys.stderr)):
        if sys.stdin.isatty():
            yield None, True
            return
        if not (args.install_dependencies or args.skip_dependencies):
            try:
                stream = open('/dev/tty', 'r')
            except OSError:
                stream = None
            if stream is not None:
                with stream:
                    if stream.isatty():
                        yield stream, True
                        return
    yield subprocess.DEVNULL, False


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    structured = '--json' in argv
    code = 'release_refused'
    try:
        args = parser().parse_args(argv)
        if sys.platform != 'linux':
            raise Refused('Buzz Server requires Linux and Python 3.12+. See docs/releases.md for prerequisites.')
        if sys.version_info < (3, 12):
            raise Refused('Install Python 3.12+ before running this installer.')
        if os.getuid() == 0:
            raise Refused('Run this installer as the existing unprivileged install account.')
        with terminal(args) as (stdin, interactive), tempfile.TemporaryDirectory(prefix='buzz-first-install-') as temp:
            directory = Path(temp)
            metadata = directory / 'metadata.json'
            if interactive:
                print('Checking the public Buzz Server release.', file=sys.stderr, flush=True)
            fetch(API_URL, metadata, 1024 * 1024)
            selected = candidate(json.loads(metadata.read_bytes()))
            artifact = directory / ASSET
            if interactive:
                print(f"Downloading Buzz Server {selected['version']}.", file=sys.stderr, flush=True)
            fetch(binary_url(selected['version']), artifact, selected['size'])
            if interactive:
                print('Verifying the installer payload.', file=sys.stderr, flush=True)
            verify(artifact, selected)
            command = ['/usr/bin/python3', '-I', str(artifact)]
            if args.json:
                command.append('--json')
            command += ['--state-dir', str(Path(args.state_dir).absolute()), '--unit-dir', str(Path(args.unit_dir).absolute()),
                        'install', '--prefix', str(Path(args.prefix).absolute())]
            if args.install_dependencies:
                command.append('--install-dependencies')
            if args.skip_dependencies:
                command.append('--skip-dependencies')
            # This is the first execution of downloaded code. Its own install
            # handles state binding and per-step limits. Do not time a user's
            # decision at the consent prompt with the noninteractive deadline.
            try:
                result = subprocess.run(command, stdin=stdin,
                                        env=environment(Path.home()), cwd=directory,
                                        timeout=None if interactive else 600)
                if result.returncode >= 0:
                    return result.returncode
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                pass
            code, message = 'install_incomplete', INCOMPLETE
    except KeyboardInterrupt:
        message = 'Installation interrupted. Retry the same prefix and directories; no successful installation is claimed.'
    except Refused as error:
        message = str(error)
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        message = MESSAGE
    if structured:
        print(json.dumps(dict(schema_version=1, operation='install', ok=False,
                              error=dict(code=code, message=message)), sort_keys=True), file=sys.stderr)
    else:
        print(f'Error [{code}]: {message}', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
