"""Offline Registry fixture and opt-in real Docker archive verification."""
import argparse
import gzip
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

import registry_parallel_pull as app


def digest(data):
    return 'sha256:' + hashlib.sha256(data).hexdigest()


class RegistryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        content = b'hello from downloader tests\n'
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w') as layer:
            info = tarfile.TarInfo('hello.txt')
            info.size = len(content)
            info.mode = 0o644
            layer.addfile(info, io.BytesIO(content))
        self.layer = buf.getvalue()
        self.diff_id = digest(self.layer)
        config = json.dumps({
            'architecture': 'amd64', 'os': 'linux',
            'config': {'Cmd': ['/hello.txt']},
            'rootfs': {'type': 'layers', 'diff_ids': [self.diff_id]},
            'history': [{'created_by': 'offline downloader test'}],
        }).encode()
        compressed = gzip.compress(self.layer)
        manifest = {
            'schemaVersion': 2,
            'mediaType': 'application/vnd.oci.image.manifest.v1+json',
            'config': {'mediaType': 'application/vnd.oci.image.config.v1+json', 'digest': digest(config), 'size': len(config)},
            'layers': [{'mediaType': 'application/vnd.oci.image.layer.v1.tar+gzip', 'digest': digest(compressed), 'size': len(compressed)}],
        }
        self.blobs = {digest(config): config, digest(compressed): compressed}
        self.ranges = []
        self.range_enabled = True
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if '/manifests/' in self.path:
                    body = json.dumps(manifest).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', manifest['mediaType'])
                else:
                    blob_digest = self.path.rsplit('/', 1)[-1]
                    body = fixture.blobs.get(blob_digest)
                    if body is None:
                        self.send_error(404)
                        return
                    byte_range = self.headers.get('Range')
                    if byte_range and fixture.range_enabled:
                        start, end = map(int, byte_range[6:].split('-'))
                        fixture.ranges.append((start, end))
                        total = len(body)
                        body = body[start:end + 1]
                        self.send_response(206)
                        self.send_header('Content-Range', f'bytes {start}-{end}/{total}')
                    else:
                        self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()

        def stop_server():
            self.server.shutdown()
            thread.join(timeout=5)
            self.server.server_close()
        self.addCleanup(stop_server)
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        env = patch.dict(os.environ, {'NO_PROXY': '127.0.0.1', 'no_proxy': '127.0.0.1'})
        env.start()
        self.addCleanup(env.stop)

    def args(self, **changes):
        values = dict(image='test/image:v1', platform='linux/amd64', proxy=None,
                      output=str(Path(self.tmp.name) / 'image.tar'), workdir=self.tmp.name,
                      workers=4, chunk_mb=1, docker='docker')
        values.update(changes)
        return argparse.Namespace(**values)

    def export(self, **changes):
        args = self.args(**changes)
        with patch.dict(app.REGISTRIES, {'docker.io': self.url}):
            app.run(args)
        return Path(args.output)

    def test_manifest_to_archive(self):
        output = self.export()
        with tarfile.open(output) as archive:
            manifest = json.load(archive.extractfile('manifest.json'))[0]
            self.assertEqual(manifest['RepoTags'], ['test/image:v1'])
            config = json.load(archive.extractfile(manifest['Config']))
            self.assertEqual(config['rootfs']['diff_ids'], [self.diff_id])
            self.assertEqual(archive.extractfile(manifest['Layers'][0]).read(), self.layer)
        self.assertFalse(any(p.name.startswith('dh-parallel-') for p in Path(self.tmp.name).iterdir()))

    def test_real_parallel_ranges(self):
        body = bytes(range(256)) * 4096
        desc = {'digest': digest(body), 'size': len(body)}
        self.blobs[desc['digest']] = body
        with patch.dict(app.REGISTRIES, {'docker.io': self.url}):
            client = app.RegistryClient('docker.io', 'test/image')
        files = app.acquire_blobs(client, [desc], Path(self.tmp.name) / 'blobs', 4, 65536)
        self.assertEqual(files[desc['digest']].read_bytes(), body)
        self.assertEqual(len(self.ranges), 17)
        self.assertIn((len(body) - 65536, len(body) - 1), self.ranges)
        self.ranges.clear()
        app.acquire_blobs(client, [desc], Path(self.tmp.name) / 'blobs', 4, 65536)
        self.assertEqual(self.ranges, [])

    def test_server_without_range_support(self):
        self.range_enabled = False
        body = bytes(range(256)) * 1024
        desc = {'digest': digest(body), 'size': len(body)}
        self.blobs[desc['digest']] = body
        with patch.dict(app.REGISTRIES, {'docker.io': self.url}):
            client = app.RegistryClient('docker.io', 'test/image')
        files = app.acquire_blobs(client, [desc], Path(self.tmp.name) / 'blobs', 4, 65536)
        self.assertEqual(files[desc['digest']].read_bytes(), body)
        self.assertEqual(self.ranges, [])

    @unittest.skipUnless(os.environ.get('TEST_DOCKER_LOAD') == '1', 'set TEST_DOCKER_LOAD=1 to verify with Docker Engine')
    def test_real_docker_load(self):
        tag = f'downloader-test-{uuid.uuid4().hex}:test'
        output = self.export(image=tag)
        try:
            subprocess.run(['docker', 'load', '-i', str(output)], check=True)
            result = subprocess.run(['docker', 'image', 'inspect', tag], check=True, text=True, capture_output=True)
            loaded = json.loads(result.stdout)[0]
            self.assertEqual(loaded['RootFS']['Layers'], [self.diff_id])
            self.assertEqual(loaded['Architecture'], 'amd64')
        finally:
            subprocess.run(['docker', 'image', 'rm', '-f', tag], check=False)
