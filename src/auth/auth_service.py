"""Framework-agnostic authentication business logic.

Routes in auth_api.py open a DB session (via get_db_manager, which tests
patch) and translate the AuthError subclasses raised here into
HTTPException; this module never imports FastAPI or raises HTTP-layer
exceptions itself.
"""

import logging
import uuid
from datetime import timedelta
from typing import List, Optional

from sqlalchemy.orm import Session

from src.core.database import utc_now
from src.core.user_models import AuditLog, AuthToken, LoginAttempt, User, UserSession

from . import (
    create_token_pair,
    generate_secure_token,
    hash_password,
    hash_token,
    verify_password,
    verify_refresh_token,
)
from .auth_config import get_auth_config

logger = logging.getLogger(__name__)
config = get_auth_config()


class AuthError(Exception):
    """Base class for domain-level authentication errors, carrying the
    HTTP status code and detail payload the route should return."""

    status_code: int = 400
    headers: Optional[dict] = None

    def __init__(self, detail):
        self.detail = detail
        super().__init__(str(detail))


class WeakPasswordError(AuthError):
    status_code = 400

    def __init__(self, errors: List[str]):
        super().__init__({"message": "Password validation failed", "errors": errors})


class EmailAlreadyRegisteredError(AuthError):
    status_code = 400

    def __init__(self):
        super().__init__("Email already registered")


class UsernameAlreadyTakenError(AuthError):
    status_code = 400

    def __init__(self):
        super().__init__("Username already taken")


class AccountLockedError(AuthError):
    status_code = 429

    def __init__(self, lockout_duration_minutes: int):
        super().__init__(
            f"Account locked due to too many failed login attempts. "
            f"Try again in {lockout_duration_minutes} minutes."
        )


class InvalidCredentialsError(AuthError):
    status_code = 401
    headers = {"WWW-Authenticate": "Bearer"}

    def __init__(self):
        super().__init__("Invalid email or password")


class AccountNotActiveError(AuthError):
    status_code = 403

    def __init__(self, account_status: str):
        super().__init__(f"Account is {account_status}")


class InvalidRefreshTokenError(AuthError):
    status_code = 401


class InactiveUserError(AuthError):
    status_code = 401

    def __init__(self):
        super().__init__("User not found or inactive")


