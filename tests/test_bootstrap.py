"""Independent bootstrap tests; only synthetic local GitHub responses."""
import copy
import errno
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from buzz_server import release

SOURCE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('bootstrap', SOURCE / 'scripts/bootstrap.py')
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bootstrap-test-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.artifact = self.base / 'candidate.pyz'
        release.build(SOURCE, self.artifact, '1.0.0')
        self.data = self.artifact.read_bytes()
        self.metadata = dict(tag_name='v1.0.0', draft=False, prerelease=False, assets=[dict(
            name='buzz-server.pyz', id=42, size=len(self.data),
            digest='sha256:' + hashlib.sha256(self.data).hexdigest())])

    def test_contract_matches_updater(self):
        self.assertEqual(bootstrap.candidate(self.metadata), release.candidate(self.metadata, '0.0.0'))

    def test_invalid_metadata_fails_closed(self):
        for key, value in [('tag_name', 'v1.0.0-rc1'), ('tag_name', 'v01.0.0'),
                           ('draft', True), ('prerelease', True), ('draft', None), ('assets', [])]:
            m = copy.deepcopy(self.metadata)
            m[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(bootstrap.Refused):
                bootstrap.candidate(m)
        for key, value in [('id', True), ('id', 0), ('size', True), ('size', bootstrap.MAX_SIZE + 1),
                           ('digest', None), ('digest', 'sha256:wrong')]:
            m = copy.deepcopy(self.metadata)
            m['assets'][0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(bootstrap.Refused):
                bootstrap.candidate(m)
        self.metadata['assets'] *= 2
        with self.assertRaises(bootstrap.Refused):
            bootstrap.candidate(self.metadata)

    def test_bytes_and_embedded_version_checked_before_execution(self):
        selected = bootstrap.candidate(self.metadata)
        bootstrap.verify(self.artifact, selected)
        for data in [self.data[:-1], self.data + b'x', b'X' + self.data[1:]]:
            self.artifact.write_bytes(data)
            with self.assertRaises(bootstrap.Refused):
                bootstrap.verify(self.artifact, selected)
        self.artifact.write_bytes(self.data)
        selected['version'] = '1.1.0'
        with self.assertRaises(bootstrap.Refused):
            bootstrap.verify(self.artifact, selected)
        link = self.base / 'link'
        link.symlink_to(self.artifact)
        with self.assertRaises(bootstrap.Refused):
            bootstrap.verify(link, selected)

    def fake_transport(self, data=None, metadata=None):
        def fetch(url, target, limit):
            target.write_bytes(json.dumps(metadata or self.metadata).encode() if url == bootstrap.API_URL
                               else self.data if data is None else data)
        return fetch

    def test_anonymous_fetch_and_real_local_install(self):
        fake = self.fake_transport()
        prefix, state, units = [self.base / n for n in ('prefix', 'state', 'units')]
        args = ['--json', '--prefix', str(prefix), '--state-dir', str(state), '--unit-dir', str(units)]
        # Actual verified zipapp executes offline; no manager operation is used.
        with patch.object(bootstrap, 'fetch', side_effect=fake):
            self.assertEqual(bootstrap.main(args), 0)
        m = json.loads((prefix / 'installation.json').read_text())
        self.assertEqual((m['state'], m['units']), (str(state), str(units)))
        self.assertTrue(m['auto_update'])
        p = subprocess.run([str(prefix / 'bin/buzz-server'), '--version'], capture_output=True, timeout=5)
        self.assertEqual((p.returncode, p.stdout.strip()), (0, b'1.0.0'))
        # Retry remains safe and preserves state.
        state.mkdir(mode=0o700, exist_ok=True)
        retained = state / 'retained'
        retained.write_bytes(b'synthetic-private-state')
        with patch.object(bootstrap, 'fetch', side_effect=fake):
            self.assertEqual(bootstrap.main(args), 0)
        self.assertEqual(retained.read_bytes(), b'synthetic-private-state')

    def test_bad_download_never_executes_or_creates_prefix(self):
        fake = self.fake_transport(data=b'corrupt')
        prefix = self.base / 'prefix'
        actual = subprocess.run
        calls = []
        def run(argv, **kw):
            calls.append(argv)
            return actual(argv, **kw)
        with patch.object(bootstrap, 'fetch', side_effect=fake), patch.object(bootstrap.subprocess, 'run', side_effect=run), patch('sys.stderr', new_callable=io.StringIO):
            self.assertEqual(bootstrap.main(['--prefix', str(prefix)]), 1)
        self.assertEqual(len(calls), 0)
        self.assertFalse(prefix.exists())

    def test_failed_retry_preserves_existing_installation(self):
        fake = self.fake_transport()
        prefix = self.base / 'prefix'
        args = ['--prefix', str(prefix), '--state-dir', str(self.base / 'state'),
                '--unit-dir', str(self.base / 'units')]
        with patch.object(bootstrap, 'fetch', side_effect=fake):
            self.assertEqual(bootstrap.main(args), 0)
        before = (prefix / 'installation.json').read_bytes()
        active = (prefix / 'active').readlink()
        fake = self.fake_transport(data=b'interrupted-download')
        with patch.object(bootstrap, 'fetch', side_effect=fake), patch('sys.stderr', new_callable=io.StringIO):
            self.assertEqual(bootstrap.main(args), 1)
        self.assertEqual((prefix / 'installation.json').read_bytes(), before)
        self.assertEqual((prefix / 'active').readlink(), active)

    def test_timeouts_are_sanitized_and_no_payload_executes(self):
        with patch.object(bootstrap, 'fetch', side_effect=bootstrap.Refused(bootstrap.MESSAGE)) as run, patch(
                    'sys.stderr', new_callable=io.StringIO) as err:
            self.assertEqual(bootstrap.main(['--json']), 1)
        self.assertEqual(run.call_count, 1)
        self.assertNotIn('private-remote-output', err.getvalue())
        self.assertEqual(json.loads(err.getvalue())['error']['code'], 'release_refused')

    def test_environment_has_no_ambient_secrets_or_loader_settings(self):
        with patch.dict(os.environ, {'GH_TOKEN': 'secret', 'OPENAI_API_KEY': 'secret', 'LD_PRELOAD': '/bad',
                                     'HTTPS_PROXY': 'secret', 'PYTHONPATH': '/bad', 'PATH': '/bad', 'XDG_CONFIG_HOME': '/bad'}):
            env = bootstrap.environment(self.base)
        self.assertEqual(set(env), {'HOME', 'PATH', 'LANG'})
        self.assertEqual(env['PATH'], '/usr/local/bin:/usr/bin:/bin')

    def test_dependency_flags_and_tty_forward_only_after_verification(self):
        def fetch(args, target, limit):
            target.write_bytes(json.dumps(self.metadata).encode() if args.endswith('/latest') else self.data)
        for flag, structured, tty in (('--install-dependencies', False, True), ('--skip-dependencies', False, True),
                                       ('--install-dependencies', True, True), ('--skip-dependencies', False, False)):
            with self.subTest(flag=flag, structured=structured, tty=tty):
                with (patch.object(bootstrap, 'fetch', side_effect=fetch),
                      patch.object(bootstrap.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run,
                      patch.object(bootstrap.sys.stdin, 'isatty', return_value=tty),
                      patch.object(bootstrap.sys.stdout, 'isatty', return_value=tty),
                      patch.object(bootstrap.sys.stderr, 'isatty', return_value=tty)):
                    self.assertEqual(bootstrap.main([flag, *(['--json'] if structured else [])]), 0)
                command = run.call_args.args[0]
                self.assertIn(flag, command)
                self.assertEqual(command[:2], ['/usr/bin/python3', '-I'])
                self.assertEqual(run.call_args.kwargs['stdin'], None if tty and not structured else subprocess.DEVNULL)
                self.assertNotIn('HTTPS_PROXY', run.call_args.kwargs['env'])

    def test_piped_input_reopens_controlling_terminal(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        def open_terminal(path, mode):
            self.assertEqual((path, mode), ('/dev/tty', 'r'))
            return os.fdopen(os.dup(slave), mode)
        args = bootstrap.parser().parse_args([])
        with (patch.object(bootstrap.sys.stdin, 'isatty', return_value=False),
              patch.object(bootstrap.sys.stdout, 'isatty', return_value=True),
              patch.object(bootstrap.sys.stderr, 'isatty', return_value=True),
              patch('builtins.open', side_effect=open_terminal)):
            with bootstrap.terminal(args) as (stream, interactive):
                self.assertTrue(interactive)
                self.assertTrue(os.isatty(stream.fileno()))

    def test_bootstrap_pipe_preserves_real_terminal_consent(self):
        source = self.base / 'source'
        shutil.copytree(SOURCE / 'buzz_server', source / 'buzz_server')
        shutil.copy2(SOURCE / 'entry.py', source / 'entry.py')
        # Keep installation, terminal handling and dependency consent real. Only
        # acquisition/platform checks are fixture-backed (no external requests).
        with (source / 'buzz_server/dependencies.py').open('a') as file:
            file.write('''
def resolve(name, root=None):
    if root is not None and (bin_dir(root) / name).exists():
        return str(bin_dir(root) / name)
    return None

def supported():
    pass

def acquire(package, directory):
    for name, _ in package['members'].values():
        binary = directory / name
        binary.write_bytes(b'#!/bin/sh\\nexit 0\\n')
        binary.chmod(0o700)
''')
        release.build(source, self.artifact, '1.0.0')
        data = self.artifact.read_bytes()
        fixture_metadata = dict(tag_name='v1.0.0', draft=False, prerelease=False, assets=[dict(
            name=bootstrap.ASSET, id=42, size=len(data), digest='sha256:' + hashlib.sha256(data).hexdigest())])
        metadata = self.base / 'metadata.json'
        metadata.write_text(json.dumps(fixture_metadata))
        script = self.base / 'installer.py'
        original = (SOURCE / 'scripts/bootstrap.py').read_text()
        prefix, separator, tail = original.partition("if __name__ == '__main__':")
        self.assertTrue(separator)
        script.write_text(prefix + '\ndef fetch(url, target, limit):\n'
                          + f'    path = {str(metadata)!r} if url == API_URL else {str(self.artifact)!r}\n'
                          + '    target.write_bytes(Path(path).read_bytes())\n\n' + separator + tail)
        for response, downloads in ((b'\n', True), (b'no\n', False)):
            with self.subTest(downloads=downloads):
                home = self.base / ('accept' if downloads else 'decline')
                home.mkdir(mode=0o700)
                child, master = pty.fork()
                if child == 0:
                    os.chdir(home)
                    os.execve('/bin/bash', ['bash', '-o', 'pipefail', '-c',
                                             '/bin/cat "$1" | /usr/bin/python3 -I -', 'bash', str(script)],
                              {'HOME': str(home), 'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
                reaped = False
                output = b''
                sent = False
                try:
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        if select.select([master], [], [], .1)[0]:
                            try:
                                part = os.read(master, 16384)
                            except OSError as exc:
                                if exc.errno != errno.EIO:
                                    raise
                                part = b''
                            output += part
                            if not sent and b'Install missing tools? [Y/n]' in output:
                                os.write(master, response)
                                sent = True
                        pid, status = os.waitpid(child, os.WNOHANG)
                        if pid:
                            reaped = True
                            self.assertEqual(os.waitstatus_to_exitcode(status), 0, output)
                            break
                    self.assertTrue(reaped and sent, output)
                finally:
                    if not reaped:
                        try:
                            os.killpg(child, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        os.waitpid(child, 0)
                    os.close(master)
                command = home / '.local/bin/buzz-server'
                self.assertTrue(command.is_symlink())
                tools = home / '.local/share/buzz-server/tools/bin'
                self.assertEqual({p.name for p in tools.glob('*')},
                                 {'buzz', 'buzz-acp', 'goose'} if downloads else set())

    def test_interactive_consent_has_no_parent_deadline(self):
        def fetch(args, target, limit):
            target.write_bytes(json.dumps(self.metadata).encode() if args.endswith('/latest') else self.data)
        with (patch.object(bootstrap, 'fetch', side_effect=fetch),
              patch.object(bootstrap.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as launch,
              patch.object(bootstrap.sys.stdin, 'isatty', return_value=True),
              patch.object(bootstrap.sys.stdout, 'isatty', return_value=True),
              patch.object(bootstrap.sys.stderr, 'isatty', return_value=True)):
            self.assertEqual(bootstrap.main([]), 0)
        self.assertIsNone(launch.call_args.kwargs.get('timeout'))

    def test_noninteractive_install_timeout_reports_possible_activation(self):
        def fetch(args, target, limit):
            target.write_bytes(json.dumps(self.metadata).encode() if args.endswith('/latest') else self.data)
        with (patch.object(bootstrap, 'fetch', side_effect=fetch),
              patch.object(bootstrap.subprocess, 'run', side_effect=subprocess.TimeoutExpired('private-child-argv', 600)) as launch,
              patch('sys.stderr', new_callable=io.StringIO) as err):
            self.assertEqual(bootstrap.main(['--json', '--install-dependencies']), 1)
        self.assertEqual(launch.call_args.kwargs.get('timeout'), 600)
        error = json.loads(err.getvalue())['error']
        self.assertEqual(error['code'], 'install_incomplete')
        self.assertIn('may already be installed', error['message'])
        self.assertIn('same command', error['message'])
        self.assertNotIn('private-child-argv', err.getvalue())


if __name__ == '__main__':
    unittest.main()
