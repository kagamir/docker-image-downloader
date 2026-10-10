import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
from urllib3.exceptions import ProtocolError

import registry_parallel_pull as app


def digest(data):
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def response(status=200, data=b'', headers=None):
    result = Mock()
    result.status_code = status
    result.headers = headers or {}
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    result.raw.stream.return_value = [data]
    result.json.return_value = json.loads(data) if data.startswith(b'{') else {}
    if status >= 400:
        result.raise_for_status.side_effect = requests.HTTPError(str(status))
    return result


class ImageTests(unittest.TestCase):
    def test_supported_references(self):
        cases = {
            'nginx': ('docker.io', 'library/nginx', 'latest', 'nginx:latest'),
            'docker.io/library/nginx:1.2': ('docker.io', 'library/nginx', '1.2', 'nginx:1.2'),
            'index.docker.io/user/img:Tag': ('docker.io', 'user/img', 'Tag', 'user/img:Tag'),
            'ghcr.io/owner/image:v1': ('ghcr.io', 'owner/image', 'v1', 'ghcr.io/owner/image:v1'),
            'user/a__b-c.d:v1': ('docker.io', 'user/a__b-c.d', 'v1', 'user/a__b-c.d:v1'),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(app.parse_image(text), expected)

    def test_reject_invalid_references(self):
        for text in ('', 'UPPER/image', 'nginx:', 'nginx@sha256:abc', 'quay.io/user/img',
                     'ghcr.io/image', 'user//image', '/nginx', 'user/image/', '_bad', 'a...b'):
            with self.subTest(text=text), self.assertRaises(RuntimeError):
                app.parse_image(text)

    def test_platform_validation(self):
        self.assertEqual(app.parse_platform('linux/arm/v7'), ['linux', 'arm', 'v7'])
        for text in ('linux', 'linux/', '/amd64', 'linux/amd64/', 'a/b/c/d'):
            with self.subTest(text=text), self.assertRaises(RuntimeError):
                app.parse_platform(text)

    def test_select_variant(self):
        client = Mock()
        selected = {'config': {}, 'layers': []}
        client.manifest.side_effect = [
            {'manifests': [
                {'digest': 'v6', 'platform': {'os': 'linux', 'architecture': 'arm', 'variant': 'v6'}},
                {'digest': 'v7', 'platform': {'os': 'linux', 'architecture': 'arm', 'variant': 'v7'}},
            ]}, selected]
        self.assertEqual(app.pick_manifest(client, 'latest', 'linux/arm/v7'), selected)
        client.manifest.assert_called_with('v7')

    def test_missing_platform(self):
        client = Mock()
        client.manifest.return_value = {'manifests': []}
        with self.assertRaisesRegex(RuntimeError, '没有匹配的平台'):
            app.pick_manifest(client, 'latest', 'linux/arm64')


class AuthenticationTests(unittest.TestCase):
    def test_challenge_and_proxy_propagation(self):
        client = app.RegistryClient('ghcr.io', 'owner/image', proxy='http://localhost:7890')
        denied = response(401, headers={'WWW-Authenticate': 'Bearer realm="https://ghcr.io/token",service="ghcr.io"'})
        token = response(data=b'{"token":"secret-token"}')
        allowed = response()
        with patch.dict(os.environ, {}, clear=True), patch.object(app.requests, 'get', side_effect=[denied, token, allowed]) as get:
            self.assertIs(client.get(client.registry_url + '/v2/'), allowed)
        self.assertEqual(get.call_args_list[1].kwargs['params']['scope'], 'repository:owner/image:pull')
        self.assertEqual(get.call_args_list[2].kwargs['headers']['Authorization'], 'Bearer secret-token')
        for call in get.call_args_list:
            self.assertEqual(call.kwargs['proxies'], {'http': 'http://localhost:7890', 'https': 'http://localhost:7890'})
        denied.close.assert_called_once()

    def test_reject_untrusted_auth_endpoint(self):
        client = app.RegistryClient('docker.io', 'library/nginx')
        with patch.object(app.requests, 'get') as get:
            for realm in ('https://evil.example/token', 'http://auth.docker.io/token',
                          'https://user:pass@auth.docker.io/token', 'https://auth.docker.io:8443/token'):
                with self.subTest(realm=realm), self.assertRaises(RuntimeError):
                    client.new_token(f'Bearer realm="{realm}"')
            get.assert_not_called()

    def test_private_credentials(self):
        client = app.RegistryClient('ghcr.io', 'owner/image')
        with patch.dict(os.environ, {'GHCR_USERNAME': 'user', 'GHCR_TOKEN': 'pat'}, clear=True), patch.object(app.requests, 'get', return_value=response(data=b'{"access_token":"t"}')) as get:
            self.assertEqual(client.new_token('Bearer realm="https://ghcr.io/token"'), 't')
            self.assertEqual(get.call_args.kwargs['auth'], ('user', 'pat'))

    def test_incomplete_credentials(self):
        client = app.RegistryClient('ghcr.io', 'owner/image')
        with patch.dict(os.environ, {'GHCR_USERNAME': 'user'}, clear=True), self.assertRaisesRegex(RuntimeError, '必须同时设置'):
            client.new_token('Bearer realm="https://ghcr.io/token"')

    def test_expired_token_refresh(self):
        client = app.RegistryClient('ghcr.io', 'owner/image')
        client.token = 'expired'
        client.challenge = 'Bearer realm="https://ghcr.io/token"'
        with patch.object(app.requests, 'get', side_effect=[response(401), response(data=b'{"token":"new"}'), response()]):
            client.get(client.registry_url + '/v2/')
        self.assertEqual(client.token, 'new')

    def test_terminal_http_error_closes_response(self):
        client = app.RegistryClient('docker.io', 'library/nginx')
        missing = response(404)
        with patch.object(app.requests, 'get', return_value=missing), patch.object(app.time, 'sleep'), self.assertRaises(requests.HTTPError):
            client.get(client.registry_url + '/v2/')
        self.assertTrue(missing.close.called)


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'blob'
        self.client = Mock()
        self.client.blob_url.return_value = 'https://example.test/blob'

    def test_range_probe(self):
        self.client.get.return_value = response(206, headers={'Content-Range': 'bytes 0-0/10'})
        self.assertTrue(app.check_range(self.client, digest(b'x'), 10))
        self.client.get.return_value = response(200)
        self.assertFalse(app.check_range(self.client, digest(b'x'), 10))

    def test_disjoint_range_write(self):
        self.path.write_bytes(b'000000')
        self.client.get.return_value = response(206, b'abc', {'Content-Range': 'bytes 2-4/6'})
        app.transfer(self.client, digest(b'000000'), self.path, 6, 2, 4)
        self.assertEqual(self.path.read_bytes(), b'00abc0')

    def test_bad_content_range_rejected(self):
        self.path.write_bytes(b'000000')
        self.client.get.return_value = response(206, b'abc', {'Content-Range': 'bytes 0-2/6'})
        with patch.object(app.time, 'sleep'), self.assertRaisesRegex(RuntimeError, 'Content-Range'):
            app.transfer(self.client, digest(b'x'), self.path, 6, 2, 4)

    def test_truncated_transfer_rejected(self):
        self.client.get.return_value = response(200, b'ab')
        with patch.object(app.time, 'sleep'), self.assertRaisesRegex(RuntimeError, '长度不正确'):
            app.transfer(self.client, digest(b'abc'), self.path, 3)

    def test_stream_disconnect_retries(self):
        broken = response()
        broken.raw.stream.side_effect = ProtocolError('connection interrupted')
        self.client.get.side_effect = [broken, response(data=b'abc')]
        with patch.object(app.time, 'sleep'):
            app.transfer(self.client, digest(b'abc'), self.path, 3)
        self.assertEqual(self.path.read_bytes(), b'abc')
        self.assertEqual(self.client.get.call_count, 2)

    def test_blob_hash_rejected(self):
        self.client.get.return_value = response(data=b'bad')
        with self.assertRaisesRegex(RuntimeError, 'SHA256 校验失败'):
            app.acquire_blobs(self.client, [{'digest': digest(b'abc'), 'size': 3}], Path(self.tmp.name), 2, 64)

    def test_duplicate_descriptors_download_once(self):
        data = b'abc'
        self.client.get.return_value = response(data=data)
        desc = {'digest': digest(data), 'size': len(data)}
        files = app.acquire_blobs(self.client, [desc, desc], Path(self.tmp.name), 2, 64)
        self.assertEqual(len(files), 1)
        self.client.get.assert_called_once()

    def test_negative_blob_size(self):
        with self.assertRaisesRegex(RuntimeError, '负数'):
            app.acquire_blobs(self.client, [{'digest': digest(b'x'), 'size': -1}], Path(self.tmp.name), 2, 64)

    def test_layer_formats_and_diffid(self):
        raw = b'layer tar contents'
        output = Path(self.tmp.name) / 'layer.tar'
        for media, payload in (
            ('application/vnd.oci.image.layer.v1.tar', raw),
            ('application/vnd.oci.image.layer.v1.tar+gzip', gzip.compress(raw)),
            ('application/vnd.docker.image.rootfs.diff.tar.gzip', gzip.compress(raw)),
        ):
            with self.subTest(media=media):
                self.path.write_bytes(payload)
                app.unpack_layer(self.path, output, media, digest(raw))
                self.assertEqual(output.read_bytes(), raw)
        with self.assertRaisesRegex(RuntimeError, 'DiffID'):
            app.unpack_layer(self.path, output, media, digest(b'wrong'))
        self.assertFalse(output.exists())

    def test_zstd_layer(self):
        try:
            import zstandard
        except ImportError:
            self.skipTest('optional zstandard dependency is not installed')
        data = b'layer tar contents'
        self.path.write_bytes(zstandard.ZstdCompressor().compress(data))
        output = Path(self.tmp.name) / 'layer.tar'
        app.unpack_layer(self.path, output, 'application/vnd.oci.image.layer.v1.tar+zstd', digest(data))
        self.assertEqual(output.read_bytes(), data)


class OutputTests(unittest.TestCase):
    def test_windows_help_handles_legacy_stream_encoding(self):
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding='cp1252')
        try:
            with patch.object(app.sys, 'platform', 'win32'), patch.object(app.sys, 'stdout', stream), patch('sys.argv', ['downloader', '--help']), self.assertRaises(SystemExit) as result:
                app.main()
            stream.flush()
            self.assertEqual(result.exception.code, 0)
            self.assertIn('并发分片下载', buffer.getvalue().decode('utf-8'))
        finally:
            stream.close()

    def test_failed_export_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'image.tar'
            target.write_bytes(b'original')
            cfg = Path(tmp) / 'config.json'
            cfg.write_text('{"os":"linux","architecture":"amd64","rootfs":{"diff_ids":[]}}')
            args = argparse.Namespace(image='nginx', platform='linux/amd64', proxy=None, output=str(target), workdir=None, workers=1, chunk_mb=1, docker='docker')
            with patch.object(app, 'pick_manifest', return_value={'config': {'digest': digest(b'config')}, 'layers': []}), patch.object(app, 'acquire_blobs', return_value={digest(b'config'): cfg}), patch.object(app, 'make_archive', side_effect=OSError('disk full')):
                with self.assertRaisesRegex(OSError, 'disk full'):
                    app.run(args)
            self.assertEqual(target.read_bytes(), b'original')
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ['config.json', 'image.tar'])

    def test_missing_docker_fails_before_download(self):
        args = argparse.Namespace(image='nginx', platform='linux/amd64', output=None, docker='nonexistent-docker')
        with patch.object(app.shutil, 'which', return_value=None), patch.object(app, 'RegistryClient') as client, self.assertRaisesRegex(RuntimeError, '找不到 Docker CLI'):
            app.run(args)
        client.assert_not_called()

    def test_invalid_platform_cli_exit(self):
        with patch('sys.argv', ['downloader', 'nginx', '--platform', 'linux']), patch('sys.stderr', new_callable=io.StringIO) as stderr, self.assertRaises(SystemExit) as result:
            app.main()
        self.assertEqual(result.exception.code, 2)
        self.assertIn('平台格式', stderr.getvalue())
