"""OAuth 2.0 / OIDC authorization server routes.

Extracted from src/mcp/server.py (design_docs/phase_1c_server_decomposition.md).
"""

import logging
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

from fastapi import (
    APIRouter,
    Body,
    Header,
    HTTPException,
)
from fastapi.responses import HTMLResponse

from src.core.simple_config import get_config

# Import routers at module level for test compatibility

logger = logging.getLogger("src.mcp.server.oauth_routes")

router = APIRouter()

@router.get("/.well-known/oauth-authorization-server")
async def oauth_server_metadata():
    """OAuth server metadata with DCR support."""
    config = get_config()
    base_url = f"http://localhost:{config.server.mcp_port}"
    return {
        "issuer": base_url,
        "authorization_endpoint": f"{base_url}/oauth/authorize",
        "token_endpoint": f"{base_url}/oauth/token",
        "registration_endpoint": f"{base_url}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint": f"{base_url}/oauth/revoke",
        "scopes_supported": ["openid", "profile", "email"],
    }

@router.get("/.well-known/openid-configuration")
async def openid_config():
    """OpenID configuration - tells Claude no auth needed."""
    config = get_config()
    base_url = f"http://localhost:{config.server.mcp_port}"
    return {
        "issuer": base_url,
        "authorization_endpoint": f"{base_url}/authorize",
        "token_endpoint": f"{base_url}/token",
        "userinfo_endpoint": f"{base_url}/userinfo",
        "response_types_supported": ["none"],
        "grant_types_supported": ["none"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["none"],
    }



# OAuth authorization codes and client registrations (persisted in-memory with proper validation)
import base64
import hashlib
import threading

from src.core.database import utc_now

_auth_codes: Dict[str, Dict] = {}  # code -> {client_id, redirect_uri, scope, code_challenge, code_challenge_method, expires_at, used}

registered_clients: Dict[str, Dict] = {}  # client_id -> client details

_revoked_tokens: set = set()  # Set of hashed revoked tokens

_auth_lock = threading.Lock()  # Thread-safe lock for auth operations




def _validate_redirect_uri(uri: str) -> bool:
    """Validate redirect URI is safe (HTTPS or localhost)."""
    from urllib.parse import urlparse
    parsed = urlparse(uri)
    # Must be HTTPS (except localhost for development)
    if parsed.scheme not in ("https",) and parsed.hostname not in ("localhost", "127.0.0.1"):
        return False
    # No fragments allowed (OAuth 2.0 security)
    if parsed.fragment:
        return False
    # No wildcards
    if "*" in uri:
        return False
    return True

def _generate_code_challenge(code_verifier: str) -> str:
    """Generate PKCE code challenge from verifier (S256 method)."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

_rate_limit_lock = threading.Lock()

_rate_limit_store: Dict[str, List[float]] = defaultdict(list)

RATE_LIMIT_WINDOW = 60  # seconds

RATE_LIMIT_MAX = 30  # requests per window

def _check_rate_limit(key: str, max_requests: int = RATE_LIMIT_MAX) -> bool:
    """Check if request is within rate limit. Returns True if allowed.
    
    Thread-safe implementation using lock to prevent race conditions.
    """
    with _rate_limit_lock:
        now = time.time()
        # Clean old entries
        _rate_limit_store[key] = [t for t in _rate_limit_store[key] if now - t < RATE_LIMIT_WINDOW]
        if len(_rate_limit_store[key]) >= max_requests:
            return False
        _rate_limit_store[key].append(now)
        return True

@router.post("/oauth/register")
async def register_client(request: Dict[str, Any]):
    """Dynamic Client Registration endpoint (RFC 7591)."""
    import secrets

    # Rate limit registration to prevent spam/DoS
    if not _check_rate_limit("oauth_register", max_requests=5):
        raise HTTPException(status_code=429, detail="Rate limit exceeded for client registration")
    
    # Validate redirect URIs
    redirect_uris = request.get("redirect_uris", ["https://claude.ai/api/mcp/auth_callback"])
    if not isinstance(redirect_uris, list) or len(redirect_uris) == 0:
        raise HTTPException(status_code=400, detail="redirect_uris must be a non-empty array")
    
    for uri in redirect_uris:
        if not isinstance(uri, str) or not _validate_redirect_uri(uri):
            raise HTTPException(status_code=400, detail=f"Invalid redirect_uri: {uri}. Must be HTTPS (or localhost for dev).")
    
    # Validate client_name length to prevent abuse
    client_name = request.get("client_name", "Claude")
    if not isinstance(client_name, str) or len(client_name) > 255:
        raise HTTPException(status_code=400, detail="client_name must be a string of 255 characters or less")
    
    client_id = f"client_{secrets.token_urlsafe(16)}"
    client_secret = secrets.token_urlsafe(32)

    # Store client registration with thread safety
    with _auth_lock:
        registered_clients[client_id] = {
            "client_id": client_id,
            "client_secret": client_secret,
            "client_name": client_name,
            "redirect_uris": redirect_uris,
            "grant_types": request.get("grant_types", ["authorization_code"]),
            "response_types": request.get("response_types", ["code"]),
            "scope": request.get("scope", "openid profile email"),
            "token_endpoint_auth_method": request.get("token_endpoint_auth_method", "none"),
            "created_at": utc_now().isoformat(),
        }

    logger.info(f"Registered new OAuth client: {client_id}")

    # Return client registration response
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "client_id_issued_at": int(utc_now().timestamp()),
        "client_secret_expires_at": 0,  # Never expires
        "redirect_uris": redirect_uris,
        "grant_types": registered_clients[client_id]["grant_types"],
        "response_types": registered_clients[client_id]["response_types"],
        "client_name": client_name,
        "scope": registered_clients[client_id]["scope"],
        "token_endpoint_auth_method": registered_clients[client_id]["token_endpoint_auth_method"],
    }

@router.get("/oauth/authorize")
async def authorize_get(
    client_id: str,
    redirect_uri: str,
    response_type: str = "code",
    scope: str = "openid profile email",
    state: Optional[str] = None,
    code_challenge: Optional[str] = None,
    code_challenge_method: Optional[str] = None,
):
    """Authorization endpoint - validates client and stores auth code."""
    import secrets

    # Rate limit authorization requests
    if not _check_rate_limit("oauth_authorize", max_requests=10):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    
    # Validate client_id exists
    with _auth_lock:
        client = registered_clients.get(client_id)
    if not client:
        raise HTTPException(status_code=400, detail="invalid_client: Unknown client_id")
    
    # Validate redirect_uri is registered for this client
    if redirect_uri not in client.get("redirect_uris", []):
        raise HTTPException(status_code=400, detail="invalid_redirect_uri: URI not registered for this client")
    
    # Validate response_type
    if response_type != "code":
        raise HTTPException(status_code=400, detail="unsupported_response_type: Only 'code' is supported")
    
    # Generate authorization code
    auth_code = secrets.token_urlsafe(32)
    
    # Store auth code with metadata (single-use, expires in 10 minutes)
    with _auth_lock:
        _auth_codes[auth_code] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scope,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
            "expires_at": time.time() + 600,  # 10 minutes
            "used": False,
        }
    
    logger.info(f"Authorization code issued for client {client_id}")

    # Build redirect URL with code
    redirect_url = f"{redirect_uri}?code={auth_code}"
    if state:
        redirect_url += f"&state={state}"

    # Return HTML that auto-redirects (simulating user approval for local development)
    html_content = f"""
    <html>
    <head>
        <meta http-equiv="refresh" content="0; url={redirect_url}">
    </head>
    <body>
        <p>Authorizing... Redirecting to client...</p>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

@router.post("/oauth/authorize")
async def authorize_post(request: Dict[str, Any]):
    """Authorization endpoint POST - for form submissions."""
    return await authorize_get(
        client_id=request.get("client_id"),
        redirect_uri=request.get("redirect_uri"),
        response_type=request.get("response_type", "code"),
        scope=request.get("scope", "openid profile email"),
        state=request.get("state"),
        code_challenge=request.get("code_challenge"),
        code_challenge_method=request.get("code_challenge_method"),
    )

@router.post("/oauth/token")
async def token(request: Dict[str, Any] = Body(...)):
    """Token endpoint - validates authorization code and issues tokens."""
    import secrets

    grant_type = request.get("grant_type")
    
    # Rate limit token requests
    if not _check_rate_limit("oauth_token", max_requests=20):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    
    if grant_type == "authorization_code":
        code = request.get("code")
        client_id = request.get("client_id")
        redirect_uri = request.get("redirect_uri")
        code_verifier = request.get("code_verifier")
        
        if not code or not client_id:
            raise HTTPException(status_code=400, detail="code and client_id are required")
        
        # Validate client exists
        with _auth_lock:
            client = registered_clients.get(client_id)
        if not client:
            raise HTTPException(status_code=401, detail="invalid_client")
        
        # Validate and consume authorization code (single-use)
        with _auth_lock:
            stored_code = _auth_codes.get(code)
            if not stored_code:
                raise HTTPException(status_code=400, detail="invalid_grant: Unknown authorization code")
            
            if stored_code["used"]:
                # Code reuse detected - potential attack, invalidate all tokens for this code
                logger.warning(f"SECURITY: Authorization code reuse detected for client {client_id}")
                raise HTTPException(status_code=400, detail="invalid_grant: Authorization code already used")
            
            if stored_code["expires_at"] < time.time():
                del _auth_codes[code]
                raise HTTPException(status_code=400, detail="invalid_grant: Authorization code expired")
            
            if stored_code["client_id"] != client_id:
                raise HTTPException(status_code=400, detail="invalid_grant: Code was not issued to this client")
            
            if redirect_uri and stored_code["redirect_uri"] != redirect_uri:
                raise HTTPException(status_code=400, detail="invalid_grant: redirect_uri mismatch")
            
            # PKCE validation
            if stored_code.get("code_challenge"):
                if not code_verifier:
                    raise HTTPException(status_code=400, detail="invalid_grant: code_verifier required")
                
                # Verify code_verifier matches stored challenge (S256 method)
                if stored_code.get("code_challenge_method") == "S256":
                    expected_challenge = _generate_code_challenge(code_verifier)
                    if expected_challenge != stored_code["code_challenge"]:
                        logger.warning(f"SECURITY: PKCE verification failed for client {client_id}")
                        raise HTTPException(status_code=400, detail="invalid_grant: PKCE verification failed")
                elif stored_code.get("code_challenge_method") == "plain":
                    if code_verifier != stored_code["code_challenge"]:
                        raise HTTPException(status_code=400, detail="invalid_grant: PKCE verification failed")
            
            # Mark code as used (single-use enforcement)
            stored_code["used"] = True
            # Clean up used code after a short delay
            code_scope = stored_code["scope"]
        
        logger.info(f"Token issued for client {client_id}")
        
        # Issue tokens
        return {
            "access_token": f"access_{secrets.token_urlsafe(32)}",
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": f"refresh_{secrets.token_urlsafe(32)}",
            "scope": code_scope,
        }
    
    elif grant_type == "refresh_token":
        refresh_token = request.get("refresh_token")
        if not refresh_token:
            raise HTTPException(status_code=400, detail="refresh_token required")
        
        # Check if token has been revoked
        import hashlib
        token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()
        if token_hash in _revoked_tokens:
            raise HTTPException(status_code=400, detail="invalid_grant: Token has been revoked")
        
        return {
            "access_token": f"access_{secrets.token_urlsafe(32)}",
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": f"refresh_{secrets.token_urlsafe(32)}",
            "scope": request.get("scope", "openid profile email"),
        }
    
    else:
        raise HTTPException(status_code=400, detail=f"unsupported_grant_type: {grant_type}")

@router.post("/oauth/revoke")
async def revoke_token(request: Dict[str, Any]):
    """Token revocation endpoint (RFC 7009)."""
    import hashlib
    
    token = request.get("token")
    if not token:
        raise HTTPException(status_code=400, detail="token is required")
    
    # Store hash of revoked token (don't store raw tokens)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    
    with _auth_lock:
        _revoked_tokens.add(token_hash)
    
    logger.info(f"Token revoked (hash: {token_hash[:16]}...)")
    return {"revoked": True}

@router.get("/userinfo")
async def userinfo(authorization: Optional[str] = Header(None)):
    """Userinfo endpoint - extracts user info from token."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")
    
    token = authorization[7:]  # Remove 'Bearer ' prefix
    
    # Check if token has been revoked
    import hashlib
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    if token_hash in _revoked_tokens:
        raise HTTPException(status_code=401, detail="Token has been revoked")
    
    # For local development, return user info based on token
    # In production, this would decode the JWT and fetch real user data
    return {
        "sub": "local-user",
        "name": "Local User",
        "preferred_username": "local",
        "email": "user@localhost",
    }
