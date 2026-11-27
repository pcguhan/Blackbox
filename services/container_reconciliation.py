"""
Reconcile recorded challenge instances with the Docker daemon.
"""

import logging
import time
from datetime import datetime, timedelta, timezone

from models import db
from models.container import ContainerInstance
from services.container_manager import container_orchestrator

logger = logging.getLogger(__name__)


def reconcile_containers(app, *, expired_only=False):
    with app.app_context():
        try:
            with container_orchestrator.lifecycle_transaction() as settings:
                interval = settings.cleanup_interval_minutes
                if expired_only and not settings.auto_cleanup_expired:
                    return interval

                container_orchestrator._ensure_docker_client()
                client = container_orchestrator.docker_client
                if client is None:
                    return interval

                containers = client.containers.list(all=True, filters={'name': 'ctf-challenge'})
                by_id = {container.id: container for container in containers}
                by_name = {container.name: container for container in containers}
                instances = ContainerInstance.query.filter(db.or_(
                    ContainerInstance.status.in_(ContainerInstance.ACTIVE_STATUSES),
                    ContainerInstance.container_name.in_(by_name),
                )).with_for_update().all()
                for instance in instances:
                    container = by_id.get(instance.container_id) or by_name.get(instance.container_name)
                    _reconcile_instance(instance, container, settings, expired_only)

                if not expired_only and settings.cleanup_stale_containers:
                    tracked_names = {instance.container_name for instance in instances}
                    _remove_stale_orphans(containers, tracked_names, settings.stale_container_hours)
                return interval
        except Exception:
            logger.exception('Container reconciliation failed')
            return None


def _reconcile_instance(instance, container, settings, expired_only):
    expired = instance.is_active() and instance.is_expired() and settings.auto_cleanup_expired
    if expired:
        reason = 'Container expired'
    elif expired_only:
        return
    elif instance.status == 'stopping':
        reason = 'Completed pending container removal'
    elif instance.is_active() and container is None:
        reason = 'Container not found in Docker'
    elif instance.is_active() and container.status in ('exited', 'dead', 'created'):
        reason = f'Container has Docker status {container.status}'
    elif not instance.is_active() and container is not None and settings.cleanup_stale_containers:
        reason = 'Removed container for inactive instance'
    else:
        return

    if container is not None:
        try:
            container_orchestrator._remove_docker_container(container.id, container)
        except Exception:
            logger.exception('Failed to reconcile challenge container %s', instance.container_name)
            return
    if instance.is_active():
        instance.status = 'stopped'
        instance.error_message = reason
    container_orchestrator._log_event(
        instance, instance.status, reason, 'expire' if expired else 'reconcile',
    )


def _remove_stale_orphans(containers, tracked_names, stale_hours):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=stale_hours or 0)
    for container in containers:
        if container.name in tracked_names or not container.labels.get('ctf.session_id'):
            continue
        created = container.attrs.get('Created')
        if not created:
            continue
        try:
            if datetime.fromisoformat(created.replace('Z', '+00:00')) > cutoff:
                continue
        except (TypeError, ValueError):
            continue
        try:
            container_orchestrator._remove_docker_container(container.id, container)
        except Exception:
            logger.exception('Failed to remove orphan challenge container %s', container.name)


def run_reconciliation_loop(app, interval_seconds=60):
    while True:
        interval_minutes = reconcile_containers(app)
        time.sleep(interval_minutes * 60 if interval_minutes and interval_minutes > 0 else interval_seconds)
