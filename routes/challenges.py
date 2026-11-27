from flask import Blueprint, render_template, request, jsonify, flash, redirect, url_for, current_app
from flask_login import login_required, current_user
from models import db
from models.challenge import Challenge
from models.submission import Submission, Solve
from models.file import ChallengeFile
from models.user import User
from services.scoring import ScoringService
from services.cache import cache_service
from services.challenge_catalog import (
    is_challenge_visible, participant_scope, unlocked_acts_for, visible_challenges_query,
)
from services.websocket import WebSocketService
from utils.audit import log_audit_event
from datetime import datetime
import re

challenges_bp = Blueprint('challenges', __name__, url_prefix='/challenges')


# Blueprint-level exception handler to ensure AJAX calls get JSON errors
@challenges_bp.errorhandler(Exception)
def _handle_challenges_exception(error):
    from werkzeug.exceptions import HTTPException

    if isinstance(error, HTTPException):
        return jsonify(success=False, message=error.name), error.code

    import traceback
    current_app.logger.error(f"Unhandled exception in challenges blueprint: {error}\n{traceback.format_exc()}")
    # Return JSON for AJAX/Fetch callers to avoid JSON.parse errors on HTML 500 pages
    return jsonify({'success': False, 'message': 'Internal server error'}), 500

@challenges_bp.route('/')
@login_required
def list_challenges():
    from models.settings import Settings
    
    # Check CTF status
    ctf_status = Settings.get_ctf_status()
    
    # Check if CTF has started (admins bypass this check)
    if not current_user.is_admin and ctf_status == 'not_started':
        start_time = Settings.get('ctf_start_time', type='datetime')
        return render_template('countdown.html', 
                             start_time=start_time,
                             page_title='Challenges',
                             return_url=url_for('challenges.list_challenges'))
    
    # If CTF is paused or ended, show status page instead of challenges
    if not current_user.is_admin and ctf_status in ('paused', 'ended'):
        return render_template('ctf_status.html', status=ctf_status)
    
    # Check if teams are enabled at all
    teams_enabled = Settings.get('teams_enabled', default=True, type='bool')
    
    # Check if team is required globally (only matters if teams are enabled)
    require_team = Settings.get('require_team_for_challenges', default=False, type='bool')
    team = current_user.get_team()
    
    # If teams are enabled AND team is required globally and user is not in a team (and not admin), show message
    if teams_enabled and require_team and not team and not current_user.is_admin:
        flash('You must join a team to view and solve challenges. Please join or create a team first.', 'warning')
        return redirect(url_for('teams.list_teams'))
    
    act_system_enabled = Settings.get('act_system_enabled', default=False, type='bool')
    unlocked_acts = unlocked_acts_for(current_user) if act_system_enabled else []
    category_order = (Challenge.category, Challenge.name)
    order = (Challenge.act, *category_order) if act_system_enabled else category_order
    challenges = visible_challenges_query(current_user).order_by(*order).all()

    # Batch check which challenges are solved (single query instead of N queries)
    # This is the key optimization - replaces N individual queries with 1 batch query
    challenge_ids = [c.id for c in challenges]
    solved_ids = {
        challenge_id for challenge_id, in db.session.query(Solve.challenge_id).filter(
            participant_scope(Solve, current_user),
            Solve.challenge_id.in_(challenge_ids),
        )
    }

    # Organize challenges by ACT, then by category within each ACT (or just by category if ACT system disabled)
    acts = {}
    for challenge in challenges:
        act_name = (challenge.act or 'Uncategorized') if act_system_enabled else 'Challenges'

        # Initialize ACT dict if needed
        if act_name not in acts:
            acts[act_name] = {}
        
        # Initialize category within ACT if needed
        if challenge.category not in acts[act_name]:
            acts[act_name][challenge.category] = []
        
        # Check if solved (using pre-loaded data)
        solved = challenge.id in solved_ids
        
        challenge_data = challenge.to_dict(include_flag=False)
        challenge_data['solved'] = solved
        challenge_data['locked'] = False
        challenge_data['requires_team'] = challenge.requires_team

        if current_user.is_admin:
            missing = challenge.get_missing_prerequisites(current_user.id, current_user.team_id)
            challenge_data['locked'] = bool(missing) or not challenge.is_unlocked_for_user(
                current_user.id, current_user.team_id,
            )
            if missing:
                challenge_data['missing_prerequisites'] = [c.name for c in missing]

        acts[act_name][challenge.category].append(challenge_data)
    
    return render_template('challenges.html', 
                          acts=acts, 
                          unlocked_acts=unlocked_acts, 
                          team=team,
                          act_system_enabled=act_system_enabled)


