"""Installed launcher and PTY checks with two zipapps and an offline release fixture."""
import errno
import hashlib
import json
import os
from pathlib import Path
import pty
import select
import shutil
import subprocess
import tempfile
import time
import unittest

from buzz_server import release


class UpdatePtyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='buzz-pty-')
        self.addCleanup(temp.cleanup)
        self.base = base = Path(temp.name)
        source = base / 'source'
        source.mkdir()
        repo = Path(__file__).resolve().parents[1]
        shutil.copytree(repo / 'buzz_server', source / 'buzz_server')
        shutil.copy(repo / 'entry.py', source / 'entry.py')
        # Replace transport only in the fixture zipapps. Allow the test to hold
        # metadata until it observes the progress message on the terminal.
        with (source / 'buzz_server/release.py').open('a') as f:
            f.write('\ndef fetch(url, target, limit):\n'
                    f'    root = Path({str(base)!r})\n'
                    "    with (root / 'requests').open('a') as log:\n"
                    "        log.write(url + '\\n')\n"
                    "    if 'releases/latest' in url and (root / 'hold-metadata').exists():\n"
                    "        deadline = time.monotonic() + 4\n"
                    "        while (root / 'hold-metadata').exists():\n"
                    "            if time.monotonic() >= deadline:\n"
                    "                denied()\n"
                    "            time.sleep(.01)\n"
                    "    fixture = 'metadata' if 'releases/latest' in url else 'download'\n"
                    "    target.write_bytes((root / fixture).read_bytes())\n")
        with (source / 'buzz_server/cli.py').open('a') as f:
            f.write('\n_original_execute = execute\ndef execute(args):\n'
                    '    if args.operation == "list":\n'
                    f'        with open({str(base / "dispatch")!r}, "a") as f:\n'
                    "            f.write(json.dumps([__version__, sys.argv[1:]]) + '\\n')\n"
                    "    return _original_execute(args)\n")
        self.a, self.b = base / 'a.pyz', base / 'b.pyz'
        release.build(source, self.a, '1.0.0')
        release.build(source, self.b, '1.1.0')
        metadata = dict(tag_name='v1.1.0', draft=False, prerelease=False, assets=[dict(
            name=release.ASSET, id=71, size=self.b.stat().st_size,
            digest='sha256:' + hashlib.sha256(self.b.read_bytes()).hexdigest())])
        (base / 'metadata').write_text(json.dumps(metadata))
        (base / 'download').write_bytes(self.b.read_bytes())
        self.state, self.units = base / 'state', base / 'units'
        self.list_args = ['--state-dir', str(self.state), '--unit-dir', str(self.units), 'list']

    def install(self, name):
        prefix = self.base / name
        release.install(self.a, prefix, self.state, self.units)
        return prefix, str(prefix / 'bin/buzz-server')

    def terminal(self, command, *, observe=None):
        master, slave = pty.openpty()
        try:
            p = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave)
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        output = b''
        deadline = time.monotonic() + 8
        try:
            while time.monotonic() < deadline:
                if not select.select([master], [], [], .1)[0]:
                    continue
                try:
                    chunk = os.read(master, 65536)
                except OSError as e:
                    if e.errno != errno.EIO:
                        raise
                    break
                if not chunk:
                    break
                output += chunk
                if observe:
                    observe(master, output)
            return p.wait(timeout=1), output.replace(b'\r\n', b'\n')
        finally:
            if p.poll() is None:
                p.kill()
                p.wait(timeout=2)
            os.close(master)

    def test_two_build_precommand_handoff(self):
        base = self.base
        for scenario in ('accept', 'decline', 'failure', 'json', 'noTTY'):
            with self.subTest(scenario=scenario):
                prefix, launcher = self.install(scenario)
                (base / 'download').write_bytes(b'bad' if scenario == 'failure' else self.b.read_bytes())
                (base / 'dispatch').unlink(missing_ok=True)
                (base / 'requests').unlink(missing_ok=True)
                argv = ['--json', *self.list_args] if scenario == 'json' else self.list_args
                command = [launcher, *argv]
                if scenario == 'noTTY':
                    p = subprocess.run(command, capture_output=True, timeout=8)
                    output, code = p.stdout + p.stderr, p.returncode
                else:
                    sent = False

                    def answer(master, output):
                        nonlocal sent
                        if b'[y/N]' in output and not sent:
                            self.assertFalse((base / 'dispatch').exists())
                            self.assertNotIn(b'Downloading', output)
                            os.write(master, b'no\n' if scenario == 'decline' else b'yes\n')
                            sent = True

                    code, output = self.terminal(command, observe=answer)
                self.assertEqual(code, 0, output)
                expected = '1.1.0' if scenario == 'accept' else '1.0.0'
                self.assertEqual([json.loads(x) for x in (base / 'dispatch').read_text().splitlines()], [[expected, argv]])
                self.assertEqual(release.manifest(prefix)['version'], expected)
                self.assertEqual(output.count(b'[y/N]'), 0 if scenario in ('json', 'noTTY') else 1)
                warning = b'Update not applied; continuing with Buzz Server 1.0.0. Run buzz-server update to retry.'
                self.assertEqual(output.count(warning), int(scenario == 'failure'))
                if scenario == 'failure':
                    self.assertLess(output.index(warning), output.index(b'No managed agents.'))
                    self.assertNotIn(b'Activating Buzz Server', output)
                if scenario == 'accept':
                    phases = [b'[y/N]', b'Downloading and verifying', b'Activating Buzz Server 1.1.0',
                              b'Updated Buzz Server from 1.0.0 to 1.1.0. Workers were not restarted.',
                              b'No managed agents.']
                    offsets = [output.index(phase) for phase in phases]
                    self.assertEqual(offsets, sorted(offsets))
                if scenario in ('json', 'noTTY'):
                    self.assertFalse((base / 'requests').exists())
                else:
                    self.assertEqual(len((base / 'requests').read_text().splitlines()), 1 if scenario == 'decline' else 2)

    def test_installed_help_and_version_stay_offline(self):
        prefix, launcher = self.install('shortcuts')
        for args in ([], ['-v'], ['--version']):
            with self.subTest(args=args):
                self.assertEqual(self.terminal([launcher, *args]), (0, b'1.0.0\n'))
        for args in ([], ['update'], ['inference', 'configure']):
            with self.subTest(args=args):
                code, output = self.terminal([launcher, *args, '--help'])
                self.assertEqual(code, 0, output)
                self.assertTrue(output.startswith(b'usage: buzz-server ' + b' '.join(x.encode() for x in args)), output)
                self.assertNotIn(b'app.pyz', output)
        code, output = self.terminal([launcher, '--json', 'version'])
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output), dict(schema_version=1, operation='version', ok=True, data={'version': '1.0.0'}))
        code, output = self.terminal([launcher, 'not-a-command'])
        self.assertEqual(code, 1, output)
        self.assertIn(b'Error [usage]', output)
        for setting in ('off', 'on'):
            code, output = self.terminal([launcher, 'update', '--auto', setting])
            self.assertEqual(code, 0, output)
            self.assertIn(b'Interactive update offers', output)
            self.assertIs(release.manifest(prefix)['auto_update'], setting == 'on')
        self.assertFalse((self.base / 'requests').exists())
        self.assertFalse((prefix / 'update-cache.json').exists())

    def test_explicit_update_bypasses_offer_throttle_with_progress(self):
        prefix, launcher = self.install('explicit')
        code, output = self.terminal([launcher, *self.list_args], observe=self.decline)
        self.assertEqual(code, 0, output)
        self.assertIn(b'Buzz Server 1.1.0 is available', output)
        self.assertTrue(release.cache(prefix)['checked'])
        self.assertEqual(self.terminal([launcher, *self.list_args]), (0, b'No managed agents.\n'))
        requests = self.base / 'requests'
        self.assertEqual(requests.read_text().splitlines(), [release.API_URL])

        hold = self.base / 'hold-metadata'
        hold.touch()

        def resume_after_progress(master, output):
            if b'Checking for a newer stable Buzz Server release...' in output:
                hold.unlink(missing_ok=True)

        code, output = self.terminal([launcher, 'update'], observe=resume_after_progress)
        self.assertEqual(code, 0, output)
        phases = [b'Checking for a newer stable', b'Downloading and verifying', b'Activating Buzz Server 1.1.0',
                  b'Updated Buzz Server from 1.0.0 to 1.1.0. Workers were not restarted.']
        offsets = [output.index(phase) for phase in phases]
        self.assertEqual(offsets, sorted(offsets))
        self.assertEqual(requests.read_text().splitlines(), [release.API_URL, release.API_URL, release.binary_url('1.1.0')])
        self.assertEqual(self.terminal([launcher, '-v']), (0, b'1.1.0\n'))

        p = subprocess.run([launcher, 'update'], capture_output=True, timeout=8)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(b'Buzz Server 1.1.0 is already current', p.stdout)
        self.assertEqual(p.stderr, b'')

    def test_explicit_update_failure_and_json_contract(self):
        prefix, launcher = self.install('structured')
        download = self.base / 'download'
        download.write_bytes(b'bad')
        code, output = self.terminal([launcher, 'update'])
        self.assertEqual(code, 1, output)
        self.assertIn(b'Downloading and verifying', output)
        self.assertIn(b'Buzz Server update failed [release_refused]', output)
        self.assertNotIn(b'Activating Buzz Server', output)
        self.assertEqual(release.manifest(prefix)['version'], '1.0.0')

        code, output = self.terminal([launcher, '--json', 'update'])
        self.assertEqual(code, 1, output)
        error = json.loads(output)
        self.assertEqual((error['schema_version'], error['operation'], error['ok']), (1, 'update', False))
        self.assertEqual(error['error']['code'], 'release_refused')

        download.write_bytes(self.b.read_bytes())
        code, output = self.terminal([launcher, '--json', 'update'])
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output), dict(schema_version=1, operation='update', ok=True, data={
            'auto_update': True, 'prefix': str(prefix), 'version': '1.1.0', 'updated': True, 'workers_restarted': False,
        }))

    @staticmethod
    def decline(master, output):
        if output.endswith(b'[y/N] '):
            os.write(master, b'no\n')
