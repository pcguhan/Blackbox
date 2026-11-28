"""Shared queries for the admin console and its CSV downloads."""

import csv
from datetime import date, datetime, timedelta, timezone
from io import StringIO

from flask import Response
from sqlalchemy import func, or_
from sqlalchemy.orm import joinedload

from models.challenge import Challenge
from models.container import ContainerInstance
from models.hint import Hint, HintUnlock
from models.submission import Submission
from models.team import Team
from models.user import User
from utils.timezone import get_platform_timezone


def console_filter_args(args, *keys):
    return {key: args[key] for key in keys if args.get(key)}


def _filter_ids(query, model, args, *fields):
    for field in fields:
        value = args.get(field, type=int)
        if value is not None:
            query = query.filter(getattr(model, field) == value)
    return query


def _contains(column, value):
    return func.lower(column).contains(value.lower(), autoescape=True)


def _date_boundary(value, days=0):
    try:
        day = date.fromisoformat(value) + timedelta(days=days)
        local = get_platform_timezone().localize(datetime.combine(day, datetime.min.time()))
        return local.astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, OverflowError):
        return None


def _filter_dates(query, column, args):
    start = _date_boundary(args.get('start_date', ''))
    end = _date_boundary(args.get('end_date', ''), days=1)
    if start is not None:
        query = query.filter(column >= start)
    if end is not None:
        query = query.filter(column < end)
    return query


def submissions_query(args):
    query = Submission.query.options(
        joinedload(Submission.user), joinedload(Submission.team), joinedload(Submission.challenge)
    )
    query = _filter_ids(query, Submission, args, 'challenge_id', 'user_id', 'team_id')
    challenge_type = args.get('challenge_type')
    if challenge_type in ('dynamic', 'static'):
        query = query.filter(Submission.challenge.has(Challenge.docker_enabled == (challenge_type == 'dynamic')))
    correct = args.get('correct_only')
    if correct in ('0', '1'):
        query = query.filter(Submission.is_correct == (correct == '1'))
    query = _filter_dates(query, Submission.submitted_at, args)
    return query.order_by(Submission.submitted_at.desc(), Submission.id.desc())


def users_query(args):
    query = User.query.options(joinedload(User.team))
    query = _filter_ids(query, User, args, 'team_id')
    user_id = args.get('user_id', type=int)
    if user_id is not None:
        query = query.filter(User.id == user_id)
    search = args.get('q', '').strip()
    if search:
        query = query.filter(or_(
            _contains(User.username, search), _contains(User.full_name, search),
            _contains(User.email, search), User.team.has(_contains(Team.name, search)),
        ))
    role = args.get('role')
    if role in ('admin', 'player'):
        query = query.filter(func.coalesce(User.is_admin, False) == (role == 'admin'))
    status = args.get('status')
    if status in ('active', 'disabled'):
        query = query.filter(func.coalesce(User.is_active, False) == (status == 'active'))
    return query.order_by(User.username, User.id)


def hint_unlocks_query(args):
    query = HintUnlock.query
    query = _filter_ids(query, HintUnlock, args, 'user_id', 'team_id')
    challenge_id = args.get('challenge_id', type=int)
    if challenge_id is not None:
        query = query.filter(HintUnlock.hint.has(Hint.challenge_id == challenge_id))
    user = args.get('user', '').strip()
    if user:
        query = query.filter(HintUnlock.user.has(or_(
            _contains(User.username, user), _contains(User.full_name, user),
        )))
    team = args.get('team', '').strip()
    if team:
        query = query.filter(HintUnlock.team.has(_contains(Team.name, team)))
    challenge = args.get('challenge', '').strip()
    if challenge:
        query = query.filter(HintUnlock.hint.has(Hint.challenge.has(_contains(Challenge.name, challenge))))
    return _filter_dates(query, HintUnlock.unlocked_at, args)


def hint_log_rows(query):
    return query.options(
        joinedload(HintUnlock.user), joinedload(HintUnlock.team),
        joinedload(HintUnlock.hint).joinedload(Hint.challenge),
    ).order_by(HintUnlock.unlocked_at.desc(), HintUnlock.id.desc())


def active_containers_query(args):
    query = ContainerInstance.query.options(
        joinedload(ContainerInstance.user), joinedload(ContainerInstance.team),
        joinedload(ContainerInstance.challenge),
    ).filter(ContainerInstance.status.in_(('starting', 'running')))
    query = _filter_ids(query, ContainerInstance, args, 'challenge_id', 'user_id', 'team_id')
    status = args.get('status')
    if status in ('starting', 'running'):
        query = query.filter(ContainerInstance.status == status)
    return query.order_by(ContainerInstance.created_at.desc(), ContainerInstance.id.desc())


def _csv_cell(value):
    if isinstance(value, str) and (
        value.startswith(('\t', '\r', '\n')) or value.lstrip().startswith(('=', '+', '-', '@'))
    ):
        return "'" + value
    return value


def csv_download(filename, headers, rows):
    output = StringIO(newline='')
    writer = csv.writer(output, quoting=csv.QUOTE_ALL)
    writer.writerow(headers)
    writer.writerows(tuple(map(_csv_cell, row)) for row in rows)
    return Response(output.getvalue(), mimetype='text/csv', headers={
        'Content-Disposition': f'attachment; filename={filename}.csv',
        'Cache-Control': 'no-store',
    })