@challenges_bp.route('/<int:challenge_id>')
@login_required
def view_challenge(challenge_id):
    """View challenge details"""
    challenge = db.session.get(Challenge, challenge_id)
    
    if not is_challenge_visible(challenge, current_user):
        return render_template('errors/404.html'), 404

    team = current_user.get_team()
    solved = challenge.solves.filter(participant_scope(Solve, current_user)).first() is not None

    challenge_data = challenge.to_dict(include_flag=False)
    challenge_data['solved'] = solved
    challenge_data['requires_team'] = challenge.requires_team
    
    # Get challenge files and images
    challenge_files = ChallengeFile.query.filter_by(challenge_id=challenge_id, is_image=False).all()
    files_data = [f.to_dict() for f in challenge_files]
    
    challenge_images = ChallengeFile.query.filter_by(challenge_id=challenge_id, is_image=True).all()
    images_data = [{'url': img.get_download_url(), 'original_filename': img.original_filename, 'id': img.id} for img in challenge_images]
    
    # Get hints
    from models.hint import Hint, HintUnlock
    hints = Hint.query.filter_by(challenge_id=challenge_id).order_by(Hint.order).all()
    hints_data = []
    for hint in hints:
        unlocked = hint.unlocks.filter(participant_scope(HintUnlock, current_user)).first() is not None

        hint_info = {
            'id': hint.id,
            'cost': hint.cost,
            'order': hint.order,
            'unlocked': unlocked,
        }
        
        if unlocked or current_user.is_admin:
            hint_info['content'] = hint.content
        
        hints_data.append(hint_info)
    
    # Check if challenge has branching (multiple paths)
    from models.branching import ChallengeFlag, ChallengeUnlock
    challenge_flags = ChallengeFlag.query.filter_by(challenge_id=challenge_id).all()
    has_branching = (
        any(flag.unlocks_challenge_id for flag in challenge_flags)
        if challenge.docker_enabled else len(challenge_flags) > 1
    )
    challenge_data['has_branching'] = has_branching
    
    # Get unlocked paths for this user/team
    unlocked_paths = []
    if solved and has_branching:
        # Find all flags from THIS challenge that unlock other challenges
        unlocking_flags = ChallengeFlag.query.filter(
            ChallengeFlag.challenge_id == challenge_id,
            ChallengeFlag.unlocks_challenge_id.isnot(None)
        ).all()
        
        # For each unlocking flag, check if user has unlocked it
        for flag in unlocking_flags:
            unlock_query = ChallengeUnlock.query.filter(
                ChallengeUnlock.unlocked_by_flag_id == flag.id
            )
            
            unlock_query = unlock_query.filter(participant_scope(ChallengeUnlock, current_user))
            unlock = unlock_query.first()
            
            if unlock:
                unlocked_challenge = Challenge.query.get(flag.unlocks_challenge_id)
                if is_challenge_visible(unlocked_challenge, current_user):
                    unlocked_paths.append({
                        'challenge_name': unlocked_challenge.name,
                        'category': unlocked_challenge.category,
                        'flag_label': flag.flag_label if flag.flag_label else 'primary flag'
                    })
    
    challenge_data['unlocked_paths'] = unlocked_paths
    
    # Get list of solvers (users/teams who solved this challenge)
    solvers = []
    from models.settings import Settings
    teams_enabled = Settings.get('teams_enabled', True)
    team_mode = Settings.get('team_mode', False)
    
    if team_mode:
        # Team mode: show teams that solved
        team_solves = Solve.query.filter(
            Solve.challenge_id == challenge_id,
            Solve.team_id.isnot(None)
        ).order_by(Solve.solved_at).all()
        
        seen_teams = set()
        for solve in team_solves:
            if solve.team_id and solve.team_id not in seen_teams:
                seen_teams.add(solve.team_id)
                from models.team import Team
                team_obj = Team.query.get(solve.team_id)
                if team_obj:
                    solvers.append({
                        'name': team_obj.name,
                        'is_team': True,
                        'solved_at': solve.solved_at
                    })
    else:
        # Solo mode: show users that solved
        user_solves = Solve.query.filter(
            Solve.challenge_id == challenge_id,
            Solve.user_id.isnot(None)
        ).order_by(Solve.solved_at).all()
        
        seen_users = set()
        for solve in user_solves:
            if solve.user_id and solve.user_id not in seen_users:
                seen_users.add(solve.user_id)
                user_obj = User.query.get(solve.user_id)
                if user_obj:
                    solvers.append({
                        'name': user_obj.username,
                        'is_team': False,
                        'solved_at': solve.solved_at
                    })
    
    challenge_data['solvers'] = solvers
    challenge_data['solver_count'] = len(solvers)
    
    return render_template('challenge_detail.html', 
                          challenge=challenge_data, 
                          files=files_data,
                          images=images_data,
                          hints=hints_data,
                          team=team)


