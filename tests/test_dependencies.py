"""Offline prerequisite acquisition fixtures; never execute vendor binaries."""
import hashlib
import gzip
import io
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from buzz_server import dependencies as d
from buzz_server.errors import Error
from buzz_server.storage import Store


class DependencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='buzz-dependencies-')
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'state')

    def archive(self, members):
        path = Path(self.temp.name) / 'fixture.tar.gz'
        with tarfile.open(path, 'w:gz') as tar:
            for name, content, kind in members:
                entry = tarfile.TarInfo(name)
                entry.type = kind
                entry.size = len(content) if kind == tarfile.REGTYPE else 0
                tar.addfile(entry, io.BytesIO(content))
        return path

    def test_system_precedence_and_no_ambient_path(self):
        with patch.object(d.shutil, 'which', return_value='/usr/bin/goose') as which:
            self.assertEqual(d.resolve('goose', self.store.root), '/usr/bin/goose')
        which.assert_called_once_with('goose', path=d.SYSTEM_PATH)
        with self.assertRaises(Error):
            d.resolve('arbitrary', self.store.root)

    def test_explicit_refresh_exchanges_complete_directory_at_same_paths(self):
        directory = d.bin_dir(self.store.root, create=True)
        old = directory / "goose"
        old.write_bytes(b"old")
        old.chmod(0o700)
        inode = directory.stat().st_ino

        def acquire(package, directory, **kwargs):
            for name, _ in package["members"].values():
                (directory / name).write_bytes(name.encode())
                (directory / name).chmod(0o700)

        with patch.object(d, "supported"), patch.object(d, "acquire", side_effect=acquire):
            report = d.refresh(self.store, structured=True)
        self.assertNotEqual(directory.stat().st_ino, inode)
        self.assertEqual(report["dependencies_refreshed"], list(d.NAMES))
        self.assertEqual(report["managed_tool_paths"]["goose"], str(old))
        self.assertFalse(report["workers_restarted"])
        self.assertEqual({p.name: p.read_bytes() for p in directory.iterdir()}, {n: n.encode() for n in d.NAMES})

    def test_failed_refresh_retains_original_directory_and_reports_static_error(self):
        directory = d.bin_dir(self.store.root, create=True)
        original = directory / "buzz"
        original.write_bytes(b"old")
        original.chmod(0o700)
        inode = directory.stat().st_ino

        def acquire(package, directory, **kwargs):
            if package["name"] == "goose":
                raise EOFError("synthetic-private-details")
            for name, _ in package["members"].values():
                (directory / name).write_bytes(b"staged")

        with patch.object(d, "supported"), patch.object(d, "acquire", side_effect=acquire):
            with self.assertRaises(Error) as failure:
                d.refresh(self.store, structured=True)
        self.assertEqual(failure.exception.code, "dependency_refused")
        self.assertNotIn("synthetic-private-details", str(failure.exception))
        self.assertEqual(directory.stat().st_ino, inode)
        self.assertEqual(list(directory.iterdir()), [original])
        self.assertEqual(original.read_bytes(), b"old")

    def test_managed_permissions_links_and_unknown_entries_rejected(self):
        directory = d.bin_dir(self.store.root, create=True)
        target = directory / 'goose'
        target.write_bytes(b'synthetic'); target.chmod(0o700)
        with patch.object(d.shutil, 'which', return_value=None):
            self.assertEqual(d.resolve('goose', self.store.root), str(target))
            target.chmod(0o755)
            with self.assertRaises(Error): d.resolve('goose', self.store.root)
            target.unlink(); target.symlink_to('/usr/bin/true')
            with self.assertRaises(Error): d.resolve('goose', self.store.root)
            target.unlink(); (directory / 'foreign').write_bytes(b'')
            with self.assertRaises(Error): d.child_path(self.store.root)

    def test_extract_exact_regular_payload_and_reject_links_duplicates_sizes(self):
        for members in (
            [('./goose', b'abc', tarfile.SYMTYPE)],
            [('./goose', b'abc', tarfile.REGTYPE)] * 2,
            [('./goose', b'ab', tarfile.REGTYPE)],
            [('../goose', b'abc', tarfile.REGTYPE)],
        ):
            with self.subTest(members=members):
                path = self.archive(members)
                with self.assertRaises(Error): d.extract(path, {'./goose': ('goose', 3)}, Path(self.temp.name) / 'stage')
        path = self.archive([('docs/', b'', tarfile.DIRTYPE), ('./goose', b'abc', tarfile.REGTYPE)])
        stage = Path(self.temp.name) / 'good'; stage.mkdir()
        d.extract(path, {'./goose': ('goose', 3)}, stage)
        self.assertEqual((stage / 'goose').read_bytes(), b'abc')

    def test_verify_size_and_digest_before_extract(self):
        path = Path(self.temp.name) / 'asset'; path.write_bytes(b'abc')
        good = dict(size=3, sha256=hashlib.sha256(b'abc').hexdigest())
        d.verify(path, good)
        for spec in (dict(good, size=4), dict(good, sha256='0' * 64)):
            with self.assertRaises(Error): d.verify(path, spec)

    def test_managed_path_cannot_add_extra_search_components(self):
        ambiguous = Path(self.temp.name) / 'ambiguous:relative'
        with self.assertRaises(Error):
            d.bin_dir(ambiguous, create=True)
        self.assertFalse(ambiguous.exists())

    def test_probe_timeout_returns_sanitized_runtime_failure(self):
        import subprocess
        with patch.object(d.subprocess, 'run', side_effect=subprocess.TimeoutExpired('private-probe', 10)):
            with self.assertRaises(Error) as error:
                d.probe('/unused', ['--help'], [], Path(self.temp.name))
        self.assertNotIn('private-probe', str(error.exception))

    def test_no_tty_decline_and_json_never_prompt(self):
        for structured, tty, answer in ((False, False, 'yes'), (True, True, 'yes'), (False, True, 'no')):
            with (patch.object(d, 'paths', return_value=dict.fromkeys(d.NAMES)),
                  patch.object(d, 'interactive', return_value=tty), patch('builtins.input', return_value=answer),
                  patch.object(d, 'acquire') as acquire, patch('sys.stderr', io.StringIO())):
                d.install(self.store, opted=False, skip=False, structured=structured)
                acquire.assert_not_called()

    def test_empty_answer_accepts_and_platform_allowlist_is_exact(self):
        with (patch.object(d, 'paths', return_value=dict.fromkeys(d.NAMES)),
              patch.object(d, 'interactive', return_value=True), patch('builtins.input', return_value=''),
              patch.object(d, 'supported', side_effect=Error('fixture', 'fixture')) as supported,
              patch('sys.stderr', io.StringIO())):
            d.install(self.store, opted=False, skip=False, structured=False)
            supported.assert_called_once()
        with (patch.object(d.platform, 'machine', return_value='x86_64'),
              patch.object(d.os, 'getuid', return_value=1000), patch.object(d.sys, 'platform', 'linux')):
            for version in ('24.04', '26.04', '25.04'):
                with self.subTest(version=version), patch.object(d.platform, 'freedesktop_os_release', return_value={'ID': 'ubuntu', 'VERSION_ID': version}):
                    if version == '25.04':
                        with self.assertRaises(Error): d.supported()
                    else:
                        d.supported()

    def test_system_tools_reused_without_fetch(self):
        with patch.object(d, 'paths', return_value={n: '/usr/bin/' + n for n in d.NAMES}), patch.object(d, 'acquire') as acquire:
            d.install(self.store, opted=True, skip=False, structured=True)
            acquire.assert_not_called()

    def test_platform_refusal(self):
        with patch.object(d.platform, 'machine', return_value='aarch64'), self.assertRaises(Error): d.supported()

    def test_only_missing_fetched_retry_preserves_valid_tools(self):
        directory = d.bin_dir(self.store.root, create=True)
        old = directory / 'buzz'; old.write_bytes(b'keep'); old.chmod(0o700)
        def acquire(package, stage):
            for _, (name, size) in package['members'].items():
                (stage / name).write_bytes(b'x' * size)
                (stage / name).chmod(0o700)
        fixtures = [dict(name='buzz', members={'usr/bin/buzz': ('buzz', 4), 'usr/bin/buzz-acp': ('buzz-acp', 2)}),
                    dict(name='goose', members={'./goose': ('goose', 3)})]
        with (patch.object(d.shutil, 'which', return_value=None), patch.object(d, 'supported'),
              patch.object(d, 'PACKAGES', fixtures), patch.object(d, 'acquire', side_effect=acquire) as fetch):
            result = d.install(self.store, opted=True, skip=False, structured=True)
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(result['dependencies_installed'], ['buzz-acp', 'goose'])
            self.assertEqual(old.read_bytes(), b'keep')
            d.install(self.store, opted=True, skip=False, structured=True)
            self.assertEqual(fetch.call_count, 2)

    def test_staging_failure_publishes_nothing_and_reports_retry(self):
        with (patch.object(d.shutil, 'which', return_value=None), patch.object(d, 'supported'),
              patch.object(d, 'acquire', side_effect=Error('synthetic', 'private-do-not-echo'))):
            result = d.install(self.store, opted=True, skip=False, structured=True)
        self.assertEqual(result['dependencies_installed'], [])
        self.assertIn('Retry install --install-dependencies', result['dependency_error'])
        self.assertNotIn('private-do-not-echo', result['dependency_error'])
        self.assertEqual(list(d.bin_dir(self.store.root).iterdir()), [])

    def test_opted_retry_cleans_only_private_abandoned_staging(self):
        tools = d.bin_dir(self.store.root, create=True).parent
        abandoned = Path(tempfile.mkdtemp(dir=tools, prefix='.stage-'))
        (abandoned / 'archive').write_bytes(b'synthetic partial download')
        keep = tools / 'operator-data'; keep.mkdir()
        (keep / 'retained').write_bytes(b'keep')
        (abandoned / 'link').symlink_to(keep, target_is_directory=True)
        external = tools / '.stage-abcdefgh'; external.symlink_to(keep, target_is_directory=True)
        unsafe = tools / '.stage-ijklmnop'; unsafe.mkdir(mode=0o755); unsafe.chmod(0o755)
        unrelated = tools / '.stage-not-an-installer-stage'; unrelated.mkdir(mode=0o700)
        with (patch.object(d, 'paths', return_value={n: '/usr/bin/' + n for n in d.NAMES}),
              patch.object(d, 'acquire') as acquire):
            d.install(self.store, opted=False, skip=True, structured=True)
            self.assertTrue(abandoned.is_dir())
            result = d.install(self.store, opted=True, skip=False, structured=True)
        acquire.assert_not_called()
        self.assertIsNone(result['dependency_error'])
        self.assertFalse(abandoned.exists())
        self.assertTrue(external.is_symlink())
        self.assertTrue(unsafe.is_dir())
        self.assertTrue(unrelated.is_dir())
        self.assertEqual((keep / 'retained').read_bytes(), b'keep')

    def test_consent_does_not_hold_lock_or_authorize_newly_missing_tools(self):
        import fcntl
        before = {'buzz': '/usr/bin/buzz', 'buzz-acp': '/usr/bin/buzz-acp', 'goose': None}
        after = dict(before, buzz=None)
        held_at_prompt = []
        def answer(_):
            with (self.store.root / '.lock').open('a') as lock:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    held_at_prompt.append(True)
                else:
                    held_at_prompt.append(False)
            return 'yes'
        def acquire(package, stage, *, show_progress=False):
            for name, _ in package['members'].values():
                (stage / name).write_bytes(b'fixture'); (stage / name).chmod(0o700)
        with (patch.object(d, 'paths', side_effect=lambda _: after if held_at_prompt else before),
              patch.object(d, 'interactive', return_value=True), patch('builtins.input', side_effect=answer),
              patch.object(d, 'supported'), patch.object(d, 'acquire', side_effect=acquire) as fetch,
              patch('sys.stderr', io.StringIO())):
            result = d.install(self.store, opted=False, skip=False, structured=False)
        self.assertEqual(held_at_prompt, [False])
        self.assertEqual([call.args[0]['name'] for call in fetch.call_args_list], ['goose'])
        self.assertEqual(result['dependencies_installed'], ['goose'])

    def test_partial_publish_retry_completes_only_missing(self):
        fixtures = [dict(name='goose', members={'./goose': ('goose', 3), 'buzz': ('buzz', 4), 'buzz-acp': ('buzz-acp', 2)})]
        def acquire(package, stage):
            for name, size in package['members'].values():
                (stage / name).write_bytes(b'x' * size); (stage / name).chmod(0o700)
        real_link = os.link
        calls = []
        def interrupted_link(src, dst):
            calls.append(dst)
            if len(calls) == 2:
                raise OSError('synthetic interrupted publish')
            real_link(src, dst)
        with (patch.object(d.shutil, 'which', return_value=None), patch.object(d, 'supported'),
              patch.object(d, 'PACKAGES', fixtures), patch.object(d, 'acquire', side_effect=acquire),
              patch.object(d.os, 'link', side_effect=interrupted_link)):
            first = d.install(self.store, opted=True, skip=False, structured=True)
            self.assertEqual(first['dependencies_installed'], ['buzz'])
            self.assertIsNotNone(first['dependency_error'])
            original = (d.bin_dir(self.store.root) / 'buzz').stat().st_ino
            retry = d.install(self.store, opted=True, skip=False, structured=True)
            self.assertEqual(retry['dependencies_installed'], ['buzz-acp', 'goose'])
            self.assertEqual((d.bin_dir(self.store.root) / 'buzz').stat().st_ino, original)

    def test_download_uses_fixed_trust_and_no_environment_proxies(self):
        import ssl
        target = Path(self.temp.name) / 'download'
        package = dict(url='https://github.com/aaif-goose/goose/releases/download/fixed/file', size=3,
                       sha256=hashlib.sha256(b'abc').hexdigest())
        class Opener:
            def open(self, url, timeout):
                self.url = url
                return io.BytesIO(b'abc')
        with (patch.object(d.urllib.request, 'build_opener', return_value=Opener()) as build,
              patch.dict(os.environ, {'HTTPS_PROXY': 'synthetic-secret', 'SSLKEYLOGFILE': '/invalid/synthetic'}),
              patch.object(d.ssl, 'create_default_context', side_effect=AssertionError('inherits SSLKEYLOGFILE'))):
            d.download(package, target)
        handlers = build.call_args.args
        self.assertEqual(handlers[0].proxies, {})
        self.assertEqual(handlers[2]._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertIsNone(handlers[2]._context.keylog_filename)
        self.assertEqual(target.read_bytes(), b'abc')

    def test_redirects_refuse_other_hosts_http_credentials_and_ports(self):
        from urllib.request import Request
        for url in ('http://github.com/file', 'https://evil.invalid/file', 'https://github.com:444/file',
                    'https://secret@github.com/file'):
            with self.subTest(url=url), self.assertRaises(Error):
                d.Redirect().redirect_request(Request('https://github.com/file'), None, 302, '', {}, url)
        for host in ('github.com', 'release-assets.githubusercontent.com'):
            redirected = d.Redirect().redirect_request(Request('https://github.com/file'), None, 302, '', {}, 'https://' + host + '/asset')
            self.assertEqual(redirected.host, host)

    def test_managed_worker_and_child_path(self):
        from buzz_server import runtime
        directory = d.bin_dir(self.store.root, create=True)
        for name in d.NAMES:
            (directory / name).write_bytes(b'synthetic'); (directory / name).chmod(0o700)
        doc = {'id': 'test'}
        with patch.object(d.shutil, 'which', return_value=None), patch.dict(os.environ, {'PATH': '/untrusted'}):
            self.assertEqual(runtime.tool('buzz-acp', self.store.root), str(directory / 'buzz-acp'))
            env, _ = runtime.goose_context(self.store, doc)
            self.assertEqual(env['PATH'], d.SYSTEM_PATH + ':' + str(directory))

    def test_archive_header_and_decompression_bounds(self):
        path = self.archive([('./goose', b'x' * 4096, tarfile.REGTYPE)])
        with patch.object(d, 'MAX_TAR', 1024), self.assertRaises(Error):
            d.extract(path, {'./goose': ('goose', 4096)}, Path(self.temp.name) / 'stage')

    def test_successful_synthetic_goose_acquisition_probes_before_publish(self):
        path = self.archive([('./goose', b'abc', tarfile.REGTYPE)])
        data = path.read_bytes()
        package = dict(name='goose', url='https://github.com/aaif-goose/goose/releases/download/fixed/file',
                       size=len(data), sha256=hashlib.sha256(data).hexdigest(), members={'./goose': ('goose', 3)})
        def download(spec, target): target.write_bytes(data)
        with (patch.object(d, 'download', side_effect=download), patch.object(d, 'probe') as probe,
              patch.object(d, 'supported'), patch.object(d, 'PACKAGES', [package]),
              patch.object(d.shutil, 'which', side_effect=lambda n, **kw: None if n == 'goose' else '/usr/bin/' + n)):
            result = d.install(self.store, opted=True, skip=False, structured=True)
            self.assertEqual(result['dependencies_installed'], ['goose'])
            self.assertEqual([c.args[1] for c in probe.call_args_list], [['acp', '--help'], ['configure', '--help']])
            self.assertEqual((d.bin_dir(self.store.root) / 'goose').read_bytes(), b'abc')

    def test_skip_suppresses_tty_prompt_and_acquisition(self):
        with (patch.object(d, 'paths', return_value=dict.fromkeys(d.NAMES)),
              patch.object(d, 'interactive', return_value=True), patch('builtins.input') as ask,
              patch.object(d, 'acquire') as acquire):
            d.install(self.store, opted=False, skip=True, structured=False)
            ask.assert_not_called()
            acquire.assert_not_called()

    def test_prompt_plan_shows_reuse_roles_sources_versions_and_paths(self):
        output = io.StringIO()
        with (patch.object(d, 'paths', return_value={'buzz': '/usr/bin/buzz', 'buzz-acp': None, 'goose': None}),
              patch.object(d, 'interactive', return_value=True), patch('builtins.input', return_value='no') as ask,
              patch('sys.stderr', output)):
            d.install(self.store, opted=False, skip=False, structured=False)
        for value in ('/usr/bin/buzz', 'missing', 'ACP adapter', 'desktop-v0.5.30', 'v1.60.2',
                      str(self.store.root / 'tools/bin/goose'), 'no sudo', 'no sudo, desktop installation, services'):
            self.assertIn(value, output.getvalue())
        ask.assert_called_once_with('Install missing tools? [Y/n] ')

    def test_runtime_prepare_selects_managed_adapter_and_buzz(self):
        from buzz_server import runtime
        from buzz_server.storage import atomic
        directory = d.bin_dir(self.store.root, create=True)
        for name in d.NAMES:
            (directory / name).write_bytes(b'synthetic'); (directory / name).chmod(0o700)
        prompt = Path(self.temp.name) / 'prompt'
        atomic(prompt, b'synthetic')
        atomic(self.store.directory('agent') / 'identity.key', b'0' * 63 + b'1')
        doc = {'id': 'agent', 'identity': 'ab' * 32, 'config': {
            'relay_url': 'ws://127.0.0.1:1234', 'owner': 'ab' * 32,
            'profile': {'name': 'Synthetic', 'about': '', 'avatar': ''},
            'prompt_file': str(prompt), 'harness': {'kind': 'goose', 'command': str(directory / 'goose')},
            'provider': {'kind': 'goose-managed'}}}
        with (patch.object(d.shutil, 'which', return_value=None), patch.object(runtime, 'help_check'),
              patch.object(runtime, 'public', return_value=doc['identity'])):
            command, profile, env, _ = runtime.prepare(self.store, doc, snapshot=False)
        self.assertEqual(command[0], str(directory / 'buzz-acp'))
        self.assertEqual(profile[0], str(directory / 'buzz'))
        self.assertEqual(command[command.index('--agent-command') + 1], str(directory / 'goose'))
        self.assertEqual(env['PATH'], d.SYSTEM_PATH + ':' + str(directory))

    def test_doctor_reports_resolved_configured_goose_without_live_operations(self):
        import contextlib
        import json
        from buzz_server import cli
        from buzz_server.storage import atomic
        link = Path(self.temp.name) / 'configured-goose'; link.symlink_to('/usr/bin/true')
        c = {'relay_url': 'ws://127.0.0.1:1234', 'owner': 'ab' * 32,
             'profile': {'name': 'Synthetic', 'about': '', 'avatar': ''},
             'prompt_file': str(Path(self.temp.name) / 'prompt'),
             'harness': {'kind': 'goose', 'command': str(link)}, 'provider': {'kind': 'goose-managed'}}
        config = Path(self.temp.name) / 'config.json'; atomic(config, json.dumps(c).encode())
        output = io.StringIO()
        with (patch.object(cli, 'Systemd'), patch('buzz_server.runtime.tool', side_effect=lambda name, root: '/usr/bin/' + name),
              patch.object(d, 'probe') as probe, patch('buzz_server.identity.public'),
              contextlib.redirect_stdout(output)):
            self.assertEqual(cli.main(['--json', '--state-dir', str(self.store.root), 'doctor', '--config', str(config)]), 0)
        self.assertEqual(json.loads(output.getvalue())['data']['goose_path'], str(Path('/usr/bin/true').resolve()))
        self.assertEqual(probe.call_count, 4)

    def test_probe_sanitizes_environment_and_bounds_output(self):
        import subprocess
        home = self.store.root
        def run(command, **kw):
            self.assertEqual(command, ['/synthetic/goose', 'acp', '--help'])
            self.assertEqual(kw['env']['HOME'], str(home))
            self.assertEqual(kw['env']['PATH'], d.SYSTEM_PATH)
            self.assertNotIn('HTTPS_PROXY', kw['env'])
            self.assertNotIn('OPENAI_API_KEY', kw['env'])
            self.assertEqual(kw['stdin'], subprocess.DEVNULL)
            self.assertEqual(kw['timeout'], 10)
            self.assertIn('preexec_fn', kw)
            kw['stdout'].write(b'synthetic help')
            return subprocess.CompletedProcess(command, 0)
        with (patch.object(d.subprocess, 'run', side_effect=run),
              patch.dict(os.environ, {'OPENAI_API_KEY': 'synthetic-secret', 'HTTPS_PROXY': 'synthetic-secret'})):
            d.probe('/synthetic/goose', ['acp', '--help'], ['synthetic'], home)
        self.assertFalse((home / 'probe-output').exists())

    def test_buzz_deb_reader_and_both_payload_probes_are_bounded(self):
        import subprocess
        source = self.archive([('usr/bin/buzz', b'abc', tarfile.REGTYPE),
                               ('usr/bin/buzz-acp', b'defg', tarfile.REGTYPE)])
        with gzip.open(source, 'rb') as stream:
            data = stream.read()
        package = dict(name='buzz', size=3, sha256=hashlib.sha256(b'deb').hexdigest(),
                       members={'usr/bin/buzz': ('buzz', 3), 'usr/bin/buzz-acp': ('buzz-acp', 4)})
        stage = Path(self.temp.name) / 'stage'; stage.mkdir(mode=0o700)
        def download(spec, target): target.write_bytes(b'deb')
        def run(command, **kw):
            self.assertEqual(command[:2], ['/usr/bin/dpkg-deb', '--fsys-tarfile'])
            self.assertEqual(kw['timeout'], 60)
            self.assertEqual(kw['env'], {'PATH': d.SYSTEM_PATH, 'LANG': 'C.UTF-8'})
            self.assertIn('preexec_fn', kw)
            kw['stdout'].write(data)
            return subprocess.CompletedProcess(command, 0)
        with (patch.object(d, 'download', side_effect=download), patch.object(d.subprocess, 'run', side_effect=run),
              patch.object(d, 'probe') as probe):
            d.acquire(package, stage)
        self.assertEqual([c.args[1] for c in probe.call_args_list], [['users', 'set-profile', '--help'], ['--help']])
        self.assertEqual((stage / 'buzz').read_bytes(), b'abc')
        self.assertEqual((stage / 'buzz-acp').read_bytes(), b'defg')
