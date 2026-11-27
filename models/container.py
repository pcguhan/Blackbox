from datetime import datetime, timezone
from models import db
from utils.flag_hmac import (
    dynamic_flag_prefix, flags_match, generate_hmac_flag, instance_flag_session,
    is_instance_session, parse_legacy_flag,
    verify_hmac_flag,
)

class ContainerInstance(db.Model):
    """Model for tracking active container instances"""
    __tablename__ = 'container_instances'
    
    ACTIVE_STATUSES = ('starting', 'running', 'stopping')

    id = db.Column(db.Integer, primary_key=True)
    challenge_id = db.Column(db.Integer, db.ForeignKey('challenges.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    team_id = db.Column(db.Integer, db.ForeignKey('teams.id'), nullable=True)
    
    # Docker container details
    container_id = db.Column(db.String(128), nullable=False, unique=True)
    container_name = db.Column(db.String(256), nullable=False)
    docker_image = db.Column(db.String(256), nullable=False)
    
    # Network details
    port = db.Column(db.Integer, nullable=False)
    host_ip = db.Column(db.String(256), nullable=True)  
    host_port = db.Column(db.Integer, nullable=True)  
    ip_address = db.Column(db.String(45), nullable=True) 
    docker_info = db.Column(db.JSON, nullable=True)  
    
    # State tracking
    status = db.Column(db.String(20), default='starting') 
    session_id = db.Column(db.String(64), nullable=False, unique=True)
    
    # Timestamps
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    started_at = db.Column(db.DateTime, nullable=True)
    expires_at = db.Column(db.DateTime, nullable=False)
    last_revert_time = db.Column(db.DateTime, nullable=True)
    
    # Error tracking
    error_message = db.Column(db.Text, nullable=True)
    
    # Dynamic flag storage 
    dynamic_flag = db.Column(db.String(512), nullable=True)
    
    # Relationships
    challenge = db.relationship('Challenge', backref='container_instances')
    user = db.relationship('User', backref='container_instances')
    team = db.relationship('Team', backref='container_instances')
    
    def __repr__(self):
        return f'<ContainerInstance {self.container_name} (user={self.user_id}, challenge={self.challenge_id})>'
    
    def to_dict(self):
        """Convert instance to dictionary"""
        connection_info = None
        if self.challenge and self.challenge.docker_connection_info:
            connection_info = self.challenge.docker_connection_info.replace(
                '{host}', self.host_ip or 'localhost'
            ).replace(
                '{port}', str(self.host_port) if self.host_port else ''
            )
        
        expires_at_ms = None
        if self.expires_at:
            expires_at_ms = int(self.expires_at.replace(tzinfo=timezone.utc).timestamp() * 1000)
        
        return {
            'id': self.id,
            'challenge_id': self.challenge_id,
            'user_id': self.user_id,
            'team_id': self.team_id,
            'container_id': self.container_id,
            'container_name': self.container_name,
            'docker_image': self.docker_image,
            'port': self.port,
            'host_ip': self.host_ip,
            'host_port': self.host_port,
            'ip_address': self.ip_address,
            'status': self.status,
            'session_id': self.session_id,
            'connection_info': connection_info,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
            'expires_at_ms': expires_at_ms, 
            'last_revert_time': self.last_revert_time.isoformat() if self.last_revert_time else None,
            'error_message': self.error_message
        }
    
    def is_expired(self):
        """Check if container has expired"""
        return datetime.utcnow() >= self.expires_at
    
    def is_active(self):
        """Check if container is in an active state"""
        return self.status in self.ACTIVE_STATUSES
    
    def get_remaining_time(self):
        """Get remaining time in human-readable format"""
        if not self.expires_at:
            return 'N/A'
        
        now = datetime.utcnow()
        if now >= self.expires_at:
            return 'Expired'
        
        delta = self.expires_at - now
        
        total_seconds = int(delta.total_seconds())
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60
        
        if hours > 0:
            return f"{hours}h {minutes}m"
        elif minutes > 0:
            return f"{minutes}m {seconds}s"
        else:
            return f"{seconds}s"
    
    @classmethod
    def from_signed_flag(cls, flag):
        session_id = instance_flag_session(flag)
        if not session_id:
            return None
        instance = cls.query.filter_by(session_id=session_id).one_or_none()
        if instance and verify_hmac_flag(
            flag, instance.challenge_id, instance.team_id, instance.user_id,
            dynamic_flag_prefix(instance.challenge), session_id=session_id,
            case_sensitive=instance.challenge.flag_case_sensitive,
        ):
            return instance
        return None

    @classmethod
    def find_flag_instance(cls, submitted, challenge, user_id, team_id):
        if not user_id:
            return None
        if instance_flag_session(submitted):
            instance = cls.from_signed_flag(submitted)
            if (instance and instance.challenge_id == challenge.id
                    and instance.user_id == user_id and instance.team_id == team_id
                    and instance.status == 'running' and not instance.is_expired()):
                return instance
            return None

        # Existing user-scoped flags can finish their original instance lifetime.
        active = cls.query.filter_by(
            challenge_id=challenge.id, user_id=user_id, team_id=team_id, status='running',
        ).filter(cls.expires_at > datetime.utcnow()).all()
        prefix = dynamic_flag_prefix(challenge)
        legacy = parse_legacy_flag(submitted)
        user_scoped = legacy and legacy['challenge_id'] == challenge.id and legacy['user_id'] == user_id
        for instance in active:
            stored = instance.dynamic_flag
            if is_instance_session(instance.session_id) or not stored:
                continue
            if flags_match(stored, submitted, challenge.flag_case_sensitive) and (
                user_scoped or verify_hmac_flag(
                    submitted, challenge.id, None, user_id, prefix,
                    case_sensitive=challenge.flag_case_sensitive,
                )
            ):
                return instance
        return None

    def get_expected_flag(self):
        """Return the issued flag for the admin diagnostic endpoint."""
        if is_instance_session(self.session_id):
            return generate_hmac_flag(
                self.challenge_id, self.team_id, self.user_id,
                dynamic_flag_prefix(self.challenge), session_id=self.session_id,
            )
        return self.dynamic_flag


class ContainerEvent(db.Model):
    """Model for logging container lifecycle events"""
    __tablename__ = 'container_events'
    
    id = db.Column(db.Integer, primary_key=True)
    container_instance_id = db.Column(db.Integer, db.ForeignKey('container_instances.id'), nullable=True)
    challenge_id = db.Column(db.Integer, db.ForeignKey('challenges.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    
    # Event details
    event_type = db.Column(db.String(50), nullable=False) 
    status = db.Column(db.String(20), nullable=False)  
    message = db.Column(db.Text, nullable=True)
    
    # Metadata
    ip_address = db.Column(db.String(45), nullable=True)
    container_id = db.Column(db.String(128), nullable=True)
    
    # Timestamp
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    
    # Relationships
    container_instance = db.relationship('ContainerInstance', backref='events')
    challenge = db.relationship('Challenge', backref='container_events')
    user = db.relationship('User', backref='container_events')
    
    def __repr__(self):
        return f'<ContainerEvent {self.event_type} (user={self.user_id}, challenge={self.challenge_id})>'
    
    def to_dict(self):
        """Convert event to dictionary"""
        return {
            'id': self.id,
            'container_instance_id': self.container_instance_id,
            'challenge_id': self.challenge_id,
            'user_id': self.user_id,
            'event_type': self.event_type,
            'status': self.status,
            'message': self.message,
            'ip_address': self.ip_address,
            'container_id': self.container_id,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None
        }
