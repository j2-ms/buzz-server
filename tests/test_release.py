"""Offline release regression tests. All releases and credentials are synthetic."""
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch, Mock

from buzz_server import release
from buzz_server.errors import Error
from buzz_server.storage import Store, atomic, read_json, write_json
import test_cli


class ConfigurationTests(test_cli.CliTests):
    def test_goose_command_reconfigure_keeps_identity_and_private_state(self):
        identity = self.create()['identity']
        state = self.root / 'agents/agent/home/.config/opaque-goose-settings'
        atomic(state, b'synthetic-private-settings')
        self.c['harness']['command'] = '/usr/bin/false'
        self.write_config(self.c)
        result = self.call('configure', 'agent', '--config', str(self.config_path))
        self.assertEqual(result['identity'], identity)
        self.assertEqual(result['configuration']['harness'], 'goose')
        self.assertEqual(result['configuration']['provider'], 'goose-managed')
        self.assertIsNone(result['configuration']['model'])
        self.assertEqual(state.read_bytes(), b'synthetic-private-settings')


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='buzz-release-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.prefix = self.base / 'install'
        self.state = self.base / 'state'
        self.units = self.base / 'units'
        self.source = Path(__file__).resolve().parents[1]
        self.a = self.base / 'a.pyz'
        self.b = self.base / 'b.pyz'
        release.build(self.source, self.a, '1.0.0')
        release.build(self.source, self.b, '1.2.0')

    def install(self, artifact=None):
        return release.install(artifact or self.a, self.prefix, self.state, self.units)

    def test_default_user_command_lifecycle_and_collision(self):
        home = self.base / 'home'
        home.mkdir(mode=0o700)
        prefix = home / '.local/buzz-server-cli'
        command = home / '.local/bin/buzz-server'
        env = dict(os.environ, HOME=str(home), PATH=str(home / '.local/bin') + ':/usr/bin:/bin')
        with patch.dict(os.environ, {'HOME': str(home)}):
            absent = subprocess.run(['/bin/sh', '-c', 'command -v buzz-server'], env=env, capture_output=True)
            self.assertNotEqual(absent.returncode, 0)
            release.install(self.a, prefix, self.state, self.units)
            self.assertEqual(os.readlink(command), str(prefix / 'bin/buzz-server'))
            self.assertEqual((prefix / 'bin/buzz-server').stat().st_mode & 0o777, 0o700)
            self.assertEqual((prefix / 'active/app.pyz').stat().st_mode & 0o777, 0o600)
            result = subprocess.run(['/bin/sh', '-c', 'buzz-server --version'], env=env, capture_output=True, timeout=5)
            self.assertEqual((result.returncode, result.stdout.strip()), (0, b'1.0.0'))
            release.install(self.b, prefix, self.state, self.units)
            result = subprocess.run(['/bin/sh', '-c', 'buzz-server --version'], env=env, capture_output=True, timeout=5)
            self.assertEqual((result.returncode, result.stdout.strip()), (0, b'1.2.0'))
            release.uninstall(prefix)
            self.assertFalse(command.is_symlink())
            command.write_text('unrelated')
            with self.assertRaises(Error):
                release.install(self.a, prefix, self.state, self.units)
            self.assertEqual(command.read_text(), 'unrelated')
            command.unlink()
            release.install(self.a, prefix, self.state, self.units)
            command.unlink()
            command.write_text('another command replaced the managed link')
            release.uninstall(prefix)
            self.assertEqual(command.read_text(), 'another command replaced the managed link')
            self.assertFalse((prefix / 'bin/buzz-server').exists())

    def test_fresh_install_enables_interactive_offers(self):
        self.assertTrue(self.install()['auto_update'])

    def test_standard_bin_repair_revalidates_the_directory_after_consent(self):
        home = self.base / 'home'
        directory = home / '.local/bin'
        directory.mkdir(parents=True)
        directory.chmod(0o775)
        moved = directory.with_name('original')

        def replace_during_prompt():
            directory.rename(moved)
            directory.mkdir()
            directory.chmod(0o775)
            return 'yes'

        with (patch.dict(os.environ, {'HOME': str(home)}),
              patch('builtins.input', side_effect=replace_during_prompt),
              patch('sys.stderr', io.StringIO())):
            with self.assertRaisesRegex(Error, 'writable by group or others'):
                release.install(self.a, home / '.local/buzz-server-cli', self.state, self.units, offer_repair=True)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o775)
        self.assertEqual(moved.stat().st_mode & 0o777, 0o775)
        self.assertFalse((directory / 'buzz-server').exists())

    def test_standard_bin_refuses_foreign_owner_before_permission_consent(self):
        home = self.base / 'home'
        directory = home / '.local/bin'
        directory.mkdir(parents=True)
        directory.chmod(0o775)
        real_stat = os.stat

        def foreign(path, **kwargs):
            info = real_stat(path, **kwargs)
            if path == 'bin' and kwargs.get('dir_fd') is not None:
                info = os.stat_result((*info[:4], os.getuid() + 1, *info[5:]))
            return info

        with (patch.dict(os.environ, {'HOME': str(home)}), patch.object(release.os, 'stat', side_effect=foreign),
              patch('builtins.input') as consent):
            with self.assertRaisesRegex(Error, 'belongs to UID') as refusal:
                release.install(self.a, home / '.local/buzz-server-cli', self.state, self.units, offer_repair=True)
        self.assertIn(str(directory), str(refusal.exception))
        self.assertNotIn('chmod', str(refusal.exception))
        self.assertEqual(directory.stat().st_mode & 0o777, 0o775)
        consent.assert_not_called()

    def test_deterministic_build_and_version_offline(self):
        other = self.base / 'other.pyz'
        release.build(self.source, other, '1.0.0')
        self.assertEqual(self.a.read_bytes(), other.read_bytes())
        p = subprocess.run(['/usr/bin/python3', '-I', str(self.a), '--version'], capture_output=True, timeout=5)
        self.assertEqual(p.stdout.strip(), b'1.0.0')
        self.assertEqual(p.returncode, 0)

    def test_reinstall_upgrade_preserve_payload_and_state(self):
        self.install()
        launcher = (self.prefix / 'bin/buzz-server').read_bytes()
        first = (self.prefix / 'active').resolve()
        Store(self.state)
        atomic(self.state / 'agents/retained/identity.key', b'synthetic')
        self.install()
        self.install(self.b)
        self.assertEqual((self.prefix / 'bin/buzz-server').read_bytes(), launcher)
        self.assertTrue(first.is_dir())
        pinned = subprocess.run(['/usr/bin/python3', '-I', str(first / 'app.pyz'), '--version'], capture_output=True, timeout=5)
        self.assertEqual(pinned.stdout.strip(), b'1.0.0')
        self.assertEqual((self.state / 'agents/retained/identity.key').read_bytes(), b'synthetic')
        p = subprocess.run([str(self.prefix / 'bin/buzz-server'), '--version'], capture_output=True, timeout=5)
        self.assertEqual(p.stdout.strip(), b'1.2.0')
        with self.assertRaises(Error):
            self.install(self.a)

    def test_interrupted_activation_uses_old_version_and_can_retry(self):
        self.install()
        active = self.prefix / 'active'
        old = active.readlink()
        self.install(self.b)
        pending = active.readlink()
        active.unlink()
        active.symlink_to(old)
        (self.prefix / '.activate').symlink_to(pending)
        self.assertEqual(release.manifest(self.prefix)['version'], '1.0.0')
        self.install(self.b)
        self.assertEqual(active.readlink(), pending)
        self.assertFalse((self.prefix / '.activate').is_symlink())

    def test_foreign_conflict_has_no_mutation(self):
        self.prefix.mkdir(mode=0o700)
        atomic(self.prefix / 'foreign', b'keep')
        before = sorted(self.prefix.rglob('*'))
        with self.assertRaises(Error):
            self.install()
        self.assertEqual(sorted(self.prefix.rglob('*')), before)

    def test_symlinks_and_corruption_refused(self):
        self.prefix.symlink_to(self.base / 'target')
        with self.assertRaises(Error):
            self.install()
        self.prefix.unlink()
        self.install()
        previous = (self.prefix / 'active').readlink()
        self.b.write_bytes(b'corrupt')
        with self.assertRaises(Error):
            self.install(self.b)
        self.assertEqual((self.prefix / 'active').readlink(), previous)

    def metadata(self, path=None, tag='v1.2.0', **extra):
        path = path or self.b
        import hashlib
        return dict(tag_name=tag, draft=False, prerelease=False, assets=[dict(id=123, name=release.ASSET,
                    size=path.stat().st_size, digest='sha256:' + hashlib.sha256(path.read_bytes()).hexdigest())], **extra)

    def test_numeric_release_selection_and_untrusted_metadata(self):
        self.assertGreater(release.version('v1.10.0'), release.version('v1.9.0'))
        for tag in ('v1.0', 'v01.2.3', 'v1.2.3-rc1', 'https://evil/v1.2.3'):
            with self.assertRaises(Error):
                release.version(tag)
        for change in ({'draft': True}, {'prerelease': True}, {'tag_name': 'v0.9.0'}):
            m = self.metadata(); m.update(change)
            self.assertIsNone(release.candidate(m, '1.0.0'))
        for digest in (None, '', 'sha256:bad'):
            m = self.metadata(); m['assets'][0]['digest'] = digest
            with self.assertRaises(Error):
                release.candidate(m, '1.0.0')

    def test_verified_update_and_failure_keep_previous(self):
        self.install()
        old = (self.prefix / 'active').readlink()
        def fake_fetch(args, target, limit):
            target.write_bytes(json.dumps(self.metadata()).encode() if 'releases/latest' in args else b'corrupt')
        with patch.object(release, 'fetch', side_effect=fake_fetch):
            with self.assertRaises(Error):
                release.update(self.prefix)
        self.assertEqual((self.prefix / 'active').readlink(), old)
        def good_fetch(args, target, limit):
            target.write_bytes(json.dumps(self.metadata()).encode() if 'releases/latest' in args else self.b.read_bytes())
        with patch.object(release, 'fetch', side_effect=good_fetch):
            self.assertEqual(release.update(self.prefix)['version'], '1.2.0')
        self.assertNotEqual((self.prefix / 'active').readlink(), old)
        self.assertTrue(read_json(self.prefix / 'installation.json')['auto_update'])
        release.set_auto(self.prefix, True)
        self.assertTrue(read_json(self.prefix / 'installation.json')['auto_update'])
        release.set_auto(self.prefix, False)

    def test_mismatched_download_version_refused(self):
        self.install()
        metadata = self.metadata(self.a)
        def fake(args, target, limit):
            target.write_bytes(json.dumps(metadata).encode() if 'releases/latest' in args else self.a.read_bytes())
        with patch.object(release, 'fetch', side_effect=fake), self.assertRaises(Error):
            release.update(self.prefix)
        self.assertEqual(read_json(self.prefix / 'installation.json')['version'], '1.0.0')

    def test_uninstall_preflight_all_units_before_stop(self):
        self.install()
        atomic(self.units / 'buzz-server-a.service', b'foreign')
        supervisor = Mock()
        supervisor.read_owned.side_effect = Error('unit_conflict', 'synthetic')
        with patch.object(release, 'Systemd', return_value=supervisor), self.assertRaises(Error):
            release.uninstall(self.prefix)
        supervisor.stop.assert_not_called()
        self.assertTrue((self.prefix / 'active').exists())

    def test_uninstall_reinstall_retains_private_data_and_foreign_file(self):
        self.install()
        Store(self.state)
        atomic(self.state / 'agents/retained/identity.key', b'synthetic')
        atomic(self.prefix / 'foreign', b'keep')
        with self.assertRaises(Error):
            release.uninstall(self.prefix)
        self.assertEqual((self.prefix / 'foreign').read_bytes(), b'keep')
        (self.prefix / 'foreign').unlink()
        release.uninstall(self.prefix)
        self.assertFalse((self.prefix / 'bin/buzz-server').exists())
        self.install()
        self.assertEqual((self.state / 'agents/retained/identity.key').read_bytes(), b'synthetic')

    def test_uninstall_with_obsolete_definition_retains_identity_and_disables_unit(self):
        self.install()
        store = Store(self.state)
        identity = 'ab' * 32
        doc = {'version': 1, 'id': 'agent', 'identity': identity, 'desired': 'running', 'revision': 1,
               'config': {'relay_url': 'wss://example.test', 'owner': '12' * 32,
                          'profile': {'name': 'Synthetic', 'about': '', 'avatar': ''}, 'prompt_file': '/unused',
                          'provider': {'kind': 'goose-openai', 'model': 'synthetic',
                                       'command': '/unused', 'env_file': '/unused'}}}
        store.save(doc)
        atomic(store.directory('agent') / 'identity.key', b'synthetic')
        atomic(self.units / 'buzz-server-agent.service', b'synthetic')
        supervisor = Mock()
        with patch.object(release, 'Systemd', return_value=supervisor):
            self.assertTrue(release.uninstall(self.prefix)['uninstalled'])
        supervisor.stop.assert_called_once_with('agent')
        supervisor.remove.assert_called_once_with('agent')
        self.assertEqual(store.load('agent'), dict(doc, desired='stopped'))
        self.assertEqual((store.directory('agent') / 'identity.key').read_bytes(), b'synthetic')

    def test_rollback_requires_uninstall_and_preserves_state_binding(self):
        self.install()
        self.install(self.b)
        release.set_auto(self.prefix, True)
        with self.assertRaises(Error):
            self.install()
        Store(self.state)
        identity = self.state / 'agents/retained/identity.key'
        atomic(identity, b'synthetic')
        release.uninstall(self.prefix)
        with self.assertRaises(Error):
            release.install(self.a, self.prefix, self.base / 'other-state', self.units)
        result = self.install()
        self.assertEqual(result['version'], '1.0.0')
        self.assertFalse(result['auto_update'])
        self.assertEqual(identity.read_bytes(), b'synthetic')

    def test_install_reports_missing_runtime_tools_without_starting_them(self):
        from buzz_server import cli
        out, err = io.StringIO(), io.StringIO()
        import contextlib
        with (patch.object(cli.sys, 'argv', [str(self.a)]),
              patch.object(cli.shutil, 'which', side_effect=lambda name, **_: '/usr/bin/buzz' if name == 'buzz' else None),
              patch.object(subprocess, 'run') as run,
              contextlib.redirect_stdout(out), contextlib.redirect_stderr(err)):
            code = cli.main(['--json', '--state-dir', str(self.state), '--unit-dir', str(self.units),
                             'install', '--prefix', str(self.prefix)])
        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue(), '')
        self.assertEqual(json.loads(out.getvalue())['data']['missing_runtime_tools'], ['buzz-acp', 'goose'])
        self.assertEqual(json.loads(out.getvalue())['data']['runtime_tool_paths'],
                         {'buzz': '/usr/bin/buzz', 'buzz-acp': None, 'goose': None})
        run.assert_not_called()

    def test_install_guidance_shows_next_step_without_manual_goose_path(self):
        from buzz_server import cli
        data = dict(version='1.0.0', prefix=str(self.prefix), missing_runtime_tools=['buzz'],
                    runtime_tool_paths={'buzz': None, 'buzz-acp': '/usr/bin/buzz-acp', 'goose': '/usr/local/bin/goose'})
        text = cli.render('install', data)
        self.assertNotIn('/usr/local/bin/goose', text)
        self.assertNotIn('harness.command', text)
        self.assertIn('https://github.com/block/buzz/releases/tag/desktop-v0.5.30', text)
        self.assertIn('buzz-server doctor', text)
        self.assertIn('custom prefix', text)

    def offer(self, answer='yes\n', **kwargs):
        if os.getuid() == 0:
            self.skipTest('Interactive update offers require an unprivileged account')
        with (patch.object(release.sys, 'argv', [str((self.prefix / 'active/app.pyz').resolve())]),
              patch.object(release.sys, 'stdin', io.StringIO(answer)),
              patch.object(release.sys, 'stderr', io.StringIO())):
            release.before_command(self.prefix, kwargs.get('operation', 'list'),
                                   kwargs.get('structured', False), kwargs.get('tty', True),
                                   kwargs.get('state', self.state), self.units, ['list'], now=100000)

    def good_download(self, args, target, limit):
        with release.locked(self.prefix, wait=0):
            pass
        target.write_bytes(json.dumps(self.metadata()).encode() if 'releases/latest' in args else self.b.read_bytes())

    def test_consent_accept_exact_artifact_and_argv(self):
        self.install()
        with patch.object(release, 'fetch', side_effect=self.good_download) as fetched, patch.object(release.os, 'execv') as exe:
            self.offer()
        self.assertEqual(fetched.call_count, 2)
        path = (self.prefix / 'active/app.pyz').resolve()
        exe.assert_called_once_with('/usr/bin/python3', ['/usr/bin/python3', '-I', str(path), 'list'])
        self.assertEqual(path.read_bytes(), self.b.read_bytes())

    def test_declines_outages_and_exclusions(self):
        for answer in ('', 'no\n', 'maybe\n'):
            with self.subTest(answer=answer):
                self.install()
                (self.prefix / 'update-cache.json').unlink(missing_ok=True)
                with patch.object(release, 'fetch', side_effect=self.good_download) as fetched, patch.object(release.os, 'execv') as exe:
                    self.offer(answer)
                    self.offer(answer)
                self.assertEqual(fetched.call_count, 1)
                exe.assert_not_called()
        for options in ({'structured': True}, {'tty': False}, {'operation': 'version'},
                        {'operation': 'update'}, {'operation': '_worker'}, {'state': self.base / 'wrong'}):
            (self.prefix / 'update-cache.json').unlink(missing_ok=True)
            with patch.object(release, 'fetch') as fetched:
                self.offer(**options)
                fetched.assert_not_called()
        with patch.object(release, 'fetch', side_effect=Error('offline', 'synthetic')) as fetched:
            self.offer()
            self.offer()
        self.assertEqual(fetched.call_count, 1)

    def test_consent_races_revalidate_without_locking_input_or_network(self):
        for race in ('off', 'upgrade', 'binding', 'tamper'):
            with self.subTest(race=race):
                # Each race starts from the same installed version.
                other = self.base / ('prefix-' + race)
                self.prefix = other
                self.install()
                def download(directory, selected):
                    with release.locked(self.prefix, wait=0):
                        pass
                    target = directory / release.ASSET
                    target.write_bytes(self.b.read_bytes())
                    if race == 'off':
                        release.set_auto(self.prefix, False)
                    elif race == 'upgrade':
                        self.install(self.b)
                    elif race == 'binding':
                        m = release.manifest(self.prefix)
                        m['state'] = str(self.base / 'changed')
                        write_json(self.prefix / 'installation.json', m)
                    else:
                        target.write_bytes(self.a.read_bytes())
                    return target
                with (patch.object(release, 'fetch', side_effect=self.good_download),
                      patch.object(release, 'download', side_effect=download), patch.object(release.os, 'execv') as exe):
                    self.offer()
                exe.assert_not_called()

    def test_reading_consent_is_unlocked_and_existing_off_is_preserved(self):
        self.install()
        release.set_auto(self.prefix, False)
        self.assertFalse(self.install(self.b)['auto_update'])
        with patch.object(release, 'fetch') as fetched:
            self.offer()
        fetched.assert_not_called()
        # Resume from a fresh prefix to exercise an actual consent read.
        self.prefix = self.base / 'read-prefix'
        self.install()
        test = self
        class Consent(io.StringIO):
            def readline(self):
                with release.locked(test.prefix, wait=0):
                    pass
                return 'no\n'
        with (patch.object(release.sys, 'argv', [str((self.prefix / 'active/app.pyz').resolve())]),
              patch.object(release.sys, 'stdin', Consent()), patch.object(release.sys, 'stderr', io.StringIO()),
              patch.object(release, 'fetch', side_effect=self.good_download)):
            release.before_command(self.prefix, 'list', False, True, self.state, self.units, ['list'])

    def test_installed_corruption_rolls_back_before_commit(self):
        self.install()
        original = (self.prefix / 'active').readlink()
        real_atomic = release.atomic
        def corrupt(path, data):
            if path.name == 'app.pyz':
                data = self.a.read_bytes()
            real_atomic(path, data)
        with (patch.object(release, 'fetch', side_effect=self.good_download),
              patch.object(release, 'atomic', side_effect=corrupt), patch.object(release.os, 'execv') as exe):
            self.offer()
        exe.assert_not_called()
        self.assertEqual((self.prefix / 'active').readlink(), original)
        release.inventory(self.prefix, release.manifest(self.prefix))

    def test_root_and_invalid_installed_artifact_never_offer(self):
        self.install()
        with patch.object(release.os, 'getuid', return_value=0), patch.object(release, 'fetch') as fetched:
            release.before_command(self.prefix, 'list', False, True, self.state, self.units, ['list'])
        fetched.assert_not_called()
        (self.prefix / 'active/app.pyz').write_bytes(b'corrupt')
        with patch.object(release, 'fetch') as fetched:
            self.offer()
        fetched.assert_not_called()

    def test_exec_failure_is_not_successful_old_dispatch(self):
        self.install()
        with (patch.object(release, 'fetch', side_effect=self.good_download),
              patch.object(release.os, 'execv', side_effect=OSError('synthetic')), self.assertRaises(OSError)):
            self.offer()
        self.assertEqual(release.manifest(self.prefix)['version'], '1.2.0')

    def test_cli_exec_failure_never_dispatches_old_command(self):
        from buzz_server import cli
        self.install()
        class Terminal(io.StringIO):
            def isatty(self):
                return True
        with (patch.object(release.sys, 'argv', [str((self.prefix / 'active/app.pyz').resolve())]),
              patch.object(release.sys, 'stdin', Terminal('yes\n')),
              patch.object(release.sys, 'stdout', Terminal()),
              patch.object(release.sys, 'stderr', Terminal()) as err,
              patch.object(release, 'fetch', side_effect=self.good_download),
              patch.object(release.os, 'execv', side_effect=OSError('synthetic')),
              patch.object(cli, 'execute') as execute):
            result = cli.main(['--state-dir', str(self.state), '--unit-dir', str(self.units), 'list'])
        self.assertEqual(result, 1)
        self.assertIn('local_failure', err.getvalue())
        execute.assert_not_called()
        self.assertEqual(release.manifest(self.prefix)['version'], '1.2.0')

    def test_interrupt_propagates_before_dispatch(self):
        self.install()
        with patch.object(release, 'latest', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.offer()

    def test_activation_failure_and_concurrent_operation(self):
        self.install()
        old = (self.prefix / 'active').readlink()
        real_replace = release.os.replace
        def fail_link(src, dest):
            if Path(src).name == '.activate':
                raise OSError('synthetic activation failure')
            return real_replace(src, dest)
        with patch.object(release.os, 'replace', side_effect=fail_link), self.assertRaises(OSError):
            self.install(self.b)
        self.assertEqual((self.prefix / 'active').readlink(), old)
        self.assertEqual(read_json(self.prefix / 'installation.json')['version'], '1.0.0')
        with release.locked(self.prefix):
            with self.assertRaises(Error):
                with release.locked(self.prefix, wait=0):
                    self.fail('concurrent lock acquired')
        self.install(self.b)

    def test_old_probe_never_contacts_network_or_activates(self):
        self.install()
        with patch.object(release, 'fetch') as fetched:
            release.probe(self.prefix)
        fetched.assert_not_called()
        self.assertEqual(release.manifest(self.prefix)['version'], '1.0.0')

    def test_explicit_update_json_errors_are_sanitized(self):
        from buzz_server import cli
        self.install()
        out, err = io.StringIO(), io.StringIO()
        import contextlib
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), patch.object(release, 'fetch', side_effect=Error('release_refused', 'Sanitized failure.')):
            code = cli.main(['--json', 'update', '--prefix', str(self.prefix)])
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), '')
        self.assertEqual(json.loads(err.getvalue())['operation'], 'update')
        with patch.object(release, 'fetch') as fetched:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(['--json', 'update', '--prefix', str(self.prefix), '--auto', 'off']), 0)
            fetched.assert_not_called()

    def test_managed_unit_stable_across_upgrade(self):
        from buzz_server.supervisor import Systemd
        self.install()
        with patch.object(release.sys, 'argv', [str((self.prefix / 'active/app.pyz').resolve())]):
            supervisor = Systemd(Store(self.state), self.units)
            before = supervisor.content('agent')
        self.install(self.b)
        with patch.object(release.sys, 'argv', [str((self.prefix / 'active/app.pyz').resolve())]):
            self.assertEqual(before, supervisor.content('agent'))
        self.assertIn(str(self.prefix / 'bin/buzz-server').encode(), before)
        self.assertNotIn(b'payloads', before)

    def test_uninstall_stop_failure_preserves_install_and_units(self):
        self.install()
        for ident in ('a', 'b'):
            atomic(self.units / ('buzz-server-' + ident + '.service'), b'synthetic')
        supervisor = Mock()
        supervisor.stop.side_effect = Error('stop_unverified', 'synthetic')
        with patch.object(release, 'Systemd', return_value=supervisor), self.assertRaises(Error):
            release.uninstall(self.prefix)
        self.assertEqual(supervisor.read_owned.call_count, 2)
        self.assertEqual(supervisor.inspect.call_count, 2)
        supervisor.remove.assert_not_called()
        self.assertTrue((self.prefix / 'bin/buzz-server').exists())
        self.assertTrue((self.units / 'buzz-server-b.service').exists())

