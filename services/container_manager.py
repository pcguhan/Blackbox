"""
Docker Container Orchestration Service
Manages container lifecycle for challenge instances
"""

import io
import math
import platform
import socket
import tarfile
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from threading import RLock
from urllib.parse import urlsplit
from uuid import uuid4

import docker
from flask import current_app, request, has_request_context
from models import db
from models.container import ContainerInstance, ContainerEvent
from models.challenge import Challenge
from models.settings import DockerSettings
from utils.flag_hmac import parse_legacy_flag


class ContainerOrchestrator:
    """Orchestrates Docker containers for CTF challenges"""
    
    _lifecycle_lock = RLock()

    def __init__(self):
        self.docker_client = None
        self._client_initialized = False
    
    def _ensure_docker_client(self):
        """Ensure Docker client is initialized (lazy initialization)"""
        if self._client_initialized:
            return
        
        try:
            settings = DockerSettings.get_config()
            
            if not settings.hostname:
                # Use local Docker socket on Linux/macOS, but Docker Desktop on Windows
                if platform.system().lower() == 'windows':
                    self.docker_client = docker.DockerClient(base_url='npipe:////./pipe/docker_engine')
                else:
                    self.docker_client = docker.from_env()
            elif settings.tls_enabled and settings.ca_cert:
                # Use TLS connection
                tls_config = self._create_tls_config(settings)
                self.docker_client = docker.DockerClient(
                    base_url=settings.hostname,
                    tls=tls_config
                )
            else:
                # Plain TCP connection
                self.docker_client = docker.DockerClient(base_url=settings.hostname)
            
            # Test connection
            self.docker_client.ping()
            if current_app:
                current_app.logger.info("Docker client initialized successfully")
            self._client_initialized = True
            
        except Exception as e:
            if current_app:
                current_app.logger.error(f"Failed to initialize Docker client: {e}")
            self.docker_client = None
            self._client_initialized = False
    
    def _init_docker_client(self):
        """Initialize Docker client with settings (deprecated, use _ensure_docker_client)"""
        self._ensure_docker_client()
    
    def _create_tls_config(self, settings):
        """Create TLS configuration from certificates"""
        try:
            # Write certificates to temp files
            ca_file = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.pem')
            ca_file.write(settings.ca_cert)
            ca_file.close()
            
            client_cert_file = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.pem')
            client_cert_file.write(settings.client_cert)
            client_cert_file.close()
            
            client_key_file = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.pem')
            client_key_file.write(settings.client_key)
            client_key_file.close()
            
            tls_config = docker.tls.TLSConfig(
                client_cert=(client_cert_file.name, client_key_file.name),
                ca_cert=ca_file.name,
                verify=True
            )
            
            return tls_config
        except Exception as e:
            current_app.logger.error(f"Failed to create TLS config: {e}")
            return None
    
    @contextmanager
    def lifecycle_transaction(self):
        # SQLite ignores FOR UPDATE. The local lock also serializes operations
        # within a worker. MySQL locks the existing singleton across workers.
        with self._lifecycle_lock:
            try:
                settings = (DockerSettings.query.order_by(DockerSettings.id)
                            .populate_existing().with_for_update().first())
                if settings is None:
                    DockerSettings.get_config()
                    settings = (DockerSettings.query.order_by(DockerSettings.id)
                                .populate_existing().with_for_update().first())
                yield settings
                db.session.commit()
            except Exception:
                db.session.rollback()
                raise

    @staticmethod
    def _active_instances():
        return ContainerInstance.query.filter(
            ContainerInstance.status.in_(ContainerInstance.ACTIVE_STATUSES)
        )

    def start_container(self, challenge_id, user_id, ip_address, team_id=None):
        return self._launch_container(challenge_id, user_id, ip_address, team_id, revert=False)

    def revert_container(self, challenge_id, user_id, ip_address, team_id=None):
        return self._launch_container(challenge_id, user_id, ip_address, team_id, revert=True)

    def _launch_container(self, challenge_id, user_id, ip_address, team_id, *, revert):
        container = None
        try:
            with self.lifecycle_transaction() as settings:
                challenge = db.session.get(Challenge, challenge_id)
                if not challenge:
                    return {'success': False, 'error': 'Challenge not found'}
                if not challenge.docker_enabled or not challenge.docker_image:
                    return {'success': False, 'error': 'This challenge does not support containers'}
                if not settings.is_image_allowed(challenge.docker_image):
                    return {'success': False, 'error': 'This Docker image is not in the allowed repositories'}

                self._ensure_docker_client()
                if not self.docker_client:
                    return self._docker_unavailable()

                active = self._active_instances().order_by(ContainerInstance.id).with_for_update().all()
                owned = [row for row in active if row.user_id == user_id]
                existing = next((row for row in owned if row.challenge_id == challenge_id), None)
                if revert:
                    if not existing or existing.status != 'running':
                        return {'success': False, 'error': 'No running container found'}
                    cooldown = self._cooldown(existing, settings, 'reverting')
                    if cooldown:
                        return cooldown
                    if not self._remove_instance(existing, 'Container reverted', event_type='revert'):
                        return {'success': False, 'error': 'Failed to remove container for revert'}
                    existing.last_revert_time = datetime.utcnow()
                    active.remove(existing)
                    owned.remove(existing)
                elif existing:
                    return {
                        'success': False, 'error': 'You already have an instance for this challenge.',
                        'status': existing.status, 'container': self._instance_data(existing),
                        'existing_challenge_id': challenge_id,
                    }

                user_limit = settings.max_containers_per_user
                if user_limit is not None and len(owned) >= user_limit:
                    return {
                        'success': False, 'error': 'You have reached your active instance limit.',
                        'existing_challenge_id': owned[0].challenge_id if owned else None,
                    }
                global_limit = settings.max_concurrent_containers
                if global_limit is not None and len(active) >= global_limit:
                    return {'success': False, 'error': 'The server has reached its challenge instance limit.'}

                ports = self._available_ports(settings, active)
                if not ports:
                    return self._no_ports()
                previous = (ContainerInstance.query.filter_by(challenge_id=challenge_id, user_id=user_id)
                            .order_by(ContainerInstance.last_revert_time.desc()).with_for_update().first())
                now = datetime.utcnow()
                session_id = uuid4().hex
                instance = ContainerInstance(
                    challenge=challenge, user_id=user_id, team_id=team_id, ip_address=ip_address,
                    session_id=session_id, container_id=f'starting_{session_id}',
                    container_name=f'ctf-challenge-user{user_id}-chal{challenge_id}-{session_id}',
                    docker_image=challenge.docker_image, host_ip=self._get_docker_host(settings),
                    port=ports[0], host_port=ports[0], status='starting',
                    expires_at=now + timedelta(minutes=settings.container_lifetime_minutes),
                    last_revert_time=previous.last_revert_time if previous else None,
                )
                db.session.add(instance)
                db.session.flush()
                self._log_event(instance, 'starting', 'Starting container', 'start')

                flag = instance.get_expected_flag()
                options = self._run_options(instance, settings, flag)
                try:
                    target_port = self._target_port(challenge.docker_image)
                    for port in ports:
                        instance.port = instance.host_port = port
                        options['ports'] = {
                            target_port: (current_app.config.get('CHALLENGE_BIND_ADDRESS', '0.0.0.0'), port),
                        }
                        try:
                            container = self._run_with_pull(challenge.docker_image, options)
                            break
                        except docker.errors.APIError as error:
                            if not self._port_conflict(error):
                                raise
                            self._remove_docker_container(instance.container_name)
                    if container is None:
                        instance.status = 'error'
                        instance.error_message = self._no_ports()['error']
                        self._log_event(instance, 'error', instance.error_message, 'error')
                        return self._no_ports()

                    instance.container_id = container.id
                    if challenge.docker_flag_path and not self._inject_flag_into_container(
                        container, flag, path=challenge.docker_flag_path,
                    ):
                        raise RuntimeError('Could not write the configured flag file')

                    instance.status = 'running'
                    instance.started_at = datetime.utcnow()
                    instance.expires_at = instance.started_at + timedelta(minutes=settings.container_lifetime_minutes)
                    instance.docker_info = {
                        'short_id': container.short_id, 'name': container.name, 'image': challenge.docker_image,
                    }
                    self._log_event(instance, 'running', f'Container started on port {port}', 'start')
                    result = {'success': True, 'status': 'running', 'container': self._instance_data(instance)}
                except Exception as error:
                    current_app.logger.exception('Failed to start challenge container')
                    instance.status = 'error'
                    try:
                        self._remove_docker_container(instance.container_name, container)
                        container = None
                    except Exception:
                        instance.status = 'stopping'
                        current_app.logger.exception('Could not remove partial challenge container')
                    instance.error_message = 'Failed to start container.'
                    self._log_event(instance, instance.status, instance.error_message, 'error')
                    return {'success': False, 'error': instance.error_message}
            return result
        except Exception:
            if container is not None:
                try:
                    self._remove_docker_container(container.id, container)
                except Exception:
                    current_app.logger.exception('Could not remove container after transaction failure')
            current_app.logger.exception('Container start error')
            return {'success': False, 'error': 'Failed to start container.'}

    def _run_options(self, instance, settings, flag):
        options = {
            'name': instance.container_name,
            'detach': True,
            'network': self.CHALLENGE_NETWORK,
            'environment': {
                'CTF_USER_ID': str(instance.user_id),
                'CTF_CHALLENGE_ID': str(instance.challenge_id),
                'CTF_SESSION_ID': instance.session_id,
                'FLAG': flag,
            },
            'labels': {
                'ctf.challenge_id': str(instance.challenge_id),
                'ctf.user_id': str(instance.user_id),
                'ctf.session_id': instance.session_id,
            },
            'restart_policy': {'Name': 'no'},
            'pids_limit': 256,
            'remove': False,
        }
        if settings.max_memory_mb and settings.max_memory_mb > 0:
            options['mem_limit'] = f'{settings.max_memory_mb}m'
        if settings.max_cpu_percent and settings.max_cpu_percent > 0:
            options['nano_cpus'] = int(settings.max_cpu_percent * 10_000_000)
        return options

    def _target_port(self, image):
        try:
            return self._image_port(image)
        except docker.errors.ImageNotFound:
            self.docker_client.images.pull(image)
            return self._image_port(image)

    def _image_port(self, image):
        config = self.docker_client.images.get(image).attrs.get('Config') or {}
        target_port = next(iter(config.get('ExposedPorts') or {}), '80/tcp')
        return target_port if '/' in target_port else f'{target_port}/tcp'

    CHALLENGE_NETWORK = 'ctf_challenges'

    def _ensure_challenge_network(self):
        """Give challenge instances a bridge network with inter-container traffic off.

        With enable_icc=false one instance cannot reach another over the bridge,
        and the app is not attached to it; players still use published ports.
        A network created earlier with ICC on is recreated once nothing is
        attached to it; until then a warning is logged.
        """
        name = self.CHALLENGE_NETWORK
        try:
            network = next((n for n in self.docker_client.networks.list(names=[name]) if n.name == name), None)
            if network is not None:
                network.reload()
                if (network.attrs.get('Options') or {}).get('com.docker.network.bridge.enable_icc') == 'false':
                    return
                if network.attrs.get('Containers'):
                    current_app.logger.warning(
                        f'Docker network {name} allows inter-container traffic; it will be recreated '
                        'isolated once no containers are attached')
                    return
                network.remove()
            self.docker_client.networks.create(
                name, driver='bridge', labels={'ctf.managed': 'true'},
                options={'com.docker.network.bridge.enable_icc': 'false'})
        except Exception as error:
            current_app.logger.error(f'Could not ensure isolated challenge network {name}: {error}')

    def _run_with_pull(self, image, options):
        if options.get('network') == self.CHALLENGE_NETWORK:
            self._ensure_challenge_network()
        try:
            return self.docker_client.containers.run(image, **options)
        except docker.errors.ImageNotFound:
            self.docker_client.images.pull(image)
            return self.docker_client.containers.run(image, **options)

    @staticmethod
    def _port_conflict(error):
        message = str(error).lower()
        return 'port is already allocated' in message or 'address already in use' in message

    @staticmethod
    def _no_ports():
        return {'success': False, 'error': 'No free ports in the configured challenge port range.'}

    @staticmethod
    def _docker_unavailable():
        return {'success': False, 'error': 'Docker is not configured or unavailable. Please contact an administrator.'}

    @staticmethod
    def _cooldown(instance, settings, action):
        if instance.last_revert_time:
            end = instance.last_revert_time + timedelta(minutes=settings.revert_cooldown_minutes or 0)
            seconds = math.ceil((end - datetime.utcnow()).total_seconds())
            if seconds > 0:
                return {
                    'success': False, 'status': 'cooldown', 'remaining_seconds': seconds,
                    'error': f'Please wait {seconds} seconds before {action}.',
                }
        return None

    def _remove_docker_container(self, identifier, container=None):
        try:
            if container is None:
                container = self.docker_client.containers.get(identifier)
            container.remove(force=True)
        except docker.errors.NotFound:
            pass

    def _remove_instance(self, instance, message, *, event_type='stop'):
        identifier = instance.container_id
        if identifier.startswith('starting_'):
            identifier = instance.container_name
        try:
            self._remove_docker_container(identifier)
        except docker.errors.DockerException:
            current_app.logger.exception('Failed to remove challenge container %s', identifier)
            return False
        instance.status = 'stopped'
        self._log_event(instance, 'stopped', message, event_type)
        return True

    def stop_container(self, challenge_id, user_id, force=False, *, on_solve=False, session_id=None):
        try:
            with self.lifecycle_transaction() as settings:
                if on_solve and not settings.auto_cleanup_on_solve:
                    return {'success': True}
                query = self._active_instances().filter_by(challenge_id=challenge_id, user_id=user_id)
                if session_id is not None:
                    query = query.filter_by(session_id=session_id)
                instance = query.with_for_update().first()
                if not instance:
                    return {'success': on_solve, 'error': 'No running container found'}
                if not force:
                    cooldown = self._cooldown(instance, settings, 'stopping')
                    if cooldown:
                        return cooldown
                self._ensure_docker_client()
                if not self.docker_client:
                    return self._docker_unavailable()
                if not self._remove_instance(instance, 'Container stopped after solve' if on_solve else 'Container stopped'):
                    return {'success': False, 'error': 'Failed to stop container.'}
                return {'success': True, 'message': 'Container stopped successfully'}
        except Exception:
            current_app.logger.exception('Container stop error')
            return {'success': False, 'error': 'Failed to stop container.'}

    def force_cleanup(self, user_id, challenge_id=None):
        try:
            with self.lifecycle_transaction():
                query = self._active_instances().filter_by(user_id=user_id).filter(
                    db.or_(ContainerInstance.status.in_(('starting', 'stopping')),
                           ContainerInstance.expires_at <= datetime.utcnow()),
                )
                if challenge_id is not None:
                    query = query.filter_by(challenge_id=challenge_id)
                instances = query.with_for_update().all()
                if not instances:
                    return {'success': False, 'error': 'No expired or stuck containers found.', 'not_found': True}
                self._ensure_docker_client()
                if not self.docker_client:
                    return self._docker_unavailable()
                cleaned = sum(self._remove_instance(instance, 'Container cleaned up') for instance in instances)
                if not cleaned:
                    return {'success': False, 'error': 'Failed to clean up containers.'}
                result = {'success': True, 'message': f'Cleaned up {cleaned} container(s).', 'cleaned_count': cleaned}
                if cleaned != len(instances):
                    result['errors'] = ['Some containers could not be removed.']
                return result
        except Exception:
            current_app.logger.exception('Container cleanup error')
            return {'success': False, 'error': 'Failed to clean up containers.'}

    def get_container_status(self, challenge_id, user_id):
        instance = self._active_instances().filter_by(challenge_id=challenge_id, user_id=user_id).first()
        if not instance:
            return {'success': True, 'status': 'none', 'container': None}
        return {'success': True, 'status': instance.status, 'container': self._instance_data(instance)}

    def _instance_data(self, instance):
        host = current_app.config.get('CHALLENGE_HOST') or instance.host_ip or self._get_docker_host()
        return {
            **instance.to_dict(),
            'host_ip': host,
            'connection_info': self._build_connection_info(instance.challenge, host, instance.host_port),
        }

    def cleanup_expired_containers(self):
        """Entry point for scheduled expiration cleanup."""
        from services.container_reconciliation import reconcile_containers

        reconcile_containers(current_app._get_current_object(), expired_only=True)

    def _get_docker_host(self, settings=None):
        """Return the public hostname used in connection instructions."""
        if current_app.config.get('CHALLENGE_HOST'):
            return current_app.config['CHALLENGE_HOST']
        settings = settings if settings is not None else DockerSettings.get_config()
        if settings.hostname:
            endpoint = settings.hostname
            hostname = urlsplit(endpoint if '://' in endpoint else f'//{endpoint}').hostname
            if hostname:
                return hostname
        if has_request_context():
            return urlsplit(f'//{request.host}').hostname
        return socket.gethostname()

    def _available_ports(self, settings, active):
        used_ports = {instance.host_port or instance.port for instance in active}
        for container in self.docker_client.containers.list():
            for bindings in (container.ports or {}).values():
                for binding in bindings or ():
                    used_ports.add(int(binding['HostPort']))
        return [
            port for port in range(max(1, settings.port_range_start), min(65535, settings.port_range_end) + 1)
            if port not in used_ports
        ]

    @staticmethod
    def parse_dynamic_flag(flag_value):
        """Resolve the owner of a signed or legacy flag for flag-abuse reporting."""
        instance = ContainerInstance.from_signed_flag(flag_value)
        if instance:
            return {
                'challenge_id': instance.challenge_id, 'user_id': instance.user_id,
                'team_id': instance.team_id, 'is_valid': True, 'is_team_flag': False,
            }
        return parse_legacy_flag(flag_value)

    def _inject_flag_into_container(self, container, flag_value, path):
        """Write the flag file at the configured absolute container path."""
        flag_path = PurePosixPath(path)
        if not flag_path.is_absolute() or not flag_path.name or '..' in flag_path.parts:
            return False

        parent = str(flag_path.parent)
        if parent != '/':
            # An image may provide the destination directory without a mkdir executable.
            try:
                container.exec_run(['mkdir', '-p', parent], user='0')
            except docker.errors.DockerException:
                current_app.logger.debug('Could not create the flag directory in Docker', exc_info=True)

        data = flag_value.encode()
        tarstream = io.BytesIO()
        with tarfile.open(fileobj=tarstream, mode='w') as archive:
            file = tarfile.TarInfo(flag_path.name)
            file.size = len(data)
            file.mode = 0o666
            archive.addfile(file, io.BytesIO(data))
        return container.put_archive(parent, tarstream.getvalue())

    @staticmethod
    def _build_connection_info(challenge, host_ip, port):
        host = f'[{host_ip}]' if ':' in host_ip and not host_ip.startswith('[') else host_ip
        template = challenge.docker_connection_info or 'http://{host}:{port}'
        return template.replace('{host}', host).replace('{port}', str(port))

    @staticmethod
    def _log_event(instance, status, message, event_type):
        db.session.add(ContainerEvent(
            container_instance=instance,
            challenge_id=instance.challenge_id,
            user_id=instance.user_id,
            container_id=instance.container_id,
            ip_address=instance.ip_address,
            event_type=event_type,
            status=status,
            message=message,
        ))

    def list_available_images(self):
        """List available Docker images"""
        self._ensure_docker_client()
        
        if not self.docker_client:
            return {'success': False, 'error': 'Docker not configured'}
        
        try:
            settings = DockerSettings.get_config()
            images = self.docker_client.images.list()
            
            allowed_repos = settings.get_allowed_repositories_list()
            
            result = []
            for image in images:
                for tag in image.tags:
                    # Check if allowed
                    if allowed_repos and not any(DockerSettings.image_matches(tag, repo) for repo in allowed_repos):
                        continue
                    
                    result.append({
                        'tag': tag,
                        'id': image.short_id,
                        'size': image.attrs.get('Size', 0),
                        'created': image.attrs.get('Created', '')
                    })
            
            return {'success': True, 'images': result}
        
        except Exception as e:
            current_app.logger.error(f"Failed to list images: {e}")
            return {'success': False, 'error': str(e)}


# Global instance
container_orchestrator = ContainerOrchestrator()