class AuthService:
    """Registration, login, and token-refresh business logic."""

    @staticmethod
    def validate_password(password: str) -> None:
        """Raise WeakPasswordError if password doesn't meet config's requirements."""
        errors = []

        if len(password) < config.min_password_length:
            errors.append(
                f"Password must be at least {config.min_password_length} characters"
            )

        if config.require_uppercase and not any(c.isupper() for c in password):
            errors.append("Password must contain at least one uppercase letter")

        if config.require_lowercase and not any(c.islower() for c in password):
            errors.append("Password must contain at least one lowercase letter")

        if config.require_digit and not any(c.isdigit() for c in password):
            errors.append("Password must contain at least one digit")

        if config.require_special and not any(
            c in "!@#$%^&*()_+-=[]{}|;:,.<>?" for c in password
        ):
            errors.append("Password must contain at least one special character")

        if errors:
            raise WeakPasswordError(errors)

    @staticmethod
    def _record_login_attempt(
        db: Session,
        email: str,
        ip_address: str,
        user_agent: str,
        success: bool,
        failure_reason: Optional[str] = None,
    ) -> None:
        """Record a login attempt for security auditing."""
        attempt = LoginAttempt(
            email=email,
            ip_address=ip_address,
            user_agent=user_agent,
            attempt_type="password",
            success=success,
            failure_reason=failure_reason,
        )
        db.add(attempt)
        db.commit()

    @staticmethod
    def _load_user_roles(db: Session, user: User) -> list:
        """Load the user's active role names for the JWT payload.

        Replaces the hardcoded `roles=[]` placeholder: a grant with a
        non-null past expires_at is not an active role, and a user with
        no (or only expired) grants gets [] -- the previous behavior, now
        derived from the database instead of assumed.
        """
        now = utc_now()
        return [
            grant.role.name
            for grant in user.roles
            if grant.role is not None
            and (grant.expires_at is None or grant.expires_at >= now)
        ]

    @staticmethod
    def _check_login_attempts(db: Session, email: str) -> bool:
        """Check if user has exceeded login attempt limit.

        Returns:
            True if login is allowed, False if account is locked
        """
        if not config.max_login_attempts:
            return True

        cutoff_time = utc_now() - timedelta(minutes=config.lockout_duration_minutes)
        recent_attempts = (
            db.query(LoginAttempt)
            .filter(
                LoginAttempt.email == email,
                not LoginAttempt.success,
                LoginAttempt.attempted_at >= cutoff_time,
            )
            .count()
        )

        return recent_attempts < config.max_login_attempts

    @staticmethod
    def _create_audit_log(
        db: Session,
        user_id: Optional[str],
        action: str,
        resource_type: str,
        resource_id: Optional[str] = None,
        status_result: str = "success",
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """Create an audit log entry."""
        audit = AuditLog(
            user_id=user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            status=status_result,
            ip_address=ip_address,
            user_agent=user_agent,
            error_message=error_message,
        )
        db.add(audit)
        db.commit()

    @staticmethod
    def register_user(db: Session, request) -> User:
        """Create a new user account.

        Args:
            db: Database session
            request: UserRegisterRequest (email, username, password, first_name, last_name)

        Returns:
            The newly created User

        Raises:
            EmailAlreadyRegisteredError: If the email is already registered
            UsernameAlreadyTakenError: If the username is already taken
        """
        existing_user = db.query(User).filter(User.email == request.email).first()
        if existing_user:
            raise EmailAlreadyRegisteredError()

        existing_user = db.query(User).filter(User.username == request.username).first()
        if existing_user:
            raise UsernameAlreadyTakenError()

        user = User(
            id=str(uuid.uuid4()),
            email=request.email,
            username=request.username,
            password_hash=hash_password(request.password),
            first_name=request.first_name,
            last_name=request.last_name,
            status="active",
            email_verified=not config.enable_email_verification,  # Auto-verify if verification disabled
        )

        db.add(user)
        db.commit()
        db.refresh(user)

        AuthService._create_audit_log(
            db=db,
            user_id=user.id,
            action="register",
            resource_type="user",
            resource_id=user.id,
            status_result="success",
        )

        logger.info(f"New user registered: {user.email}")

        return user

    @staticmethod
    def authenticate(
        db: Session,
        email: str,
        password: str,
        ip_address: str = "",
        user_agent: str = "",
    ) -> dict:
        """Verify credentials, mint tokens, and record a session for a successful login.

        Args:
            db: Database session
            email: Login email (passed as OAuth2PasswordRequestForm's username field)
            password: Plaintext password to verify
            ip_address: Caller's IP for the login-attempt/session/audit rows
            user_agent: Caller's user agent for the same rows

        Returns:
            Token dict from create_token_pair (access_token, refresh_token, token_type)

        Raises:
            AccountLockedError: If the account has too many recent failed attempts
            InvalidCredentialsError: If the email/password combination is wrong
            AccountNotActiveError: If the account status isn't "active"
        """
        if not AuthService._check_login_attempts(db, email):
            raise AccountLockedError(config.lockout_duration_minutes)

        user = db.query(User).filter(User.email == email).first()

        if not user or not verify_password(password, user.password_hash):
            AuthService._record_login_attempt(
                db=db,
                email=email,
                ip_address=ip_address,
                user_agent=user_agent,
                success=False,
                failure_reason="Invalid credentials",
            )
            raise InvalidCredentialsError()

        if user.status != "active":
            raise AccountNotActiveError(user.status)

        AuthService._record_login_attempt(
            db=db,
            email=email,
            ip_address=ip_address,
            user_agent=user_agent,
            success=True,
        )

        user.last_login_at = utc_now()
        db.commit()

        tokens = create_token_pair(
            user_id=user.id,
            email=user.email,
            roles=AuthService._load_user_roles(db, user),
        )

        refresh_token_record = AuthToken(
            id=str(uuid.uuid4()),
            user_id=user.id,
            token_hash=hash_token(tokens["refresh_token"]),
            token_type="refresh",
            expires_at=utc_now() + timedelta(days=config.refresh_token_expire_days),
        )
        db.add(refresh_token_record)

        session = UserSession(
            id=str(uuid.uuid4()),
            user_id=user.id,
            session_token_hash=generate_secure_token(),
            ip_address=ip_address,
            user_agent=user_agent,
            expires_at=utc_now() + timedelta(minutes=config.session_timeout_minutes),
        )
        db.add(session)

        AuthService._create_audit_log(
            db=db,
            user_id=user.id,
            action="login",
            resource_type="user",
            resource_id=user.id,
            status_result="success",
            ip_address=ip_address,
            user_agent=user_agent,
        )

        db.commit()

        logger.info(f"User logged in: {user.email}")

        return tokens

    @staticmethod
    def refresh_tokens(db: Session, refresh_token: str) -> dict:
        """Mint a new token pair from a valid, unexpired, unrevoked refresh token.

        Args:
            db: Database session
            refresh_token: The refresh token to exchange

        Returns:
            Token dict from create_token_pair (access_token, refresh_token, token_type)

        Raises:
            InvalidRefreshTokenError: If the token fails signature verification,
                isn't found, was revoked, or has expired
            InactiveUserError: If the token's user no longer exists or isn't active
        """
        payload = verify_refresh_token(refresh_token)
        if not payload:
            raise InvalidRefreshTokenError("Invalid refresh token")

        token_hash = hash_token(refresh_token)
        stored_token = (
            db.query(AuthToken)
            .filter(
                AuthToken.token_hash == token_hash, AuthToken.token_type == "refresh"
            )
            .first()
        )

        if not stored_token:
            raise InvalidRefreshTokenError("Refresh token not found")

        if stored_token.revoked_at:
            raise InvalidRefreshTokenError("Refresh token has been revoked")

        if stored_token.expires_at and stored_token.expires_at < utc_now():
            raise InvalidRefreshTokenError("Refresh token has expired")

        user = db.query(User).filter(User.id == payload["sub"]).first()
        if not user or user.status != "active":
            raise InactiveUserError()

        stored_token.last_used_at = utc_now()

        tokens = create_token_pair(
            user_id=user.id,
            email=user.email,
            roles=AuthService._load_user_roles(db, user),
        )

        # Optionally revoke old refresh token and store new one
        if not config.allow_multiple_sessions:
            stored_token.revoked_at = utc_now()

        new_refresh_token = AuthToken(
            id=str(uuid.uuid4()),
            user_id=user.id,
            token_hash=hash_token(tokens["refresh_token"]),
            token_type="refresh",
            expires_at=utc_now() + timedelta(days=config.refresh_token_expire_days),
        )
        db.add(new_refresh_token)

        db.commit()

        logger.info(f"Token refreshed for user: {user.email}")

        return tokens
