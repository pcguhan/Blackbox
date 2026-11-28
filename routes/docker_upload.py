"""Administrator workflow for building and assigning challenge images."""

import hashlib
import secrets
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import docker
from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

from models import db
from models.branching import ChallengeFlag
from models.challenge import Challenge
from models.hint import Hint
from models.settings import DockerSettings
from routes.admin import admin_required
from security_utils import CSRFProtection
from services import docker_builder
from services.cache import cache_service
from utils.audit import log_audit_event


docker_upload_bp = Blueprint('docker_upload', __name__, url_prefix='/admin/docker/uploads')


@docker_upload_bp.after_request
def no_cache(response):
    response.cache_control.no_store = True
    return response


@docker_upload_bp.before_app_request
def keep_solution_notes_private():
    filename = (request.view_args or {}).get('filename', '')
    if filename.replace('\\', '/').split('/')[-1].rstrip(' .').casefold() == 'solution.md':
        abort(404)
    if request.method == 'POST' and request.mimetype == 'multipart/form-data':
        if any(secure_filename(upload.filename or '').rstrip(' .').casefold() == 'solution.md'
               for _, upload in request.files.items(multi=True)):
            abort(400, 'Keep SOLUTION.md outside public uploads.')


def _replacement_target(challenge_id):
    if challenge_id is None:
        abort(404)
    challenge = db.get_or_404(Challenge, challenge_id)
    if not challenge.docker_enabled:
        abort(409, 'Enable Docker for this challenge before replacing its image.')
    return challenge


@docker_upload_bp.get('/new')
@login_required
@admin_required
def docker_upload():
    target = None
    if 'challenge_id' in request.args:
        target = _replacement_target(request.args.get('challenge_id', type=int))
    targets = [] if target else Challenge.query.filter_by(docker_enabled=True).order_by(Challenge.name).all()
    return render_template(
        'admin/docker_upload.html', target=target, targets=targets,
        upload_max_mib=current_app.config['MAX_CONTENT_LENGTH'] / 1024**2,
        default_flag_prefix=docker_builder.DEFAULT_FLAG_PREFIX,
    )


@docker_upload_bp.errorhandler(RequestEntityTooLarge)
def upload_too_large(_error):
    return jsonify(error='The upload exceeds the request size limit.'), 413


@docker_upload_bp.post('')
@login_required
@admin_required
@CSRFProtection.csrf_protect()
def start_docker_upload():
    archives = request.files.getlist('archive')
    if len(archives) != 1 or not archives[0]:
        return jsonify(error='Choose one archive to upload.'), 400
    archive = archives[0]
    filename = secure_filename(archive.filename)
    if not filename.lower().endswith(docker_builder.ARCHIVE_SUFFIXES):
        return jsonify(error='Upload a .zip, .tar, .tar.gz or .tgz file.'), 400

    app = current_app._get_current_object()
    workdir = None
    started = False
    try:
        workdir = TemporaryDirectory(prefix='blackbox-docker-upload-')
        folder = Path(workdir.name)
        path = folder / 'archive'
        digest, size = hashlib.sha256(), 0
        with path.open('wb') as output:
            while chunk := archive.stream.read(docker_builder.CHUNK_BYTES):
                size += len(chunk)
                if size > app.config['MAX_CONTENT_LENGTH']:
                    raise RequestEntityTooLarge
                digest.update(chunk)
                output.write(chunk)
        kind, context, manifest, warnings = docker_builder.prepare_archive(path, folder / 'context')
        path.unlink()
        job_id = docker_builder.start_job(
            app, workdir=workdir, kind=kind, context=context,
            filename=filename, sha256=digest.hexdigest(), user_id=current_user.id,
            manifest=manifest, warnings=warnings,
        )
        started = True
    except docker_builder.UploadError as error:
        return jsonify(error=str(error)), 400
    except OSError:
        app.logger.exception('Could not stage Docker upload')
        return jsonify(error='The archive could not be saved or read.'), 500
    finally:
        if workdir is not None and not started:
            workdir.cleanup()
    return jsonify(job_id=job_id, kind=kind, manifest=manifest, warnings=warnings), 202


def _owned_job(job_id, require_done=False):
    job = docker_builder.get_job(job_id)
    if job is None or job['user_id'] != current_user.id:
        abort(404)
    if require_done and (job['status'] != 'done' or not job['image']):
        abort(409, 'Wait for the image to finish building or loading.')
    return job


@docker_upload_bp.get('/<job_id>')
@login_required
@admin_required
def docker_upload_status(job_id):
    return jsonify(_owned_job(job_id))


def _allow_image(tag):
    """Serialize updates to the existing Docker allowlist when it is configured."""
    settings = DockerSettings.query.order_by(DockerSettings.id).with_for_update().first()
    if settings is not None and not settings.is_image_allowed(tag):
        settings.allowed_repositories = '\n'.join([*settings.get_allowed_repositories_list(), tag])


def _challenge_fields(form):
    fields = {}
    for field in docker_builder.TEXT_LIMITS:
        if field in form and form[field].strip():
            fields[field] = form[field].strip()
    for src, dest in (('docker_connection_info', 'connection'), ('docker_flag_path', 'flag_path')):
        if form.get(src, '').strip():
            fields[dest] = form[src].strip()
    try:
        for src, dest, default in (('initial_points', 'points', 500), ('decay_solves', 'decay', 30)):
            fields[dest] = int(form.get(src, default))
        fields['min_points'] = int(form.get('minimum_points', min(50, fields['points'])))
        hints = form.getlist('hint_content[]')
        costs = form.getlist('hint_cost[]')
        if len(hints) != len(costs):
            raise ValueError
        hints = [{'content': content, 'cost': int(cost or 0)} for content, cost in zip(hints, costs)]
    except ValueError as error:
        raise docker_builder.UploadError('Enter whole numbers for points, decay and hint costs.') from error
    if any(not fields.get(field) for field in ('name', 'description', 'category')):
        raise docker_builder.UploadError('Enter a name, category and description.')
    docker_builder.validate_fields(fields)
    return fields, docker_builder.validate_hints(hints)


