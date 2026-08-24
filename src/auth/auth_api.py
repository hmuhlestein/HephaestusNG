"""Authentication API endpoints for Hephaestus."""

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr, Field

from src.core.user_models import User

from .auth_config import get_auth_config
from .auth_db import get_db_manager
from .auth_middleware import CurrentUser
from .auth_middleware import get_current_user as _get_authenticated_user
from .auth_service import AuthError, AuthService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["authentication"])
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")
config = get_auth_config()


# Request/Response models
class UserRegisterRequest(BaseModel):
    """User registration request model."""

    email: EmailStr
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=8)
    first_name: Optional[str] = None
    last_name: Optional[str] = None


class UserLoginRequest(BaseModel):
    """User login request model."""

    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    """Token response model."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds


class RefreshTokenRequest(BaseModel):
    """Refresh token request model."""

    refresh_token: str


class UserResponse(BaseModel):
    """User response model."""

    id: str
    email: str
    username: str
    first_name: Optional[str]
    last_name: Optional[str]
    created_at: datetime
    email_verified: bool
    status: str


# API Endpoints
@router.post("/register", response_model=UserResponse)
async def register(request: UserRegisterRequest):
    """Register a new user account."""
    db_manager = get_db_manager()

    try:
        AuthService.validate_password(request.password)

        with db_manager.session_scope() as db:
            user = AuthService.register_user(db, request)
    except AuthError as e:
        raise HTTPException(
            status_code=e.status_code, detail=e.detail, headers=e.headers
        ) from e

    return UserResponse(
        id=user.id,
        email=user.email,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        created_at=user.created_at,
        email_verified=user.email_verified,
        status=user.status,
    )


@router.post("/login", response_model=TokenResponse)
async def login(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
):
    """Login with email and password."""
    db_manager = get_db_manager()

    # First hop of X-Forwarded-For when present (behind a proxy), else the
    # direct peer address -- this is what the login-attempt/session/audit
    # rows record as "from where".
    forwarded = request.headers.get("x-forwarded-for", "")
    client_ip = forwarded.split(",")[0].strip() or (
        request.client.host if request.client else ""
    )

    try:
        with db_manager.session_scope() as db:
            tokens = AuthService.authenticate(
                db,
                form_data.username,
                form_data.password,
                ip_address=client_ip,
                user_agent=request.headers.get("user-agent", ""),
            )
    except AuthError as e:
        # Failure-path log: the LoginAttempt row records this for the audit
        # DB, but a rejected login (bad password, locked account, inactive
        # user) previously left nothing in the log stream -- a brute-force
        # or credential-stuffing attempt was invisible to anyone tailing
        # logs.
        logger.warning(f"Login failed for {form_data.username!r} from {client_ip}: {e.detail}")
        raise HTTPException(
            status_code=e.status_code, detail=e.detail, headers=e.headers
        ) from e

    return TokenResponse(
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        token_type=tokens["token_type"],
        expires_in=config.access_token_expire_minutes * 60,
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(request: RefreshTokenRequest):
    """Refresh access token using refresh token."""
    db_manager = get_db_manager()

    try:
        with db_manager.session_scope() as db:
            tokens = AuthService.refresh_tokens(db, request.refresh_token)
    except AuthError as e:
        # Same reasoning as the login path: a rejected refresh (revoked or
        # expired token) is a possible token-reuse signal and previously
        # left no log-line trace at all.
        logger.warning(f"Token refresh rejected: {e.detail}")
        raise HTTPException(
            status_code=e.status_code, detail=e.detail, headers=e.headers
        ) from e

    return TokenResponse(
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        token_type=tokens["token_type"],
        expires_in=config.access_token_expire_minutes * 60,
    )


@router.post("/logout")
async def logout(token: str = Depends(oauth2_scheme)):
    """Logout and invalidate tokens."""
    # TODO: Implement token blacklisting or session termination
    return {"message": "Logged out successfully"}


@router.get("/me", response_model=UserResponse)
async def get_my_profile(current_user: CurrentUser = Depends(_get_authenticated_user)):
    """Get current user information.

    _get_authenticated_user (auth_middleware.get_current_user) already
    verifies the token and confirms the User row exists/is active --
    CurrentUser itself doesn't carry the profile fields UserResponse
    needs (first_name/last_name/created_at/email_verified/status), so
    this re-fetches the full row, same mapping as register()'s.
    """
    db_manager = get_db_manager()
    with db_manager.session_scope() as db:
        user = db.query(User).filter(User.id == current_user.id).first()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
            )

        return UserResponse(
            id=user.id,
            email=user.email,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            created_at=user.created_at,
            email_verified=user.email_verified,
            status=user.status,
        )
