"""Validate private Docker uploads and run builds against the configured engine."""

import gzip
import json
import re
import shutil
import stat
import tarfile
import zipfile
from contextlib import ExitStack
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryFile
from threading import Thread
from uuid import uuid4

from flask import current_app
import yaml
from yaml.events import AliasEvent

from services.cache import cache_service


MAX_ENTRIES = 10_000
MAX_EXPANDED_BYTES = 2 * 1024**3
MAX_MANIFEST_BYTES = 256 * 1024
CHUNK_BYTES = 1024**2
JOB_TTL = 24 * 3600
MANIFEST_NAMES = ('challenge.yml', 'challenge.yaml')
DEFAULT_FLAG_PREFIX = 'CYS'
ARCHIVE_SUFFIXES = ('.zip', '.tar', '.tar.gz', '.tgz')
TEXT_LIMITS = {
    'name': 100, 'category': 50, 'description': 16_000, 'author': 100,
    'difficulty': 20, 'flag_mode': 20, 'flag_prefix': 32, 'flag': 255,
    'flag_path': 256, 'connection': 512,
}
NUMBER_FIELDS = ('points', 'min_points', 'decay')


class UploadError(ValueError):
    """An upload error that may be displayed to the administrator."""


def _member_parts(name):
    path = name.replace('\\', '/')
    if path.startswith('/'):
        raise UploadError('Archive entries must use relative paths.')
    parts = tuple(part for part in path.split('/') if part not in ('', '.'))
    for part in parts:
        if (part == '..' or part.endswith((' ', '.'))
                or re.search(r'[\x00-\x1f<>:"|?*]', part)
                or re.fullmatch(r'(?i:con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])',
                                part.split('.')[0].rstrip())):
            raise UploadError('An archive entry has an unsafe path.')
        if part.casefold() == 'solution.md':
            raise UploadError('Keep SOLUTION.md outside the uploaded archive.')
    return parts


def _check_budget(count, size):
    if count > MAX_ENTRIES:
        raise UploadError(f'The archive has more than {MAX_ENTRIES:,} entries.')
    if size > MAX_EXPANDED_BYTES:
        raise UploadError('The archive exceeds the expanded size limit.')


class _UploadTarInfo(tarfile.TarInfo):
    def _proc_member(self, archive):
        # tarfile consumes PAX/GNU headers before yielding visible members.
        archive.upload_headers = getattr(archive, 'upload_headers', 0) + 1
        _check_budget(archive.upload_headers, 0)
        if self.type == tarfile.GNUTYPE_SPARSE:
            raise UploadError('Sparse tar files are unsupported.')
        if self.type not in (*tarfile.REGULAR_TYPES, tarfile.DIRTYPE):
            if self.size > MAX_MANIFEST_BYTES:
                raise UploadError('A tar metadata entry is too large.')
        return super()._proc_member(archive)


def _open_tar(path, stack):
    stream = stack.enter_context(path.open('rb'))
    signature = stream.read(2)
    stream.seek(0)
    if signature == b'\x1f\x8b':
        expanded = stack.enter_context(TemporaryFile(dir=path.parent))
        # Permit tar headers and block padding in addition to file contents.
        for chunk in _gzip_chunks(stream, MAX_EXPANDED_BYTES + 32 * CHUNK_BYTES):
            expanded.write(chunk)
        expanded.seek(0)
        stream = expanded
    return stack.enter_context(tarfile.open(fileobj=stream, mode='r:', tarinfo=_UploadTarInfo))


def _gzip_chunks(stream, limit):
    with gzip.GzipFile(fileobj=stream) as compressed:
        while chunk := compressed.read(CHUNK_BYTES):
            limit -= len(chunk)
            if limit < 0:
                raise UploadError('The archive exceeds the expanded size limit.')
            yield chunk