@challenges_bp.route('/<int:challenge_id>/submit', methods=['POST'])
@login_required
def submit_flag(challenge_id):
    """Submit a flag for a challenge"""
    from models.settings import Settings
    
    challenge = db.session.get(Challenge, challenge_id)
    if not is_challenge_visible(challenge, current_user):
        return jsonify(success=False, message='Challenge not found'), 404
    if current_user.is_admin and not Settings.is_ctf_running():
        return jsonify(success=False, message='The event is currently unavailable.'), 403

    team = current_user.get_team()
    team_id = team.id if team else None

    submitted_flag = request.form.get('flag', '').strip()
    
    if not submitted_flag:
        return jsonify({'success': False, 'message': 'Please enter a flag'}), 400
    
    
    
    # PER-TEAM LOCK: scope serialization to this team/user + this challenge only,
    # instead of the whole Challenge row, so unrelated teams' submissions for the
    # same challenge stop queuing behind each other. NX+EX means it self-expires
    # in 10s even if this request dies before finishing, so we don't need a
    # try/finally to release it.
    solve_lock_key = f"solve_lock:{challenge_id}:{team_id or current_user.id}"
    if not cache_service.redis_client.set(solve_lock_key, "1", nx=True, ex=10):
        return jsonify({
            'success': False,
            'message': 'Please wait a moment before resubmitting.'
        }), 429
    # TOCTOU FIX: Acquire row-level lock on the Challenge row BEFORE checking
    # already_solved. This serializes concurrent flag submissions for the same
    # challenge, preventing double-solve and double first-blood race conditions.
    # The lock is held until the transaction commits or rolls back.
    Challenge.query.with_for_update().get(challenge_id)
    
    # Check if already solved (by user or team) — now protected by the lock above
    already_solved = challenge.solves.filter(participant_scope(Solve, current_user)).first() is not None
    
    # Check if this challenge has branching flags (allows re-submission for different paths)
    from models.branching import ChallengeFlag
    challenge_flags = ChallengeFlag.query.filter_by(challenge_id=challenge_id).all()
    has_branching = len(challenge_flags) > 1
    
    # If already solved and challenge has no branching, reject submission
    if already_solved and (challenge.docker_enabled or not has_branching):
        return jsonify({'success': False, 'message': 'This challenge has already been solved'}), 400
    
    # Check max attempts limit (0 means unlimited)
    attempts = Submission.query.filter(Submission.challenge_id == challenge_id, participant_scope(Submission, current_user))
    if challenge.max_attempts and challenge.max_attempts > 0:
        if attempts.count() >= challenge.max_attempts:
            return jsonify({
                'success': False,
                'message': f'Maximum attempts ({challenge.max_attempts}) reached for this challenge'
            }), 400
    
    # Rate limiting check (prevent brute force)
    rate_limit_key = f'submissions:{current_user.id}:{challenge_id}'
    is_allowed, remaining = cache_service.check_rate_limit(rate_limit_key, limit=10, window=60)
    
    if not is_allowed:
        return jsonify({
            'success': False,
            'message': 'Too many attempts. Please wait before trying again.'
        }), 429
    
    # Check the flag
    matched_flag = challenge.check_flag(submitted_flag, team_id=team_id, user_id=current_user.id)
    is_correct = matched_flag is not None
    
    # ANTI-FLAG-SHARING: Check if submitted flag is a dynamic flag from another team
    if not is_correct and challenge.docker_enabled:
        from services.container_manager import ContainerOrchestrator
        from models.flag_abuse import FlagAbuseAttempt
        
        parsed_flag = ContainerOrchestrator.parse_dynamic_flag(submitted_flag)
        if parsed_flag and parsed_flag.get('is_valid'):
            # This is a valid dynamic flag format
            flag_challenge_id = parsed_flag.get('challenge_id')
            flag_team_id = parsed_flag.get('team_id')
            flag_instance_id = parsed_flag.get('user_id')  # This is user_id for user flags
            
            # Check if this flag belongs to a different team/user
            is_wrong_team = False
            actual_owner_team_id = None
            actual_owner_user_id = None
            
            if flag_challenge_id == challenge_id:
                # Flag is for this challenge
                if parsed_flag.get('is_team_flag') and flag_team_id:
                    # It's a team flag - check if it's a different team
                    if team_id and flag_team_id != team_id:
                        is_wrong_team = True
                        actual_owner_team_id = flag_team_id
                    elif not team_id and flag_team_id:
                        # User not in team trying to use team's flag
                        is_wrong_team = True
                        actual_owner_team_id = flag_team_id
                elif not parsed_flag.get('is_team_flag') and flag_instance_id:
                    # It's a user flag (no team) - the flag contains user_id
                    flag_user_id = flag_instance_id
                    
                    if flag_user_id != current_user.id or (flag_team_id and flag_team_id != team_id):
                        is_wrong_team = True
                        actual_owner_user_id = flag_user_id
                        actual_owner_team_id = flag_team_id

            # Log the flag sharing attempt
            if is_wrong_team:
                # Validate that referenced entities exist (to avoid FK constraint errors)
                from models.team import Team
                from models.user import User
                
                if actual_owner_team_id:
                    team_exists = Team.query.get(actual_owner_team_id) is not None
                    if not team_exists:
                        actual_owner_team_id = None  # Team was deleted, set to NULL
                
                if actual_owner_user_id:
                    user_exists = User.query.get(actual_owner_user_id) is not None
                    if not user_exists:
                        actual_owner_user_id = None  # User was deleted, set to NULL
                
                # Analyze temporal patterns to determine severity
                pattern_analysis = FlagAbuseAttempt.analyze_temporal_patterns(
                    challenge_id=challenge_id,
                    submitting_team_id=team_id,
                    actual_team_id=actual_owner_team_id,
                    time_window_minutes=15
                )
                
                # Determine severity based on pattern analysis
                severity = pattern_analysis.get('severity', 'warning')
                pattern_notes = pattern_analysis.get('notes', '')
                
                # Resolve owner name for nicer notes/logs
                owner_label = None
                try:
                    if actual_owner_team_id:
                        owner = Team.query.get(actual_owner_team_id)
                        owner_label = f"team {owner.name}" if owner else f"team {actual_owner_team_id}"
                    elif actual_owner_user_id:
                        owner = User.query.get(actual_owner_user_id)
                        owner_label = f"user {owner.username}" if owner else f"user {actual_owner_user_id}"
                    else:
                        owner_label = "deleted user/team"
                except Exception:
                    owner_label = f"team {actual_owner_team_id}" if actual_owner_team_id else f"user {actual_owner_user_id or 'unknown'}"
                
                # Build comprehensive notes
                base_note = f'Attempted to submit flag belonging to {owner_label}'
                if pattern_notes:
                    full_notes = f"{base_note}. {pattern_notes}"
                else:
                    full_notes = base_note

                abuse_record = FlagAbuseAttempt(
                    user_id=current_user.id,
                    team_id=team_id,
                    challenge_id=challenge_id,
                    submitted_flag=submitted_flag,
                    actual_team_id=actual_owner_team_id,
                    actual_user_id=actual_owner_user_id,
                    ip_address=request.remote_addr,
                    severity=severity,
                    notes=full_notes
                )
                db.session.add(abuse_record)
                
                # Log with appropriate severity level
                if severity == 'critical':
                    current_app.logger.error(
                        f"FLAG SHARING - CRITICAL PATTERN: User {current_user.id} (team {team_id}) "
                        f"submitted flag for challenge {challenge_id} belonging to {owner_label}. {pattern_notes}"
                    )
                elif severity == 'suspicious':
                    current_app.logger.warning(
                        f"FLAG SHARING - SUSPICIOUS: User {current_user.id} (team {team_id}) "
                        f"submitted flag for challenge {challenge_id} belonging to {owner_label}. {pattern_notes}"
                    )
                else:
                    current_app.logger.warning(
                        f"FLAG SHARING ATTEMPT: User {current_user.id} (team {team_id}) "
                        f"submitted flag for challenge {challenge_id} that belongs to {owner_label}"
                    )
    
    # Create submission record
    submission = Submission(
        user_id=current_user.id,
        challenge_id=challenge_id,
        team_id=team_id,
        submitted_flag=submitted_flag,
        is_correct=is_correct,
        ip_address=request.remote_addr
    )
    db.session.add(submission)
    
    log_audit_event(
        user_id=current_user.id, 
        team_id=team_id, 
        action='SUBMIT_FLAG', 
        details={'challenge_id': challenge_id, 'is_correct': is_correct}
    )
    
    if is_correct:
        # DETECT exact regex-based flag sharing (admin-controlled per-challenge)
        try:
            from models.flag_abuse import FlagAbuseAttempt
            from datetime import timedelta, datetime

            # Only consider when this flag was matched via a regex flag and the challenge has monitoring enabled
            if hasattr(matched_flag, 'is_regex') and matched_flag.is_regex and getattr(challenge, 'detect_regex_sharing', False):
                # Time window (minutes) to consider prior identical submissions. Configurable via app config.
                window_minutes = int(current_app.config.get('REGEX_SHARING_WINDOW_MINUTES', 60))
                cutoff = datetime.utcnow() - timedelta(minutes=window_minutes)

                # Find a prior correct submission of the exact same submitted_flag by another team/user within the window
                prior_query = Submission.query.filter(
                    Submission.challenge_id == challenge_id,
                    Submission.submitted_flag == submitted_flag,
                    Submission.is_correct == True,
                    Submission.submitted_at >= cutoff
                )

                # Exclude current team/user
                if team_id:
                    prior_query = prior_query.filter(Submission.team_id.isnot(None), Submission.team_id != team_id)
                else:
                    prior_query = prior_query.filter(Submission.user_id.isnot(None), Submission.user_id != current_user.id)

                prior = prior_query.order_by(Submission.submitted_at.asc()).first()

                if prior:
                    actual_owner_team_id = prior.team_id
                    actual_owner_user_id = prior.user_id

                    # Validate that referenced entities exist (to avoid FK constraint errors)
                    from models.team import Team
                    from models.user import User
                    
                    if actual_owner_team_id:
                        team_exists = Team.query.get(actual_owner_team_id) is not None
                        if not team_exists:
                            actual_owner_team_id = None
                    
                    if actual_owner_user_id:
                        user_exists = User.query.get(actual_owner_user_id) is not None
                        if not user_exists:
                            actual_owner_user_id = None

                    # Resolve owner label (team name or username) for nicer notes/logs
                    owner_label = None
                    try:
                        if actual_owner_team_id:
                            owner = Team.query.get(actual_owner_team_id)
                            owner_label = f"team {owner.name}" if owner and owner.name else f"team {actual_owner_team_id}"
                        elif actual_owner_user_id:
                            owner = User.query.get(actual_owner_user_id)
                            owner_label = f"user {owner.username}" if owner and owner.username else f"user {actual_owner_user_id}"
                        else:
                            owner_label = "deleted user/team"
                    except Exception:
                        owner_label = f"team {actual_owner_team_id}" if actual_owner_team_id else f"user {actual_owner_user_id or 'unknown'}"

                    # Record a FlagAbuseAttempt for this exact-match regex sharing
                    notes = f'Exact regex-derived flag "{submitted_flag}" previously submitted by {owner_label} at {prior.submitted_at.isoformat()}'

                    abuse_record = FlagAbuseAttempt(
                        user_id=current_user.id,
                        team_id=team_id,
                        challenge_id=challenge_id,
                        submitted_flag=submitted_flag,
                        actual_team_id=actual_owner_team_id,
                        actual_user_id=actual_owner_user_id,
                        ip_address=request.remote_addr,
                        severity='suspicious',
                        notes=notes
                    )
                    db.session.add(abuse_record)
                    current_app.logger.warning(f"Regex-sharing detected: {notes}")
                else:
                    # If no exact-match prior submission was found, also check for prior
                    # submissions that match the same regex pattern (different concrete strings).
                    try:
                        flags_pattern = None
                        if matched_flag.is_case_sensitive:
                            flags_pattern = re.compile(matched_flag.flag_value)
                        else:
                            flags_pattern = re.compile(matched_flag.flag_value, re.IGNORECASE)

                        broader_query = Submission.query.filter(
                            Submission.challenge_id == challenge_id,
                            Submission.is_correct == True,
                            Submission.submitted_at >= cutoff
                        )

                        # Exclude current team/user
                        if team_id:
                            broader_query = broader_query.filter(Submission.team_id.isnot(None), Submission.team_id != team_id)
                        else:
                            broader_query = broader_query.filter(Submission.user_id.isnot(None), Submission.user_id != current_user.id)

                        # Iterate prior correct submissions and check whether their submitted_flag
                        # would also match the same regex pattern. This detects when different
                        # users submitted different concrete values that both match the same regex.
                        for prior_row in broader_query.order_by(Submission.submitted_at.asc()).limit(50).all():
                            try:
                                if flags_pattern.fullmatch(prior_row.submitted_flag or ''):
                                    actual_owner_team_id = prior_row.team_id
                                    actual_owner_user_id = prior_row.user_id

                                    # Validate that referenced entities exist (to avoid FK constraint errors)
                                    from models.team import Team
                                    from models.user import User
                                    
                                    if actual_owner_team_id:
                                        team_exists = Team.query.get(actual_owner_team_id) is not None
                                        if not team_exists:
                                            actual_owner_team_id = None
                                    
                                    if actual_owner_user_id:
                                        user_exists = User.query.get(actual_owner_user_id) is not None
                                        if not user_exists:
                                            actual_owner_user_id = None

                                    # Resolve owner label (team name or username) for nicer notes/logs
                                    owner_label = None
                                    try:
                                        if actual_owner_team_id:
                                            owner = Team.query.get(actual_owner_team_id)
                                            owner_label = f"team {owner.name}" if owner and owner.name else f"team {actual_owner_team_id}"
                                        elif actual_owner_user_id:
                                            owner = User.query.get(actual_owner_user_id)
                                            owner_label = f"user {owner.username}" if owner and owner.username else f"user {actual_owner_user_id}"
                                        else:
                                            owner_label = "deleted user/team"
                                    except Exception:
                                        owner_label = f"team {actual_owner_team_id}" if actual_owner_team_id else f"user {actual_owner_user_id or 'unknown'}"

                                    notes = (
                                        f'Regex-derived flag pattern "{matched_flag.flag_value}" matched prior submission "{prior_row.submitted_flag}" '
                                        f'by {owner_label} at {prior_row.submitted_at.isoformat()}'
                                    )

                                    abuse_record = FlagAbuseAttempt(
                                        user_id=current_user.id,
                                        team_id=team_id,
                                        challenge_id=challenge_id,
                                        submitted_flag=submitted_flag,
                                        actual_team_id=actual_owner_team_id,
                                        actual_user_id=actual_owner_user_id,
                                        ip_address=request.remote_addr,
                                        severity='suspicious',
                                        notes=notes
                                    )
                                    db.session.add(abuse_record)
                                    current_app.logger.warning(f"Regex-sharing (pattern match) detected: {notes}")
                                    break
                            except Exception:
                                # ignore per-row match errors and continue
                                continue
                    except re.error:
                        # Invalid regex - nothing to do here (should be validated earlier)
                        pass
        except Exception as e:
            current_app.logger.debug(f"Regex-sharing detection error: {e}")
        # Calculate points at time of solve
        from services.scoring import ScoringService
        current_solve_count = challenge.get_solves_count()
        points = ScoringService.calculate_dynamic_points(challenge, solve_count=current_solve_count)
        
        # Check if flag has a points override
        if hasattr(matched_flag, 'points_override') and matched_flag.points_override:
            points = matched_flag.points_override
        
        # Check if this is first blood (first solve of this challenge)
        from models.settings import Settings
        is_first_blood = False
        first_blood_bonus = Settings.get('first_blood_bonus', 0, type='int')
        
        # NOTE: Challenge row is already locked via with_for_update() at the top of
        # submit_flag(). Both the already_solved check and this first-blood check
        # are serialized under that single lock — no separate lock needed here.
        
        existing_solves = Solve.query.filter_by(challenge_id=challenge_id).filter(
            Solve.challenge_id != None  # Exclude manual adjustments
        ).count()
        
        if existing_solves == 0:
            is_first_blood = True
            if first_blood_bonus > 0:
                points += first_blood_bonus
        
        # Create solve record - marks challenge as solved for entire team
        solve = Solve(
            user_id=current_user.id,
            challenge_id=challenge_id,
            flag_id=matched_flag.id if hasattr(matched_flag, 'id') else None,
            team_id=team_id,
            points_earned=points,
            is_first_blood=is_first_blood
        )
        db.session.add(solve)
        
        # Handle challenge unlocking via flags and prerequisites
        from models.branching import ChallengeUnlock, ChallengePrerequisite
        unlocked_challenges = []
        unlocked_act = None
        
        # Handle ACT unlocking if this challenge unlocks a new ACT (only if ACT system enabled)
        act_system_enabled = Settings.get('act_system_enabled', default=False, type='bool')
        if (act_system_enabled and challenge.unlocks_act and not current_user.is_admin
                and challenge.unlocks_act not in unlocked_acts_for(current_user)):
            from models.act_unlock import ActUnlock

            db.session.add(ActUnlock(
                act=challenge.unlocks_act,
                user_id=None if team_id else current_user.id,
                team_id=team_id,
                unlocked_by_challenge_id=challenge_id,
            ))
            unlocked_act = challenge.unlocks_act
            current_app.logger.info(
                f"ACT {challenge.unlocks_act} unlocked for "
                f"{'team ' + str(team_id) if team_id else 'user ' + str(current_user.id)} "
                f"by solving challenge {challenge_id} ({challenge.name})"
            )

        # Check if solving this challenge unlocks any prerequisite-gated challenges
        prerequisite_dependents = ChallengePrerequisite.query.filter_by(
            prerequisite_challenge_id=challenge_id
        ).all()
        
        for dependent in prerequisite_dependents:
            dependent_challenge = dependent.challenge
            
            # Check if all prerequisites are now met for this dependent challenge
            if dependent_challenge.unlock_mode == 'prerequisite':
                if is_challenge_visible(dependent_challenge, current_user):
                    unlocked_challenges.append({
                        'id': dependent_challenge.id,
                        'name': dependent_challenge.name,
                        'category': dependent_challenge.category
                    })
                    current_app.logger.info(
                        f"Challenge {dependent_challenge.id} ({dependent_challenge.name}) now visible for "
                        f"{'team ' + str(team_id) if team_id else 'user ' + str(current_user.id)} "
                        f"after completing prerequisite {challenge_id} ({challenge.name})"
                    )
        
        if hasattr(matched_flag, 'unlocks_challenge_id') and matched_flag.unlocks_challenge_id:
            # Check if this specific path/challenge was already unlocked by this user/team
            unlock_query = ChallengeUnlock.query.filter(
                ChallengeUnlock.challenge_id == matched_flag.unlocks_challenge_id
            )
            
            unlock_query = unlock_query.filter(participant_scope(ChallengeUnlock, current_user))

            existing_unlock = unlock_query.first()
            
            if existing_unlock:
                # Path already unlocked, inform user but don't create duplicate
                unlocked_challenge = Challenge.query.get(matched_flag.unlocks_challenge_id)
                name = unlocked_challenge.name if is_challenge_visible(unlocked_challenge, current_user) else 'this challenge'
                return jsonify({
                    'success': False,
                    'message': f'You have already unlocked the path to "{name}" with a different flag.'
                }), 400
            
            # This flag unlocks another challenge - create unlock record (per-user/team)
            # Create unlock record. The DB requires a non-null user_id, so
            # always store the current user as the actor. We also include
            # team_id when this is a team-based unlock so it can be treated
            # as a team-wide unlock semantically (team_id != None).
            unlock_record = ChallengeUnlock(
                user_id=current_user.id,
                team_id=team_id if team_id else None,
                challenge_id=matched_flag.unlocks_challenge_id,
                unlocked_by_flag_id=matched_flag.id
            )
            db.session.add(unlock_record)
            
            # Get the unlocked challenge details
            unlocked_challenge = Challenge.query.get(matched_flag.unlocks_challenge_id)
            if is_challenge_visible(unlocked_challenge, current_user):
                unlocked_challenges.append({
                    'id': unlocked_challenge.id,
                    'name': unlocked_challenge.name,
                    'category': unlocked_challenge.category
                })
                current_app.logger.info(
                    f"Challenge {unlocked_challenge.id} ({unlocked_challenge.name}) unlocked for "
                    f"{'team ' + str(team_id) if team_id else 'user ' + str(current_user.id)} "
                    f"via flag {matched_flag.id}"
                )
        
        # Scores are automatically calculated from Solve records
        # No need to update user.score or team.score (they don't exist as columns)
        
        try:
            db.session.commit()
        except Exception as commit_err:
            db.session.rollback()
            # Handle duplicate solve (race condition with concurrent submissions)
            err_msg = str(commit_err).lower()
            if 'unique' in err_msg or 'duplicate' in err_msg or 'integrity' in err_msg:
                return jsonify({'success': False, 'message': 'This challenge has already been solved'}), 400
            raise
        
        if challenge.docker_enabled:
            from services.container_manager import container_orchestrator

            stop_result = container_orchestrator.stop_container(
                challenge_id, current_user.id, force=True, on_solve=True,
                session_id=getattr(matched_flag, 'container_session_id', None),
            )
            if not stop_result['success']:
                current_app.logger.warning('Container cleanup after solve failed: %s', stop_result['error'])

        # Invalidate caches
        cache_service.invalidate_scoreboard()
        cache_service.invalidate_challenge(challenge_id)
        if team_id:
            cache_service.invalidate_team(team_id)
        else:
            cache_service.invalidate_user(current_user.id)
        
        # Emit WebSocket events for live updates
        solve_data = {
            'user': current_user.username,
            'team': team.name if team else None,
            'challenge': challenge.name,
            'category': challenge.category,
            'points': points,
            'timestamp': datetime.utcnow().isoformat()
        }
        WebSocketService.emit_new_solve(solve_data)
        
        # Update challenge points (may have changed)
        new_points = challenge.get_current_points()
        if new_points != points:
            WebSocketService.emit_challenge_update({
                'id': challenge.id,
                'name': challenge.name,
                'points': new_points
            })
        
        # Send updated scoreboard (check if teams are enabled)
        teams_enabled = Settings.get('teams_enabled', default=True, type='bool')
        cache_key = 'scoreboard_team' if teams_enabled else 'scoreboard_individual'
        scoreboard = ScoringService.get_scoreboard(team_based=teams_enabled, limit=100)
        cache_service.set(cache_key, scoreboard, ttl=60)
        WebSocketService.emit_scoreboard_update(scoreboard)
        
        message = 'Correct flag! Challenge solved!'
        if team:
            message += f' Points awarded to team "{team.name}".'
        
        # Add info about unlocked challenges
        response_data = {
            'success': True,
            'message': message,
            'points': points
        }
        
        # Add ACT unlock notification
        if unlocked_act:
            response_data['unlocked_act'] = unlocked_act
            response_data['message'] += f'{unlocked_act} has been unlocked!'
        
        if unlocked_challenges:
            unlocked_names = ', '.join([c['name'] for c in unlocked_challenges])
            response_data['unlocked_challenges'] = unlocked_challenges
            response_data['message'] += f' New challenge(s) unlocked: {unlocked_names}!'
        
        return jsonify(response_data)
    else:
        db.session.commit()
        
        # Calculate remaining attempts if limited
        attempts_remaining = None
        if challenge.max_attempts and challenge.max_attempts > 0:
            attempts_remaining = challenge.max_attempts - attempts.count()
        
        response = {
            'success': False,
            'message': 'Incorrect flag'
        }
        
        if attempts_remaining is not None:
            response['attempts_remaining'] = attempts_remaining
            if attempts_remaining > 0:
                response['message'] += f' ({attempts_remaining} attempts remaining)'
        
        return jsonify(response), 400


