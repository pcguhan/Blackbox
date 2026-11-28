"""Visibility for challenge listings and direct access."""

from models import db
from models.act_unlock import ActUnlock
from models.branching import ChallengePrerequisite, ChallengeUnlock
from models.challenge import Challenge
from models.settings import Settings
from models.submission import Solve


def participant_scope(model, user):
    """Match progress belonging to the player's current team or solo entry.

    In individual mode (teams disabled) a stored team_id is ignored and only
    the user's own records count, matching how submissions and scoring work.
    """
    if not Settings.get('teams_enabled', True, type='bool'):
        return model.user_id == user.id
    if user.team_id is not None:
        return model.team_id == user.team_id
    return db.and_(model.team_id.is_(None), model.user_id == user.id)


def unlocked_acts_for(user):
    if user.is_admin:
        return ActUnlock.get_unlocked_acts(user_id=user.id, team_id=user.team_id)
    rows = db.session.query(ActUnlock.act).filter(participant_scope(ActUnlock, user))
    return list(dict.fromkeys(['ACT I', *(act for act, in rows.order_by(ActUnlock.unlocked_at))]))


def visible_challenges_query(user):
    """Return a query for challenges the user may see, including admin previews."""
    query = Challenge.query
    if not user.is_authenticated:
        return query.filter(db.false())
    if user.is_admin:
        return query
    if not Settings.is_ctf_running():
        return query.filter(db.false())

    # Hidden branching challenges are published to a participant once unlocked.
    # A regular unpublished challenge stays private regardless of progress.
    prerequisite_solved = Solve.query.filter(
        Solve.challenge_id == ChallengePrerequisite.prerequisite_challenge_id,
        participant_scope(Solve, user),
    ).exists()
    missing_prerequisite = ChallengePrerequisite.query.filter(
        ChallengePrerequisite.challenge_id == Challenge.id, ~prerequisite_solved,
    ).exists()
    flag_unlocked = ChallengeUnlock.query.filter(
        ChallengeUnlock.challenge_id == Challenge.id,
        participant_scope(ChallengeUnlock, user),
    ).exists()
    unlock_satisfied = db.or_(
        db.and_(Challenge.unlock_mode == 'prerequisite', ~missing_prerequisite),
        db.and_(Challenge.unlock_mode == 'flag_unlock', flag_unlocked),
    )
    query = query.filter(
        Challenge.is_enabled.is_(True),
        db.or_(
            db.and_(Challenge.is_hidden.is_not(True), Challenge.is_visible.is_(True)),
            db.and_(Challenge.is_hidden.is_(True), unlock_satisfied),
        ),
        db.or_(Challenge.unlock_mode != 'prerequisite', Challenge.unlock_mode.is_(None), ~missing_prerequisite),
    )

    if Settings.get('act_system_enabled', False, type='bool'):
        query = query.filter(Challenge.act.in_(unlocked_acts_for(user)))
    if user.team_id is None and Settings.get('teams_enabled', True, type='bool'):
        if Settings.get('require_team_for_challenges', False, type='bool'):
            return query.filter(db.false())
        query = query.filter(Challenge.requires_team.is_not(True))
    return query


def is_challenge_visible(challenge, user):
    if challenge is None or not user.is_authenticated:
        return False
    if user.is_admin:
        return True
    return visible_challenges_query(user).filter(Challenge.id == challenge.id).first() is not None
