import json
import redis
from flask_caching import Cache
from contextlib import contextmanager
from decimal import Decimal
from uuid import uuid4

cache = Cache()

class DecimalEncoder(json.JSONEncoder):
    """Custom JSON encoder to handle Decimal objects"""
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super(DecimalEncoder, self).default(obj)

class CacheService:
    """Service for managing Redis cache"""
    
    def __init__(self, app=None):
        self.redis_client = None
        if app:
            self.init_app(app)
    
    def init_app(self, app):
        """Initialize cache with Flask app"""
        cache.init_app(app)
        
        # Initialize Redis client
        redis_url = app.config.get('REDIS_URL', 'redis://localhost:6379/0')
        self.redis_client = redis.from_url(redis_url, decode_responses=True)
    
    # Scoreboard caching
    def get_scoreboard(self):
        """Get cached scoreboard"""
        data = self.redis_client.get('scoreboard:team')
        return json.loads(data) if data else None
    
    def set_scoreboard(self, scoreboard_data, ttl=60):
        """Cache scoreboard data"""
        self.redis_client.setex(
            'scoreboard:team',
            ttl,
            json.dumps(scoreboard_data, cls=DecimalEncoder)
        )
    
    def invalidate_scoreboard(self):
        """Clear scoreboard cache"""
        self.redis_client.delete('scoreboard_full_team', 'scoreboard_full_individual')
        self.redis_client.delete('scoreboard:team')
        self.redis_client.delete('scoreboard:individual')
        self.redis_client.delete('scoreboard_team', 'scoreboard_individual')
    
    def invalidate_scoring(self, challenge_id=None):
        """Drop cached values affected by a scoring or participant change."""
        self.invalidate_scoreboard()
        self.invalidate_stats()
        keys = [key for pattern in ('team:*:score', 'user:*:score')
                for key in self.redis_client.scan_iter(match=pattern)]
        if keys:
            self.redis_client.delete(*keys)
        if challenge_id is not None:
            self.invalidate_challenge(challenge_id)

    def invalidate_stats(self):
        self.redis_client.delete('stats:platform')

    @contextmanager
    def solve_lock(self, challenge_id, team_id, user_id):
        owner = f'team:{team_id}' if team_id is not None else f'user:{user_id}'
        key = f'solve_lock:{challenge_id}:{owner}'
        token = uuid4().hex
        acquired = self.redis_client.set(key, token, nx=True, ex=10)
        try:
            yield acquired
        finally:
            if acquired:
                # Compare and delete atomically in case this lock expired and was replaced.
                with self.redis_client.pipeline() as pipe:
                    while True:
                        try:
                            pipe.watch(key)
                            if pipe.get(key) == token:
                                pipe.multi()
                                pipe.delete(key)
                                pipe.execute()
                            break
                        except redis.WatchError:
                            continue

    # Challenge caching
    def get_challenge(self, challenge_id):
        """Get cached challenge"""
        data = self.redis_client.get(f'challenge:{challenge_id}')
        return json.loads(data) if data else None
    
    def set_challenge(self, challenge_id, challenge_data, ttl=300):
        """Cache challenge data"""
        self.redis_client.setex(
            f'challenge:{challenge_id}',
            ttl,
            json.dumps(challenge_data, cls=DecimalEncoder)
        )
    
    def invalidate_challenge(self, challenge_id):
        """Clear challenge cache"""
        self.redis_client.delete(f'challenge:{challenge_id}')
    
    def invalidate_all_challenges(self):
        """Clear all challenge caches"""
        keys = self.redis_client.keys('challenge:*')
        if keys:
            self.redis_client.delete(*keys)
    
    # User/Team caching
    def get_user_score(self, user_id):
        """Get cached user score"""
        data = self.redis_client.get(f'user:{user_id}:score')
        return int(data) if data else None
    
    def set_user_score(self, user_id, score, ttl=300):
        """Cache user score"""
        self.redis_client.setex(f'user:{user_id}:score', ttl, score)
    
    def get_team_score(self, team_id):
        """Get cached team score"""
        data = self.redis_client.get(f'team:{team_id}:score')
        return int(data) if data else None
    
    def set_team_score(self, team_id, score, ttl=300):
        """Cache team score"""
        self.redis_client.setex(f'team:{team_id}:score', ttl, score)
    
    def invalidate_user(self, user_id):
        """Clear user cache"""
        self.redis_client.delete(f'user:{user_id}:score')
    
    def invalidate_team(self, team_id):
        """Clear team cache"""
        self.redis_client.delete(f'team:{team_id}:score')
        # Also invalidate all team members
        from models.user import User
        members = User.query.filter_by(team_id=team_id).all()
        for member in members:
            self.invalidate_user(member.id)
    
    # Stats caching
    def get_stats(self):
        """Get cached platform statistics"""
        data = self.redis_client.get('stats:platform')
        return json.loads(data) if data else None
    
    def set_stats(self, stats_data, ttl=300):
        """Cache platform statistics"""
        self.redis_client.setex(
            'stats:platform',
            ttl,
            json.dumps(stats_data, cls=DecimalEncoder)
        )
    
    # Rate limiting
    def check_rate_limit(self, key, limit=5, window=60):
        """
        Check if rate limit is exceeded
        
        Args:
            key: Unique identifier (e.g., f'submissions:{user_id}:{challenge_id}')
            limit: Maximum number of attempts
            window: Time window in seconds
        
        Returns:
            Tuple of (is_allowed, attempts_remaining)
        """
        pipe = self.redis_client.pipeline()
        pipe.incr(key)
        pipe.expire(key, window)
        result = pipe.execute()
        
        current_count = result[0]
        
        if current_count > limit:
            return False, 0
        
        return True, limit - current_count
    
    def reset_rate_limit(self, key):
        """Reset rate limit for a key"""
        self.redis_client.delete(key)
    
    # Generic cache operations
    def get(self, key):
        """Get value from cache"""
        data = self.redis_client.get(key)
        try:
            return json.loads(data) if data else None
        except:
            return data
    
    def set(self, key, value, ttl=300):
        """Set value in cache"""
        if isinstance(value, (dict, list)):
            value = json.dumps(value, cls=DecimalEncoder)
        self.redis_client.setex(key, ttl, value)
    
    def delete(self, key):
        """Delete key from cache"""
        self.redis_client.delete(key)
    
    def exists(self, key):
        """Check if key exists"""
        return self.redis_client.exists(key)
    
    def clear_all(self):
        """Clear all cache (use with caution)"""
        self.redis_client.flushdb()

# Global cache service instance
cache_service = CacheService()