def safe_extract(path, destination):
    """Extract regular files and directories to a new, private directory."""
    destination.mkdir()
    seen = set()
    total = 0
    try:
        with ExitStack() as stack:
            is_zip = zipfile.is_zipfile(path)
            if is_zip:
                # Bound the central directory before ZipFile materializes its entries.
                with path.open('rb') as stream:
                    footer = zipfile._EndRecData(stream)
                _check_budget(footer[zipfile._ECD_ENTRIES_TOTAL], 0)
                if footer[zipfile._ECD_SIZE] > MAX_ENTRIES * 1024:
                    raise UploadError('The ZIP directory is too large.')
                archive = stack.enter_context(zipfile.ZipFile(path))
                members = archive.infolist()
                _check_budget(len(members), 0)
                open_member = archive.open
            else:
                archive = _open_tar(path, stack)
                members = archive
                open_member = archive.extractfile

            for count, member in enumerate(members, 1):
                if is_zip:
                    name, size = member.orig_filename, member.file_size
                    directory = member.is_dir()
                    mode = stat.S_IFMT(member.external_attr >> 16)
                    safe_type = mode in (0, stat.S_IFDIR if directory else stat.S_IFREG)
                else:
                    name, size = member.name, member.size
                    directory = member.isdir()
                    safe_type = directory or (member.type in (tarfile.REGTYPE, tarfile.AREGTYPE)
                                              and member.sparse is None)
                if not safe_type or size < 0:
                    raise UploadError('Archive links and special files are unsupported.')
                total += size
                _check_budget(count, total)
                parts = _member_parts(name)
                if not parts and directory:
                    continue
                folded = tuple(part.casefold() for part in parts)
                if not parts or folded in seen:
                    raise UploadError('The archive contains a missing or duplicate path.')
                seen.add(folded)
                target = destination.joinpath(*parts)
                if directory:
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with open_member(member) as source, target.open('xb') as output:
                        shutil.copyfileobj(source, output, CHUNK_BYTES)
                    if target.stat().st_size != size:
                        raise UploadError('An archive entry has an incorrect size.')
                    if not is_zip and member.mode & 0o111:
                        target.chmod(0o755)
                    if is_zip and (member.external_attr >> 16) & 0o111:
                        target.chmod(0o755)
        return 'zip' if is_zip else 'tar'
    except (tarfile.TarError, zipfile.BadZipFile, gzip.BadGzipFile, EOFError,
            RuntimeError, ValueError, FileExistsError, NotADirectoryError, IsADirectoryError) as error:
        if isinstance(error, UploadError):
            raise
        raise UploadError('The archive is invalid, unsupported, or has conflicting entries.') from error


def find_context(root):
    if (root / 'Dockerfile').is_file():
        return root
    entries = [entry for entry in root.iterdir() if entry.name not in ('__MACOSX', '.DS_Store')]
    if len(entries) == 1 and entries[0].is_dir() and (entries[0] / 'Dockerfile').is_file():
        return entries[0]
    raise UploadError('Put a Dockerfile at the archive root or inside one top-level folder.')


class _ManifestLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        if self.check_event(AliasEvent):
            raise UploadError('YAML aliases are unsupported in challenge.yml.')
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if type(key) is not str or key in result:
                raise UploadError('YAML mapping keys must be unique strings.')
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def validate_fields(data):
    """Validate the known manifest fields, also used for the final admin form."""
    for key, limit in TEXT_LIMITS.items():
        if key in data:
            value = data[key]
            if not isinstance(value, str) or not value.strip() or '\x00' in value or len(value) > limit:
                raise UploadError(f'Enter valid text for {key} (up to {limit:,} characters).')
    for key in NUMBER_FIELDS:
        if key in data and (type(data[key]) is not int or not 1 <= data[key] <= 2**31 - 1):
            raise UploadError(f'{key} must be a positive whole number.')
    if data.get('min_points', 0) > data.get('points', 2**31 - 1):
        raise UploadError('Minimum points cannot exceed initial points.')
    if data.get('difficulty', 'easy') not in {'easy', 'medium', 'hard'}:
        raise UploadError('Use easy, medium or hard for difficulty.')
    if data.get('flag_mode', 'dynamic') not in {'dynamic', 'static'}:
        raise UploadError('Use dynamic or static for flag_mode.')
    if not re.fullmatch(r'[A-Za-z0-9_]{1,32}', data.get('flag_prefix', DEFAULT_FLAG_PREFIX)):
        raise UploadError('Use letters, numbers or underscores for flag_prefix.')
    if data.get('flag_mode') == 'static' and not data.get('flag'):
        raise UploadError('Enter a static flag.')
    if 'flag_path' in data:
        parts = data['flag_path'].split('/')
        if parts[0] != '' or any(part in ('', '.', '..') for part in parts[1:]):
            raise UploadError('Use an absolute file path such as /flag.txt for flag_path.')


