import math
import re
from collections import defaultdict

from flask import current_app
from models import db
from models.challenge import Challenge
from models.hint import HintUnlock
from models.settings import Settings
from models.submission import Solve
from models.team import Team
from models.user import User

_ROLL_NUMBER = re.compile(r'\b\d{2}[A-Z]{3}\d{4}\b', re.IGNORECASE)


def _member_identity(user):
    if not getattr(user, 'google_sub', None):
        return {'name': getattr(user, 'nickname', None) or user.username}

    full_name = user.full_name or ''
    match = _ROLL_NUMBER.search(full_name)
    roll = (getattr(user, 'roll_number', None) or getattr(user, 'registration_number', None)
            or getattr(user, 'reg_no', None) or (match.group() if match else None))
    name = _ROLL_NUMBER.sub('', full_name)
    name = ' '.join(name.replace('()', '').split())
    return {'name': name or 'Team member', 'roll_number': roll.upper() if roll else None}


class ScoringService:
    """Service for managing challenge scoring and calculations"""
    
    @staticmethod
    def calculate_dynamic_points(challenge, solve_count=None, decay_function=None):
        """
        Calculate dynamic points for a challenge based on solve count
        Supports both logarithmic and parabolic decay functions
        """
        if not challenge.is_dynamic:
            return challenge.initial_points
        
        if solve_count is None:
            solve_count = challenge.get_solves_count()
        
        if solve_count <= 1:
            return challenge.initial_points
            
        # Shift so decay begins AFTER the first solve
        solve_count -= 1
        
        # If we've reached the decay threshold, return minimum
        if solve_count >= challenge.decay_solves:
            return challenge.minimum_points
        
        if decay_function is None:
            decay_function = ScoringService.decay_function()

        max_points = challenge.initial_points
        min_points = challenge.minimum_points
        decay = challenge.decay_solves
        
        if decay_function == 'parabolic':
            # Parabolic decay (CTFd-style)
            # Formula: value = (((minimum - initial) / (decay²)) * (solves²)) + initial
            # Steeper decrease early on, then levels out
            points = (((min_points - max_points) / (decay ** 2)) * (solve_count ** 2)) + max_points
            points = math.ceil(points)
        else:
            # Logarithmic decay (default)
            # Smooth, gradual decrease using natural log
            points = max_points - (max_points - min_points) * \
                     (math.log(solve_count + 1) / math.log(decay + 1))
            points = int(points)
        
        return max(points, min_points)
    
    @staticmethod
    def decay_function():
        return Settings.get('decay_function', type='string') or current_app.config.get(
            'DECAY_FUNCTION', 'logarithmic'
        )

    @staticmethod
    def first_blood_bonus():
        return max(0, Settings.get('first_blood_bonus', 0, type='int'))

    @staticmethod
    def solve_award(base_points, override, is_first_blood, bonus):
        return (base_points if override is None else override) + (bonus if is_first_blood else 0)

    @staticmethod
    def get_challenge_solve_counts(team_based, challenge_id=None):
        """Get solve counts by challenge for the selected competition mode."""
        owner = Solve.team_id if team_based else Solve.user_id
        solves = Solve.participant_solves().filter(Solve.challenge_id.is_not(None), owner.is_not(None))
        if challenge_id is not None:
            solves = solves.filter(Solve.challenge_id == challenge_id)
        if not team_based:
            solves = solves.filter(User.is_admin.is_not(True))
        return dict(solves.with_entities(
            Solve.challenge_id, db.func.count(db.distinct(owner))
        ).group_by(Solve.challenge_id))

    @staticmethod
    def team_rosters(users):
        rosters = defaultdict(list)
        admin_teams = set()
        participant_teams = set()
        for user in users:
            if user.team_id is None:
                continue
            if user.is_admin:
                admin_teams.add(user.team_id)
            else:
                participant_teams.add(user.team_id)
                if user.is_active:
                    rosters[user.team_id].append(_member_identity(user))
        return rosters, admin_teams - participant_teams

    @staticmethod
    def get_scoreboard(team_based=True, limit=None):
        """Return ranked teams or individual participants using bulk queries."""
        challenges = {challenge.id: challenge for challenge in Challenge.query.all()}
        solves = Solve.participant_solves().with_entities(
            Solve.user_id, Solve.team_id, Solve.challenge_id, Solve.points_earned,
            Solve.base_points_override, Solve.is_first_blood, Solve.solved_at, User.is_admin,
        ).all()

        # Dynamic value counts the entrants of the competition mode (not the
        # board being requested), so every view agrees on a challenge's value.
        # Same rule as get_challenge_solve_counts, from the rows already loaded.
        mode_teams = Settings.get('teams_enabled', True, type='bool')
        owners = defaultdict(set)
        for solve in solves:
            owner = solve.team_id if mode_teams else solve.user_id
            if solve.challenge_id is None or owner is None or (not mode_teams and solve.is_admin):
                continue
            owners[solve.challenge_id].add(owner)
        counts = defaultdict(int, {cid: len(ids) for cid, ids in owners.items()})
        decay = ScoringService.decay_function()
        bonus = ScoringService.first_blood_bonus()
        values = {cid: ScoringService.calculate_dynamic_points(challenge, counts[cid], decay)
                  for cid, challenge in challenges.items() if challenge.is_dynamic}

        scores, totals, first_bloods = defaultdict(int), defaultdict(int), defaultdict(int)
        latest = {}
        for solve in solves:
            owner = solve.team_id if team_based else solve.user_id
            if owner is None:
                continue
            points = solve.points_earned
            if solve.challenge_id in values:
                points = ScoringService.solve_award(
                    values[solve.challenge_id], solve.base_points_override, solve.is_first_blood, bonus
                )
            scores[owner] += points
            if solve.challenge_id is not None:
                totals[owner] += 1
                first_bloods[owner] += int(bool(solve.is_first_blood))
            if solve.solved_at and (owner not in latest or solve.solved_at > latest[owner]):
                latest[owner] = solve.solved_at

        cost_owner = HintUnlock.team_id if team_based else HintUnlock.user_id
        costs = db.session.query(cost_owner, db.func.sum(HintUnlock.cost_paid))
        if not team_based:
            costs = costs.filter(HintUnlock.team_id.is_(None))
        for owner, cost in costs.group_by(cost_owner):
            scores[owner] -= int(cost)

        users = User.query.order_by(User.id).all()
        if team_based:
            rosters, admin_teams = ScoringService.team_rosters(users)
            entrants = [dict(id=team.id, name=team.name, affiliation=team.affiliation, members=rosters[team.id])
                        for team in Team.query.filter_by(is_active=True)
                        if team.id not in admin_teams]
        else:
            entrants = [dict(id=user.id, name=user.username, affiliation=user.full_name or '')
                        for user in users if user.is_active and not user.is_admin]

        for entry in entrants:
            owner = entry['id']
            last = latest.get(owner)
            entry.update(score=scores[owner], solves=totals[owner], first_bloods=first_bloods[owner],
                         last_solve=last.isoformat() if last else None)
        entrants.sort(key=lambda entry: (-entry['score'], entry['last_solve'] or '9999', entry['id']))
        for rank, entry in enumerate(entrants, 1):
            entry['rank'] = rank
        return entrants[:limit] if limit is not None else entrants

    @staticmethod
    def get_challenge_statistics():
        """Get statistics for all challenges"""
        challenges = Challenge.query.filter_by(is_visible=True, is_enabled=True).all()
        
        stats = []
        for challenge in challenges:
            solves = challenge.get_solves_count()
            submissions = challenge.get_submissions_count()
            
            stats.append({
                'id': challenge.id,
                'name': challenge.name,
                'category': challenge.category,
                'points': challenge.get_current_points(),
                'solves': solves,
                'submissions': submissions,
                'solve_rate': (solves / submissions * 100) if submissions > 0 else 0
            })
        
        return stats
    
    @staticmethod
    def get_user_progress(user_id):
        """Get detailed progress for a user"""
        user = User.query.get(user_id)
        if not user:
            return None
        
        solves = Solve.participant_solves().filter(Solve.user_id == user_id, Solve.challenge_id.is_not(None)).all()
        
        progress = {
            'total_score': user.get_score(),
            'challenges_solved': len(solves),
            'solves': []
        }
        
        for solve in solves:
            # Skip if challenge has been deleted
            if not solve.challenge:
                continue
                
            progress['solves'].append({
                'challenge_id': solve.challenge_id,
                'challenge_name': solve.challenge.name,
                'category': solve.challenge.category,
                'docker_enabled': solve.challenge.docker_enabled,
                'points_earned': solve.get_current_points(),
                'is_first_blood': solve.is_first_blood,
                'solved_at': solve.solved_at.isoformat()
            })
        
        return progress
    
    @staticmethod
    def get_team_progress(team_id):
        """Get detailed progress for a team"""
        team = Team.query.get(team_id)
        if not team:
            return None
        
        solves = Solve.participant_solves().filter(Solve.team_id == team_id, Solve.challenge_id.is_not(None)).all()
        
        progress = {
            'total_score': team.get_score(),
            'challenges_solved': len(solves),
            'solves': []
        }
        
        for solve in solves:
            # Skip if challenge has been deleted
            if not solve.challenge:
                continue
                
            solver = User.query.get(solve.user_id)
            progress['solves'].append({
                'challenge_id': solve.challenge_id,
                'challenge_name': solve.challenge.name,
                'category': solve.challenge.category,
                'points_earned': solve.get_current_points(),
                'is_first_blood': solve.is_first_blood,
                'solved_at': solve.solved_at.isoformat(),
                'solved_by': solver.username if solver else 'Unknown'
            })
        
        return progress
