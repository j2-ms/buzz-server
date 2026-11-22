"""Offline security contract shared by the standalone bootstrap and updater."""
import inspect
import io
import os
from pathlib import Path
import ssl
import tempfile
import time
import unittest
import urllib.request
from unittest.mock import Mock, patch

from buzz_server import release
from test_bootstrap import bootstrap


class PublicTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='buzz-public-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    @staticmethod
    def member(module, name):
        value = module
        for part in name.split('.'):
            value = getattr(value, part)
        return value

    def test_standalone_transport_parity(self):
        for name in ('binary_url', 'destination', 'Redirect.__init__', 'Redirect.redirect_request', 'Redirect.http_error_302', 'transfer', 'transfer_child', 'fetch'):
            self.assertEqual(inspect.getsource(self.member(bootstrap, name)),
                             inspect.getsource(self.member(release, name)))
        self.assertEqual(bootstrap.API_URL, release.API_URL)
        self.assertEqual(bootstrap.REPO, release.REPO)
        self.assertEqual(release.REPO, 'repos/j2-ms/buzz-server')
        self.assertEqual(bootstrap.REQUEST_SECONDS, release.REQUEST_SECONDS)
        self.assertEqual(bootstrap.MAX_REDIRECTS, release.MAX_REDIRECTS)

    def test_documented_public_contract_matches_product(self):
        root = Path(__file__).resolve().parents[1]
        readme = (root / 'README.md').read_text()
        guide = (root / 'docs/releases.md').read_text()
        checks = (root / 'docs/testing.md').read_text()
        self.assertIn(release.API_URL, guide)
        self.assertIn(release.binary_url('1.2.0').replace('v1.2.0', 'vX.Y.Z'), guide)
        self.assertIn(f'{release.REQUEST_SECONDS}-second total process deadline', guide)
        self.assertEqual(release.MAX_REDIRECTS, 4)
        self.assertIn('at most four redirects', guide)
        for document in (readme, guide):
            self.assertIn('j2-ms/buzz-server', document)
            self.assertNotIn('gh auth login', document)
            self.assertNotIn('gh release download', document)
        self.assertIn('tests/test_public_transport.py', checks)

    def test_exact_browser_identity_and_stable_fields(self):
        import hashlib
        metadata = dict(draft=False, prerelease=False, tag_name='v1.2.0', assets=[dict(
            name=release.ASSET, id=12, size=10, digest='sha256:' + hashlib.sha256(b'x').hexdigest())])
        asset = metadata['assets'][0]
        expected = release.binary_url('1.2.0')
        for module in (release, bootstrap):
            choose = lambda: module.candidate(metadata, '1.0.0') if module is release else module.candidate(metadata)
            asset['browser_download_url'] = expected
            self.assertEqual(choose()['version'], '1.2.0')
            for url in (expected + '?token=secret', expected.replace('github.com', 'evil.test'),
                        expected.replace('v1.2.0', 'v1.3.0'), expected.replace(release.ASSET, 'other.pyz'), None):
                asset['browser_download_url'] = url
                with self.subTest(module=module.__name__, url=url), self.assertRaises((release.Error, bootstrap.Refused)):
                    choose()

    def test_redirect_boundaries_and_finite_chain(self):
        for module in (release, bootstrap):
            request = urllib.request.Request(module.binary_url('1.2.0'))
            for url in ('http://github.com/file', 'https://evil.test/file', 'https://github.com:444/file',
                        'https://user@github.com/file', 'https://:pass@github.com/file',
                        'https://github.com/file#fragment', 'https://github.com:bad/file',
                        'https://github.com/\\evil', 'https://github.com/file\nheader'):
                handler = module.Redirect(('github.com', 'release-assets.githubusercontent.com'), time.monotonic() + 10)
                with self.subTest(url=url), self.assertRaises((release.Error, bootstrap.Refused)):
                    handler.redirect_request(request, None, 302, '', {}, url)
            handler = module.Redirect(('github.com', 'release-assets.githubusercontent.com'), time.monotonic() + 10)
            for _ in range(module.MAX_REDIRECTS):
                self.assertIsNotNone(handler.redirect_request(request, None, 302, '', {},
                                      'https://release-assets.githubusercontent.com/file?signature=synthetic'))
            with self.assertRaises((release.Error, bootstrap.Refused)):
                handler.redirect_request(request, None, 302, '', {}, 'https://github.com/file')
            handler = module.Redirect(('api.github.com',), time.monotonic() + 10)
            with self.assertRaises((release.Error, bootstrap.Refused)):
                handler.redirect_request(request, None, 302, '', {}, 'https://github.com/file')
            handler = module.Redirect(('github.com',), time.monotonic() - 1)
            with self.assertRaises((release.Error, bootstrap.Refused)):
                handler.redirect_request(request, None, 302, '', {}, 'https://github.com/file')

    def test_redirect_bodies_are_closed_without_unbounded_reads(self):
        for module in (release, bootstrap):
            handler = module.Redirect(('github.com', 'release-assets.githubusercontent.com'), time.monotonic() + 10)
            handler.parent = Mock()
            request = urllib.request.Request(module.binary_url('1.2.0'))
            fp = Mock()
            handler.http_error_302(request, fp, 302, '', {'Location': 'https://release-assets.githubusercontent.com/file'})
            fp.close.assert_called_once()
            fp.read.assert_not_called()
            self.assertEqual(handler.parent.open.call_args.args[0].full_url,
                             'https://release-assets.githubusercontent.com/file')
            handler.parent.reset_mock()
            for location in ('https://evil.test/file', ' https://github.com/file', 'https://github.com/file\nheader'):
                fp = Mock()
                with self.assertRaises((release.Error, bootstrap.Refused)):
                    handler.http_error_302(request, fp, 302, '', {'Location': location})
                fp.close.assert_called_once()
                fp.read.assert_not_called()
            handler.parent.open.assert_not_called()

    def transport(self, module, data, limit, *, status=200, encoding='identity'):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.status = status
        response.headers = {'Content-Encoding': encoding}
        response.geturl.return_value = module.API_URL
        stream = io.BytesIO(data)
        response.read1.side_effect = stream.read1
        opener = Mock()
        opener.open.return_value = response
        target = self.base / ('body-' + str(len(list(self.base.iterdir()))))
        with (patch.object(module.urllib.request, 'build_opener', return_value=opener) as build,
              patch.dict(os.environ, {'HTTPS_PROXY': 'https://invalid.test', 'GH_TOKEN': 'synthetic',
                                     'SSL_CERT_FILE': '/invalid', 'REQUESTS_CA_BUNDLE': '/invalid'})):
            module.transfer(module.API_URL, target, limit)
        handlers = build.call_args.args
        self.assertEqual(handlers[0].proxies, {})
        context = handlers[2]._context
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, module.API_URL)
        self.assertFalse(any(k.lower() in ('authorization', 'cookie', 'proxy-authorization') for k in request.headers))
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        return target

    def test_tls_proxy_auth_byte_limit_and_http_status(self):
        for module in (release, bootstrap):
            self.assertEqual(self.transport(module, b'{}', 2).read_bytes(), b'{}')
            for kw in (dict(data=b'xxx', limit=2), dict(data=b'{}', limit=2, status=403),
                       dict(data=b'{}', limit=2, encoding='gzip')):
                with self.subTest(module=module.__name__, kw=kw), self.assertRaises((release.Error, bootstrap.Refused)):
                    self.transport(module, **kw)
        self.assertTrue(all(p.stat().st_size <= 2 for p in self.base.iterdir()))

    def test_total_deadline_kills_blocked_transport_and_sanitizes_failure(self):
        for module in (release, bootstrap):
            target = self.base / module.__name__
            def blocked(*args):
                time.sleep(2)
                target.write_bytes(b'too late')
            start = time.monotonic()
            with patch.object(module, 'transfer', side_effect=blocked), patch.object(module, 'REQUEST_SECONDS', 0.03):
                with self.assertRaises((release.Error, bootstrap.Refused)):
                    module.fetch(module.API_URL, target, 10)
            self.assertLess(time.monotonic() - start, 1)
            self.assertFalse(target.exists())
            with patch.object(module, 'transfer', side_effect=ValueError('synthetic secret')):
                with self.assertRaises((release.Error, bootstrap.Refused)) as error:
                    module.fetch(module.API_URL, target, 10)
            self.assertNotIn('synthetic secret', str(error.exception))

    def test_transport_child_scrubs_environment_without_credentials(self):
        import json
        for module in (release, bootstrap):
            target = self.base / module.__name__
            def injected(url, path, limit):
                path.write_text(json.dumps(dict(os.environ)))
            with (patch.object(module, 'transfer', side_effect=injected),
                  patch.dict(os.environ, {'GH_TOKEN': 'synthetic', 'HTTPS_PROXY': 'synthetic',
                                         'GH_CONFIG_DIR': '/invalid', 'SSL_CERT_FILE': '/invalid'})):
                module.fetch(module.API_URL, target, 1024)
            self.assertEqual(json.loads(target.read_text()),
                             dict(PATH='/usr/local/bin:/usr/bin:/bin', LANG='C.UTF-8'))

    def test_initial_endpoints_and_exclusive_files(self):
        for module in (release, bootstrap):
            for url in ('https://evil.test', module.API_URL + '?override=1',
                        module.binary_url('1.0.0') + '?override=1',
                        module.binary_url('1.0.0').replace('v1.0.0', 'v01.0.0')):
                with patch.object(module, 'transfer') as transfer, self.assertRaises((release.Error, bootstrap.Refused)):
                    module.fetch(url, self.base / 'unused', 10)
                transfer.assert_not_called()
            target = self.base / 'keep'
            target.write_bytes(b'keep')
            with patch.object(module.urllib.request, 'build_opener'), self.assertRaises(FileExistsError):
                module.transfer(module.API_URL, target, 10)
            self.assertEqual(target.read_bytes(), b'keep')
