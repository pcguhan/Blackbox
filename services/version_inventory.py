"""Collect the technology versions used by the running deployment."""

from importlib import metadata
import platform
import ssl
import subprocess

from flask import current_app
from sqlalchemy import text

from models import db
from models.challenge import Challenge
from services.cache import cache_service
from services.container_manager import container_orchestrator


# Versions recorded in static/vendor/README.md.
BROWSER_LIBRARIES = (
    ('Bootstrap', '5.3.0', 'static/vendor'),
    ('Bootstrap Icons', '1.10.0', 'static/vendor'),
    ('Socket.IO client', '4.5.4', 'static/vendor and cdn.socket.io'),
)
IMAGE_VERSION_KEYS = {
    'ALPINE_VERSION', 'DEBIAN_VERSION', 'GOLANG_VERSION', 'MARIADB_VERSION', 'MYSQL_VERSION',
    'NGINX_VERSION', 'NODE_VERSION', 'PHP_VERSION', 'PYTHON_VERSION', 'REDIS_VERSION',
    'RUBY_VERSION', 'UBUNTU_VERSION',
}


def _probe(label, operation):
    try:
        return operation()
    except Exception as error:
        current_app.logger.debug('Version probe for %s failed: %s', label, type(error).__name__)
        return None


def _database_version():
    query = 'SELECT sqlite_version()' if db.engine.dialect.name == 'sqlite' else 'SELECT VERSION()'
    with db.engine.connect() as connection:
        return connection.execute(text(query)).scalar()


def _docker_cli_version():
    result = subprocess.run(['docker', '--version'], capture_output=True, text=True, timeout=5, check=True)
    return result.stdout.strip().removeprefix('Docker version ')


def _describe_image(image, label):
    config = image.attrs.get('Config') or {}
    versions = {}
    for variable in config.get('Env') or []:
        key, separator, value = variable.partition('=')
        if separator and key in IMAGE_VERSION_KEYS:
            versions[key.removesuffix('_VERSION').title()] = value
    return {
        'name': label,
        'image': ', '.join(image.tags) or image.short_id,
        'versions': versions,
        'created': (image.attrs.get('Created') or '')[:10],
    }


def _docker_versions():
    container_orchestrator._ensure_docker_client()
    client = container_orchestrator.docker_client
    host, services, challenges = [], [], []
    if client is None:
        return host, services, challenges

    engine = _probe('Docker Engine', client.version) or {}
    details = _probe('Docker host', client.info) or {}
    containerd = next((item.get('Version') for item in engine.get('Components', [])
                      if item.get('Name') == 'containerd'), None)
    for name, version in (
        ('Docker Engine', engine.get('Version')), ('Docker API', engine.get('ApiVersion')),
        ('containerd', containerd), ('Host OS', details.get('OperatingSystem')),
        ('Host kernel', details.get('KernelVersion')),
    ):
        if version:
            host.append({'name': name, 'version': version})

    for container in _probe('service containers', client.containers.list) or []:
        label = (container.labels or {}).get('com.docker.compose.service')
        if label:
            row = _probe(label, lambda: _describe_image(container.image, label))
            if row:
                services.append(row)

    images = (Challenge.query.with_entities(Challenge.name, Challenge.docker_image)
              .filter(Challenge.docker_image.isnot(None), Challenge.docker_image != '')
              .order_by(Challenge.name))
    for name, image_name in images:
        row = _probe(name, lambda: _describe_image(client.images.get(image_name), name))
        challenges.append(row or {'name': name, 'image': image_name, 'versions': {}, 'created': ''})
    return host, sorted(services, key=lambda row: row['name']), challenges


def collect_versions():
    runtime = (
        ('Python', platform.python_version()),
        ('OpenSSL (Python)', ssl.OPENSSL_VERSION),
        ('App OS', _probe('app OS', lambda: platform.freedesktop_os_release()['PRETTY_NAME'])
         or platform.platform()),
        ('Database server', _probe('database', _database_version)),
        ('Redis server', _probe('Redis', lambda: cache_service.redis_client.info('server').get('redis_version'))),
        ('Docker CLI', _probe('Docker CLI', _docker_cli_version)),
    )
    host, services, challenges = _probe('Docker inventory', _docker_versions) or ([], [], [])
    packages = [{'name': name, 'version': dist.version} for dist in metadata.distributions()
                if (name := dist.metadata.get('Name'))]
    return {
        'runtime': [{'name': name, 'version': version} for name, version in runtime if version],
        'host': host,
        'services': services,
        'challenge_images': challenges,
        'browser_libraries': [{'name': name, 'version': version, 'source': source}
                              for name, version, source in BROWSER_LIBRARIES],
        'python_packages': sorted(packages, key=lambda row: row['name'].lower()),
    }
