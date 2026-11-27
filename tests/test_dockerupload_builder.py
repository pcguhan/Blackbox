import gzip
import io
import json
import stat
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from services import docker_builder as builder


DOCKERFILE = b'FROM scratch\nEXPOSE 8080\n'


def zip_archive(entries):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for filename, content, *mode in entries:
            member = zipfile.ZipInfo(filename)
            if mode:
                member.external_attr = mode[0] << 16
            archive.writestr(member, content)
    return payload.getvalue()


def tar_archive(entries, mode='w'):
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode=mode) as archive:
        for filename, content, *kind in entries:
            member = tarfile.TarInfo(filename)
            member.size = len(content)
            if kind:
                member.type = kind[0]
                if len(kind) > 1:
                    member.linkname = kind[1]
            archive.addfile(member, io.BytesIO(content) if member.isfile() else None)
    return payload.getvalue()


def saved_image_archive(*extra_entries, config='image.json', layer=None):
    manifest = json.dumps([{
        'Config': config, 'Layers': ['layer.tar'], 'RepoTags': ['existing:latest'],
    }]).encode()
    return tar_archive([
        ('manifest.json', manifest), ('image.json', b'{}'), ('layer.tar', tar_archive([]) if layer is None else layer),
        ('index.json', b'{}'), *extra_entries,
    ])


class ArchiveSafetyTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=Path.cwd())))
        self.path = self.root / 'archive'
        self.count = 0

    def extract(self, payload):
        self.path.write_bytes(payload)
        self.count += 1
        output = self.root / f'context-{self.count}'
        return builder.prepare_archive(self.path, output)

    def test_zip_paths_and_links(self):
        for entry in [
            ('../escape.txt', b'x'), ('/absolute', b'x'), ('C:/absolute', b'x'),
            ('ok/..\\..\\escape.txt', b'x'), ('directory/NUL.txt', b'x'),
            ('link', b'Dockerfile', stat.S_IFLNK | 0o777), ('Solution.MD', b'private'),
        ]:
            with self.subTest(entry=entry[0]), self.assertRaises(builder.UploadError):
                self.extract(zip_archive([('Dockerfile', DOCKERFILE), entry]))
        self.assertFalse((self.root / 'escape.txt').exists())

    def test_tar_paths_links_and_devices(self):
        for entry in [
            ('../escape.txt', b'x'), ('/absolute', b'x'),
            ('link', b'', tarfile.SYMTYPE, 'Dockerfile'),
            ('hardlink', b'', tarfile.LNKTYPE, 'Dockerfile'),
            ('device', b'', tarfile.CHRTYPE), ('fifo', b'', tarfile.FIFOTYPE),
        ]:
            with self.subTest(entry=entry[0]), self.assertRaises(builder.UploadError):
                self.extract(tar_archive([('Dockerfile', DOCKERFILE), entry]))
        self.assertFalse((self.root / 'escape.txt').exists())

    def test_file_sizes_and_entry_budgets_for_zip_and_tar(self):
        entries = [('Dockerfile', DOCKERFILE), ('large', b'a' * 1000)]
        for payload in (zip_archive(entries), tar_archive(entries, 'w:gz')):
            with mock.patch.object(builder, 'MAX_ENTRIES', 1), self.assertRaisesRegex(builder.UploadError, 'entries'):
                self.extract(payload)
            with mock.patch.object(builder, 'MAX_EXPANDED_BYTES', 50), self.assertRaisesRegex(builder.UploadError, 'expanded'):
                self.extract(payload)

    def test_duplicate_and_conflicting_paths(self):
        for entries in [
            [('README', b'a'), ('readme', b'b')],
            [('README', b'a'), ('README/folder', b'b')],
        ]:
            with self.subTest(entries=entries), self.assertRaises(builder.UploadError):
                self.extract(zip_archive([('Dockerfile', DOCKERFILE), *entries]))

    def test_context_discovery_and_private_manifest(self):
        for prefix, create in [('', zip_archive), ('wrapped/', tar_archive)]:
            with self.subTest(prefix=prefix):
                kind, context, fields, warnings = self.extract(create([
                    (prefix + 'Dockerfile', DOCKERFILE),
                    (prefix + 'challenge.yml', b'name: Local fixture\n'),
                ]))
                self.assertEqual(kind, 'context')
                self.assertEqual((context / 'Dockerfile').read_bytes(), DOCKERFILE)
                self.assertEqual(fields, {'name': 'Local fixture'})
                self.assertEqual(warnings, [])
                self.assertFalse((context / 'challenge.yml').exists())
        with self.assertRaisesRegex(builder.UploadError, 'Dockerfile'):
            self.extract(zip_archive([('a/Dockerfile', DOCKERFILE), ('b/Dockerfile', DOCKERFILE)]))

    def test_saved_image_is_repacked_with_only_the_job_tag(self):
        kind, context, _, _ = self.extract(saved_image_archive())
        self.assertEqual(kind, 'image')
        docker = mock.Mock()
        tag = builder.image_tag('Demo.tgz', 'one')

        def inspect(stream):
            with tarfile.open(fileobj=stream, mode='r:') as archive:
                self.assertEqual(set(archive.getnames()), {'manifest.json', 'image.json', 'layer.tar'})
                self.assertEqual(json.load(archive.extractfile('manifest.json'))[0]['RepoTags'], [tag])
            return [mock.Mock()]

        docker.images.load.side_effect = inspect
        builder._saved_image(context, docker, tag)
        docker.images.load.assert_called_once()
        self.assertEqual(tag, 'blackbox/demo:one')
        self.assertNotEqual(tag, builder.image_tag('Demo.tgz', 'two'))

    def test_saved_image_members_receive_same_archive_checks(self):
        with self.assertRaises(builder.UploadError):
            self.extract(saved_image_archive(('link', b'', tarfile.LNKTYPE, 'image.json')))
        kind, context, _, _ = self.extract(saved_image_archive(config='../external.json'))
        self.assertEqual(kind, 'image')
        docker = mock.Mock()
        with self.assertRaises(builder.UploadError):
            builder._saved_image(context, docker, 'blackbox/demo:one')
        docker.images.load.assert_not_called()

    def test_saved_image_compressed_layer_budget(self):
        layer = gzip.compress(tar_archive([('large-file', b'a' * 2000)]))
        _, context, _, _ = self.extract(saved_image_archive(layer=layer))
        docker = mock.Mock()
        with mock.patch.object(builder, 'MAX_EXPANDED_BYTES', 1000):
            with self.assertRaisesRegex(builder.UploadError, 'expanded'):
                builder._saved_image(context, docker, 'blackbox/demo:one')
        docker.images.load.assert_not_called()