def validate_hints(hints):
    if not isinstance(hints, list) or len(hints) > 100:
        raise UploadError('Provide a list of up to 100 hints.')
    clean = []
    for hint in hints:
        if isinstance(hint, str):
            hint = {'content': hint}
        if not isinstance(hint, dict) or hint.keys() - {'content', 'cost'}:
            raise UploadError('Each hint needs content and may include a cost.')
        content, cost = hint.get('content'), hint.get('cost', 0)
        if not isinstance(content, str) or not content.strip() or len(content) > 16_000 or '\x00' in content:
            raise UploadError('Enter valid text for each hint.')
        if type(cost) is not int or not 0 <= cost <= 2**31 - 1:
            raise UploadError('Hint costs must be nonnegative whole numbers.')
        clean.append({'content': content.strip(), 'cost': cost})
    return clean


def parse_manifest(content):
    try:
        data = yaml.load(content, Loader=_ManifestLoader)
    except UploadError:
        raise
    except (yaml.YAMLError, RecursionError, ValueError, OverflowError) as error:
        raise UploadError('challenge.yml must contain valid YAML.') from error
    if not isinstance(data, dict):
        raise UploadError('challenge.yml must contain a mapping of fields to values.')
    known = set(TEXT_LIMITS) | set(NUMBER_FIELDS) | {'hints'}
    fields = {key: value for key, value in data.items() if key in known}
    validate_fields(fields)
    if 'hints' in fields:
        fields['hints'] = validate_hints(fields['hints'])
    return fields, [f'Ignored unknown manifest field {key!r}.' for key in data if key not in known]


def read_manifest(context):
    manifests = [context / name for name in MANIFEST_NAMES if (context / name).is_file()]
    if not manifests:
        return {}, []
    if len(manifests) != 1:
        raise UploadError('Include only one challenge.yml or challenge.yaml.')
    path = manifests[0]
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        raise UploadError('challenge.yml exceeds 256 KiB.')
    try:
        return parse_manifest(path.read_text(encoding='utf-8-sig'))
    except UnicodeError as error:
        raise UploadError('Save challenge.yml as UTF-8.') from error


def prepare_archive(archive_path, root):
    archive_type = safe_extract(archive_path, root)
    if archive_type == 'tar' and (root / 'manifest.json').is_file() and not (root / 'Dockerfile').is_file():
        return 'image', root, {}, []
    context = find_context(root)
    manifest, warnings = read_manifest(context)
    # The manifest can contain a static flag and is only needed by the admin form.
    for name in MANIFEST_NAMES:
        path = context / name
        if path.is_file():
            path.unlink()
    return 'context', context, manifest, warnings


def image_tag(name, job_id):
    stem = re.sub(r'\.(zip|tar\.gz|tar|tgz)$', '', name, flags=re.IGNORECASE)
    slug = re.sub(r'[^a-z0-9]+', '-', stem.lower()).strip('-')[:48].rstrip('-')
    return f'blackbox/{slug or "challenge"}:{job_id}'


def _saved_image(root, client, tag):
    manifest_path = root / 'manifest.json'
    if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
        raise UploadError('The saved-image manifest is too large.')
    try:
        images = json.loads(manifest_path.read_bytes())
        if not isinstance(images, list) or len(images) != 1 or not isinstance(images[0], dict):
            raise ValueError
        config, layers = images[0]['Config'], images[0]['Layers']
        if not isinstance(layers, list) or len(layers) >= MAX_ENTRIES:
            raise ValueError
        for name in [config, *layers]:
            if (not isinstance(name, str) or '/'.join(_member_parts(name)) != name
                    or name.casefold() in {'manifest.json', 'index.json', 'oci-layout', 'repositories'}
                    or not (root / name).is_file()):
                raise ValueError
    except (ValueError, KeyError, RecursionError) as error:
        raise UploadError('Upload a docker save archive containing one image with its config and layers.') from error

    remaining_bytes = MAX_EXPANDED_BYTES
    for name in layers:
        layer = root / name
        with layer.open('rb') as stream:
            signature = stream.read(6)
            stream.seek(0)
            if signature.startswith(b'\x1f\x8b'):
                try:
                    remaining_bytes -= sum(len(chunk) for chunk in _gzip_chunks(stream, remaining_bytes))
                except (gzip.BadGzipFile, EOFError) as error:
                    raise UploadError('A saved image layer contains invalid gzip data.') from error
            elif signature.startswith((b'BZh', b'\xfd7zXZ\x00', b'\x28\xb5\x2f\xfd')):
                raise UploadError('Saved image layers must be uncompressed or gzip-compressed.')
            else:
                remaining_bytes -= layer.stat().st_size
            if remaining_bytes < 0:
                raise UploadError('The archive exceeds the expanded size limit.')

    # Repack only the validated files and assign our job tag before loading.
    # This keeps archive-supplied tags and OCI indexes from replacing existing images.
    with TemporaryFile(dir=root.parent) as stream:
        with tarfile.open(fileobj=stream, mode='w') as archive:
            for name in dict.fromkeys([config, *layers]):
                archive.add(root / name, arcname=name, recursive=False)
            manifest = json.dumps([{'Config': config, 'Layers': layers, 'RepoTags': [tag]}]).encode()
            info = tarfile.TarInfo('manifest.json')
            info.size = len(manifest)
            archive.addfile(info, BytesIO(manifest))
        stream.seek(0)
        if not client.images.load(stream):
            raise UploadError('Docker did not load an image from the archive.')