class LaunchTests(unittest.TestCase):
    def test_managed_launch_excludes_caller_credentials_and_overrides(self):
        import os
        from buzz_server.runtime import prepare
        with tempfile.TemporaryDirectory(prefix='buzz-launch-') as temp:
            root = Path(temp)
            store = Store(root / 'state')
            directory = store.directory('agent')
            atomic(directory / 'identity.key', b'0' * 63 + b'1')
            atomic(root / 'prompt', b'synthetic')
            c = {'relay_url': 'ws://127.0.0.1:1234', 'owner': 'ab' * 32,
                 'profile': {'name': 'Synthetic', 'about': '', 'avatar': ''},
                 'prompt_file': str(root / 'prompt'),
                 'harness': {'kind': 'goose', 'command': '/usr/bin/true'},
                 'provider': {'kind': 'goose-managed'}}
            doc = {'id': 'agent', 'identity': 'ab' * 32, 'config': c}
            ambient = {key: 'synthetic-private' for key in (
                'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'GOOSE_PROVIDER', 'GOOSE_MODEL',
                'GOOSE_DISABLE_KEYRING', 'BUZZ_PRIVATE_KEY', 'LD_PRELOAD')}
            with (patch.dict(os.environ, ambient),
                  patch('buzz_server.runtime.public', return_value=doc['identity']),
                  patch('buzz_server.runtime.tool', return_value='/usr/bin/true'),
                  patch('buzz_server.runtime.help_check')):
                command, env, _ = prepare(store, doc)
            for key in ambient.keys() - {'GOOSE_DISABLE_KEYRING', 'BUZZ_PRIVATE_KEY'}:
                self.assertNotIn(key, env)
            self.assertEqual(env['GOOSE_DISABLE_KEYRING'], '1')
            self.assertEqual(env['BUZZ_PRIVATE_KEY'], '0' * 63 + '1')
            self.assertNotIn('--model', command)
            self.assertEqual(command[command.index('--agent-command') + 1], '/usr/bin/true')
            self.assertEqual((directory / 'identity.key').read_bytes(), b'0' * 63 + b'1')
