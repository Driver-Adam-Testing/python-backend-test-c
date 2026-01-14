import json
import time
from urllib.request import urlopen

import jwt
from app.auth.models import M2M, User
from app.core.config import settings
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWTError
from jwt.algorithms import RSAAlgorithm
from shared.utils.decorators import expiring_cache

_jwt_scheme = HTTPBearer(auto_error=False)

ALGORITHMS = ["RS256"]


@expiring_cache(3600)
def get_jwks() -> dict:
    """Fetch Auth0 JWKS (memoised for 1 hour)."""
    jwks_url = f"https://{settings.AUTH0_DOMAIN}/.well-known/jwks.json"
    last_exc = None
    for attempt in range(3):
        try:
            return json.loads(urlopen(jwks_url).read())
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(1)
    raise last_exc


def _get_rsa_key(jwks: dict, kid: str) -> dict:
    """Return the JWK that matches *kid* (or {})."""
    return next((k for k in jwks["keys"] if k["kid"] == kid), {})


def verify_jwt(token: str) -> dict:
    """
    Verify an Auth0 RS256 JWT and return its payload.

    Raises
    ------
    HTTPException(401)
        If the token is invalid or cannot be verified.
    """

    header = jwt.get_unverified_header(token)
    rsa_key = _get_rsa_key(get_jwks(), header["kid"])
    if not rsa_key:
        raise HTTPException(401, "Unable to find appropriate key")

    try:
        public_key = RSAAlgorithm.from_jwk(json.dumps(rsa_key))
        return jwt.decode(
            token,
            public_key,
            algorithms=ALGORITHMS,
            audience=settings.AUTH0_AUDIENCE,
            issuer=f"https://{settings.AUTH0_DOMAIN}/",
        )
    except PyJWTError:
        raise HTTPException(401, "Unauthorized")


def require_jwt(
    creds: HTTPAuthorizationCredentials | None = Depends(_jwt_scheme),
) -> dict:
    """Dependency: assert request carries a valid Bearer token."""
    if not creds or not creds.credentials:
        raise HTTPException(401, "Missing Bearer token")
    return User(**verify_jwt(creds.credentials))


def require_m2m_jwt(
    creds: HTTPAuthorizationCredentials | None = Depends(_jwt_scheme),
) -> dict:
    """Dependency: assert request carries a valid Bearer token."""
    if not creds or not creds.credentials:
        raise HTTPException(401, "Missing Bearer token")
    return M2M(**verify_jwt(creds.credentials))