def _docker_client():
    from services.container_manager import container_orchestrator

    container_orchestrator._ensure_docker_client()
    if container_orchestrator.docker_client is None:
        raise UploadError('The Docker engine is unavailable. Check Docker Config.')
    return container_orchestrator.docker_client


def _job_key(job_id):
    return f'docker_upload:{job_id}'


def _save_job(job):
    cache_service.redis_client.setex(_job_key(job['id']), JOB_TTL, json.dumps(job))


def get_job(job_id):
    if not re.fullmatch(r'[a-f0-9]{32}', job_id):
        return None
    raw = cache_service.redis_client.get(_job_key(job_id))
    return json.loads(raw) if raw else None


def start_job(app, *, workdir, kind, context, filename, sha256, user_id,
              manifest, warnings, run_inline=False):
    job = {
        'id': uuid4().hex, 'user_id': user_id, 'kind': kind, 'filename': filename,
        'sha256': sha256, 'manifest': manifest, 'warnings': warnings, 'status': 'queued',
        'image': None, 'exposed_ports': [], 'error': None, 'log': [],
    }
    _save_job(job)

    def work():
        with app.app_context():
            try:
                _run(job, context)
            finally:
                workdir.cleanup()

    if run_inline:
        work()
    else:
        Thread(target=work, name=f'docker-upload-{job["id"][:8]}', daemon=True).start()
    return job['id']


def _run(job, context):
    from utils.audit import log_audit_event

    tag = image_tag(job['manifest'].get('name', job['filename']), job['id'])
    job['status'] = 'loading' if job['kind'] == 'image' else 'building'
    try:
        _save_job(job)
        client = _docker_client()
        if job['kind'] == 'image':
            _saved_image(context, client, tag)
        else:
            for chunk in client.api.build(path=str(context), tag=tag, rm=True, forcerm=True, pull=True, decode=True):
                if 'error' in chunk or 'errorDetail' in chunk:
                    detail = (chunk.get('errorDetail') or {}).get('message') or chunk.get('error')
                    raise UploadError(f'Docker build failed: {str(detail or "unknown error")[:2000]}')
                line = chunk.get('stream') or chunk.get('status')
                if line:
                    job['log'].append(str(line)[:2000].rstrip())
                    del job['log'][:-100]
                    _save_job(job)
        ports = list((client.images.get(tag).attrs.get('Config') or {}).get('ExposedPorts') or {})
        if not ports:
            raise UploadError('The image exposes no port. Add an EXPOSE instruction to the Dockerfile.')
        if len(ports) > 1:
            job['warnings'].append(f'Use the same first exposed port as the container runtime ({ports[0]}).')
        job.update(status='done', image=tag, exposed_ports=ports)
    except UploadError as error:
        job.update(status='error', error=str(error))
    except Exception:
        current_app.logger.exception('Docker upload job %s failed', job['id'])
        job.update(status='error', error='Docker could not process the archive. Check the server logs.')
    _save_job(job)
    log_audit_event(user_id=job['user_id'], action='DOCKER_IMAGE_UPLOAD', details={
        'job_id': job['id'], 'tag': tag, 'filename': job['filename'],
        'sha256': job['sha256'], 'status': job['status'],
    })