class ManifestTests(unittest.TestCase):
    def test_valid_manifest_and_warnings(self):
        fields, warnings = builder.parse_manifest(
            'name: Example\ncategory: Web\npoints: 50\ndifficulty: easy\n'
            'flag_mode: dynamic\nflag_prefix: CYS\nflag_path: /flag.txt\n'
            'hints: [{content: Inspect the headers., cost: 10}, Try refreshing.]\nextra: true'
        )
        self.assertEqual(fields['points'], 50)
        self.assertEqual(fields['flag_prefix'], 'CYS')
        self.assertEqual(fields['hints'], [
            {'content': 'Inspect the headers.', 'cost': 10},
            {'content': 'Try refreshing.', 'cost': 0},
        ])
        self.assertIn('extra', warnings[0])

    def test_malformed_yaml_and_invalid_schema(self):
        for manifest in [
            'name: [unclosed', '- just a list', 'points: true', 'points: "50"', 'points: -1',
            'min_points: 200\npoints: 100', 'flag_mode: static', 'flag_prefix: "bad prefix!"',
            'flag_path: /tmp/../flag', 'hints: [{cost: 5}]', 'hints: [{content: Hint, cost: -1}]',
            'name: !!python/object:builtins.object {}', 'name: &name Example\ncategory: *name',
            'name: One\nname: Two', 'name: 2026-99-99', 'name: ' + 'x' * 101,
        ]:
            with self.subTest(manifest=manifest), self.assertRaises(builder.UploadError):
                builder.parse_manifest(manifest)
