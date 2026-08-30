import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import cf_deploy


def multipart(modules):
    boundary = '----release-test'
    parts = []
    for name, source in modules:
        parts.append(
            ('--%s\r\n'
             'Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
             'Content-Type: application/javascript+module\r\n\r\n' % (boundary, name, name)).encode()
            + source
            + b'\r\n'
        )
    parts.append(('--%s--\r\n' % boundary).encode())
    return b''.join(parts), 'multipart/form-data; boundary=' + boundary


class DeployBackupTests(unittest.TestCase):
    def test_extracts_single_module_from_cloudflare_multipart(self):
        expected = b'export default { fetch() { return new Response("ok") } };' * 30
        payload, content_type = multipart([('worker.js', expected)])

        source, modules = cf_deploy.extract_main_module(payload, content_type)

        self.assertEqual(source, expected)
        self.assertEqual(modules, ['worker.js'])

    def test_rejects_multi_module_backup_before_deploy(self):
        payload, content_type = multipart([
            ('worker.js', b'export default {}'),
            ('dependency.js', b'export const value = 1'),
        ])

        with redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                cf_deploy.extract_main_module(payload, content_type)

    def test_writes_redeployable_source_and_non_secret_manifest(self):
        expected = b'export default { fetch() { return new Response("stable") } };' * 30
        payload, content_type = multipart([('worker.js', expected)])
        with tempfile.TemporaryDirectory() as directory:
            source_path, manifest_path, manifest = cf_deploy.write_source_backup(
                directory,
                'emby-proxy',
                'version-123',
                payload,
                content_type,
            )

            self.assertEqual(Path(source_path).read_bytes(), expected)
            saved_manifest = json.loads(Path(manifest_path).read_text(encoding='utf-8'))
            self.assertEqual(saved_manifest, manifest)
            self.assertEqual(saved_manifest['active_version_id'], 'version-123')
            self.assertEqual(saved_manifest['modules'], ['worker.js'])
            self.assertNotIn('bindings', saved_manifest)


if __name__ == '__main__':
    unittest.main()