@challenges_bp.route('/<int:challenge_id>/explore', methods=['POST'])
@login_required
def explore_flag(challenge_id):
    """Submit an additional flag for an already-solved challenge to unlock more paths (no points awarded)"""
    from models.branching import ChallengeFlag, ChallengeUnlock
    from models.settings import Settings
    
    challenge = db.session.get(Challenge, challenge_id)
    if not is_challenge_visible(challenge, current_user):
        return jsonify(success=False, message='Challenge not found'), 404
    if current_user.is_admin and not Settings.is_ctf_running():
        return jsonify(success=False, message='The event is currently unavailable.'), 403

    team = current_user.get_team()
    team_id = team.id if team else None

    already_solved = challenge.solves.filter(participant_scope(Solve, current_user)).first() is not None

    if not already_solved:
        return jsonify({
            'success': False, 
            'message': 'You must solve this challenge first before exploring additional paths'
        }), 403
    
    # Check if this challenge has branching flags
    challenge_flags = ChallengeFlag.query.filter_by(challenge_id=challenge_id).all()
    has_branching = (
        any(flag.unlocks_challenge_id for flag in challenge_flags)
        if challenge.docker_enabled else len(challenge_flags) > 1
    )
    
    if not has_branching:
        return jsonify({
            'success': False, 
            'message': 'This challenge has no additional paths to explore'
        }), 400
    
    submitted_flag = request.form.get('flag', '').strip()
    
    if not submitted_flag:
        return jsonify({'success': False, 'message': 'Please enter a flag'}), 400
    
    # Rate limiting check (prevent brute force)
    rate_limit_key = f'explore:{current_user.id}:{challenge_id}'
    is_allowed, remaining = cache_service.check_rate_limit(rate_limit_key, limit=10, window=60)
    
    if not is_allowed:
        return jsonify({
            'success': False,
            'message': 'Too many attempts. Please wait before trying again.'
        }), 429
    
    # Check the flag
    if challenge.docker_enabled:
        matched_flag = challenge.check_docker_unlock_flag(submitted_flag, team_id, current_user.id)
    else:
        matched_flag = challenge.check_flag(submitted_flag, team_id=team_id, user_id=current_user.id)
    
    if not matched_flag:
        return jsonify({'success': False, 'message': 'Incorrect flag'}), 400
    
    # Check if this specific flag unlocks anything
    if not hasattr(matched_flag, 'unlocks_challenge_id') or not matched_flag.unlocks_challenge_id:
        return jsonify({
            'success': False, 
            'message': 'This flag does not unlock any additional paths'
        }), 400
    
    # Check if this path was already unlocked
    unlock_query = ChallengeUnlock.query.filter(
        ChallengeUnlock.unlocked_by_flag_id == matched_flag.id
    )
    
    unlock_query = unlock_query.filter(participant_scope(ChallengeUnlock, current_user))

    existing_unlock = unlock_query.first()
    
    if existing_unlock:
        return jsonify({
            'success': False, 
            'message': 'You have already unlocked this path'
        }), 400
    
    # Create unlock record (no points, no solve record - just unlocking).
    # The DB schema requires `user_id` to be non-null, so store the current
    # user as the actor while also setting team_id for team-scoped unlocks.
    unlock_record = ChallengeUnlock(
        user_id=current_user.id,
        team_id=team_id if team_id else None,
        challenge_id=matched_flag.unlocks_challenge_id,
        unlocked_by_flag_id=matched_flag.id
    )
    db.session.add(unlock_record)
    
    # Get the unlocked challenge details
    unlocked_challenge = Challenge.query.get(matched_flag.unlocks_challenge_id)
    
    db.session.commit()
    
    # Invalidate caches
    cache_service.invalidate_challenge(challenge_id)
    cache_service.invalidate_challenge(matched_flag.unlocks_challenge_id)
    
    response_data = {
        'success': True,
        'message': 'Correct flag! New path unlocked!',
        'unlocked_challenges': []
    }
    
    if is_challenge_visible(unlocked_challenge, current_user):
        response_data['unlocked_challenges'].append({
            'id': unlocked_challenge.id,
            'name': unlocked_challenge.name,
            'category': unlocked_challenge.category
        })
    
    return jsonify(response_data)


@challenges_bp.route('/solves/<int:challenge_id>')
@login_required
def challenge_solves(challenge_id):
    """Get list of teams/users who solved a challenge (Admin only)"""
    # Restrict to admin only - removed public access
    if not current_user.is_admin:
        return jsonify({'error': 'Admin access required'}), 403
    
    challenge = Challenge.query.get_or_404(challenge_id)
    
    solves = Solve.query.filter_by(challenge_id=challenge_id)\
        .order_by(Solve.solved_at.asc()).all()
    
    solve_list = []
    for solve in solves:
        if solve.team_id:
            team = solve.team
            solve_list.append({
                'name': team.name,
                'type': 'team',
                'solved_at': solve.solved_at.isoformat(),
                'points': solve.points_earned,
                'is_first_blood': solve.is_first_blood
            })
        else:
            user = solve.user
            solve_list.append({
                'name': user.username,
                'type': 'user',
                'solved_at': solve.solved_at.isoformat(),
                'points': solve.points_earned,
                'is_first_blood': solve.is_first_blood
            })
    
    return jsonify({
        'challenge': challenge.name,
        'solves': solve_list
    })