def _connection(job):
    return job['manifest'].get('connection') or (
        'http://{host}:{port}' if job['exposed_ports'][0].split('/')[0] in {'80', '8080', '3000', '5000'}
        else 'nc {host} {port}'
    )


@docker_upload_bp.post('/<job_id>/challenge')
@login_required
@admin_required
@CSRFProtection.csrf_protect()
def create_challenge_from_docker_upload(job_id):
    job = _owned_job(job_id, require_done=True)
    try:
        fields, hints = _challenge_fields(request.form)
    except docker_builder.UploadError as error:
        abort(400, str(error))

    static_flag = fields.get('flag_mode') == 'static'
    prefix = fields.get('flag_prefix', docker_builder.DEFAULT_FLAG_PREFIX)
    flag = fields['flag'] if static_flag else f'{prefix}{{{secrets.token_hex(24)}}}'
    _allow_image(job['image'])
    challenge = Challenge(
        name=fields['name'], category=fields['category'], description=fields['description'],
        author=fields.get('author'), difficulty=fields.get('difficulty', 'easy'),
        initial_points=fields['points'], minimum_points=fields['min_points'], decay_solves=fields['decay'],
        is_visible=request.form.get('is_visible') == 'true',
        is_dynamic=request.form.get('is_dynamic') == 'true',
        requires_team=request.form.get('requires_team') == 'true',
        flag=flag, flag_case_sensitive=True,
        docker_enabled=True, docker_image=job['image'],
        docker_connection_info=fields.get('connection') or _connection(job),
        docker_flag_path=fields.get('flag_path') if not static_flag else None,
    )
    db.session.add(challenge)
    db.session.flush()
    if static_flag:
        db.session.add(ChallengeFlag(challenge_id=challenge.id, flag_value=flag, flag_label='Primary Flag'))
    for index, hint in enumerate(hints, 1):
        db.session.add(Hint(challenge_id=challenge.id, order=index, **hint))
    db.session.commit()
    cache_service.invalidate_all_challenges()
    log_audit_event(user_id=current_user.id, action='DOCKER_UPLOAD_CHALLENGE_CREATED',
                    details={'job_id': job_id, 'challenge_id': challenge.id})
    flash(f'Challenge "{challenge.name}" created.', 'success')
    return redirect(url_for('admin.edit_challenge', challenge_id=challenge.id))


@docker_upload_bp.post('/<job_id>/replace/<int:challenge_id>')
@login_required
@admin_required
@CSRFProtection.csrf_protect()
def replace_challenge_image(job_id, challenge_id):
    job = _owned_job(job_id, require_done=True)
    challenge = _replacement_target(challenge_id)
    if request.form.get('confirm') != 'true':
        abort(400, 'Confirm the image replacement.')
    _allow_image(job['image'])
    challenge.docker_image = job['image']
    db.session.commit()
    cache_service.invalidate_challenge(challenge_id)
    cache_service.invalidate_all_challenges()
    log_audit_event(user_id=current_user.id, action='DOCKER_UPLOAD_IMAGE_REPLACED',
                    details={'job_id': job_id, 'challenge_id': challenge_id})
    flash('Docker image replaced. Running instances keep their current image until restarted.', 'success')
    return redirect(url_for('admin.edit_challenge', challenge_id=challenge_id))


@docker_upload_bp.post('/<job_id>/test')
@login_required
@admin_required
@CSRFProtection.csrf_protect()
def test_docker_upload(job_id):
    from services.container_manager import container_orchestrator

    job = _owned_job(job_id, require_done=True)
    port = job['exposed_ports'][0]
    target_port = port if '/' in port else f'{port}/tcp'
    flag = f'{docker_builder.DEFAULT_FLAG_PREFIX}{{{secrets.token_hex(24)}}}'
    template = request.form.get('docker_connection_info') or _connection(job)
    try:
        docker_builder.validate_fields({'connection': template})
    except docker_builder.UploadError as error:
        return jsonify(error=str(error)), 400
    try:
        container = docker_builder._docker_client().containers.create(
            job['image'], detach=True, pids_limit=256, mem_limit='512m',
            ports={target_port: ('127.0.0.1', None)},
            environment={'FLAG': flag, 'CTF_USER_ID': str(current_user.id)},
        )
        try:
            container.start()
            result = container.exec_run('printenv FLAG')
            container.reload()
            host_port = container.ports[target_port][0]['HostPort']
        finally:
            container.remove(force=True)
        connection = container_orchestrator._build_connection_info(
            SimpleNamespace(docker_connection_info=template), '127.0.0.1', host_port,
        )
    except (docker.errors.DockerException, docker_builder.UploadError, KeyError, IndexError, TypeError):
        current_app.logger.exception('Docker test instance failed for upload %s', job_id)
        return jsonify(error='The test instance could not be checked. Check the server logs.'), 502
    return jsonify(connection=connection, flag_verified=result.exit_code == 0 and result.output.strip() == flag.encode())
