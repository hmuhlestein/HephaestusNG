"""Authentication configuration for Hephaestus."""

from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings


class AuthConfig(BaseSettings):
    """Authentication configuration settings."""

    # JWT Settings
    jwt_secret_key: str = Field(
        default="",
        description="Secret key for JWT token signing. MUST be set via AUTH_JWT_SECRET_KEY env var or hephaestus_config.yaml in production.",
    )
    jwt_algorithm: str = Field(
        default="HS256",
        description="Algorithm for JWT token signing. HS256 requires a strong secret.",
    )
    access_token_expire_minutes: int = Field(
        default=30, description="Access token expiration time in minutes"
    )
    refresh_token_expire_days: int = Field(
        default=7, description="Refresh token expiration time in days"
    )

    # Password Policy
    min_password_length: int = Field(default=8, description="Minimum password length")
    require_uppercase: bool = Field(
        default=True, description="Require at least one uppercase letter"
    )
    require_lowercase: bool = Field(
        default=True, description="Require at least one lowercase letter"
    )
    require_digit: bool = Field(default=True, description="Require at least one digit")
    require_special: bool = Field(
        default=True, description="Require at least one special character"
    )

    # Security Settings
    max_login_attempts: int = Field(
        default=5, description="Maximum login attempts before lockout"
    )
    lockout_duration_minutes: int = Field(
        default=30, description="Account lockout duration in minutes"
    )
    enable_email_verification: bool = Field(
        default=False, description="Require email verification for new accounts"
    )
    enable_two_factor: bool = Field(
        default=False, description="Enable two-factor authentication"
    )

    # Session Settings
    session_timeout_minutes: int = Field(
        default=1440,  # 24 hours
        description="Session timeout in minutes",
    )
    allow_multiple_sessions: bool = Field(
        default=True, description="Allow multiple concurrent sessions per user"
    )
    max_sessions_per_user: int = Field(
        default=5, description="Maximum concurrent sessions per user"
    )

    # Rate Limiting
    enable_rate_limiting: bool = Field(
        default=True, description="Enable rate limiting for auth endpoints"
    )
    login_rate_limit: str = Field(
        default="5/minute", description="Rate limit for login attempts"
    )
    register_rate_limit: str = Field(
        default="3/minute", description="Rate limit for registration attempts"
    )

    class Config:
        """Pydantic config."""

        env_file = ".env"
        env_prefix = "AUTH_"
        case_sensitive = False
        extra = "ignore"  # Ignore extra fields from environment


# Singleton instance
_auth_config: Optional[AuthConfig] = None


def get_auth_config() -> AuthConfig:
    """Get the auth configuration singleton."""
    global _auth_config
    if _auth_config is None:
        _auth_config = AuthConfig()
        # SECURITY: Warn if using default/empty JWT secret
        if not _auth_config.jwt_secret_key or _auth_config.jwt_secret_key == "":
            # Auto-generate a secure random key for development
            import secrets

            _auth_config.jwt_secret_key = secrets.token_urlsafe(64)
            import logging

            logger = logging.getLogger(__name__)
            logger.warning(
                "SECURITY: No AUTH_JWT_SECRET_KEY set. Auto-generated a random key. "
                "Tokens will NOT be valid across server restarts. "
                "Set AUTH_JWT_SECRET_KEY environment variable for production."
            )
    return _auth_config
