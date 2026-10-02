"""Authentication, authorisation and audit.

Even though all ingested data is public, the system is built with real
authentication from the start: the opportunity set a company is looking at is
itself commercially sensitive, and retrofitting auth is how products end up with
holes.

RBAC-ready rather than RBAC-complete: roles are an enum with a capability map
here, not a table. When roles need per-resource permissions the map becomes a
table without changing call sites.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.config import Settings, get_settings
from ..core.db import get_db
from ..core.models import AuditLogEntry, User

# Argon2id rather than bcrypt. Two reasons: bcrypt silently truncates input at
# 72 bytes (so a long passphrase is weaker than it looks), and passlib's bcrypt
# backend is incompatible with current bcrypt releases. Argon2id has no length
# limit and is the current password-hashing recommendation.
password_hasher = PasswordHasher()
bearer_scheme = HTTPBearer(auto_error=False)

# Role capabilities. `admin` additionally sees the Data Health screen and the
# entity-resolution review queue, which expose operational detail an ordinary
# analyst does not need.
ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    "admin": frozenset({
        "read:opportunities", "read:molecules", "read:companies", "read:evidence",
        "read:whitespace", "write:investigation", "read:datahealth", "read:reviewqueue",
        "write:reviewqueue", "read:audit",
    }),
    "analyst": frozenset({
        "read:opportunities", "read:molecules", "read:companies", "read:evidence",
        "read:whitespace", "write:investigation",
    }),
    "viewer": frozenset({
        "read:opportunities", "read:molecules", "read:companies", "read:evidence",
        "read:whitespace",
    }),
}


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: uuid.UUID | None
    email: str
    role: str

    @property
    def capabilities(self) -> frozenset[str]:
        return ROLE_CAPABILITIES.get(self.role, frozenset())

    def can(self, capability: str) -> bool:
        return capability in self.capabilities


# The principal used when authentication is disabled for local development.
# Deliberately named so it is obvious in an audit log.
DEV_PRINCIPAL = Principal(user_id=None, email="dev-auth-disabled@localhost", role="admin")


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a password. Never raises on a bad input — returns False.

    A malformed stored hash must fail closed rather than propagate an exception
    that a caller might mistake for a server fault and retry.
    """
    try:
        return password_hasher.verify(hashed, plain)
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


def create_access_token(user: User, settings: Settings) -> str:
    if not settings.jwt_secret:
        raise RuntimeError("MR_JWT_SECRET is not configured; cannot issue tokens.")
    now = dt.datetime.now(dt.UTC)
    payload = {
        "sub": str(user.id),
        "email": user.email,
        "role": user.role,
        "iat": now,
        "exp": now + dt.timedelta(minutes=settings.jwt_expiry_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def get_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> Principal:
    """Resolve the caller.

    With `MR_AUTH_ENABLED=false` (development only) every request is an admin, and
    the application refuses to boot in production with that setting, so the
    convenience cannot escape a developer machine.
    """
    if not settings.auth_enabled:
        return DEV_PRINCIPAL

    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not settings.jwt_secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server authentication is not configured.",
        )
    try:
        payload: dict[str, Any] = jwt.decode(
            credentials.credentials, settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has expired.",
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token.",
        ) from exc

    user_id = payload.get("sub")
    user = db.get(User, uuid.UUID(user_id)) if user_id else None
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User no longer active.",
        )
    return Principal(user_id=user.id, email=user.email, role=user.role)


def require(capability: str):
    """Dependency factory enforcing a capability."""

    def _dependency(principal: Principal = Depends(get_principal)) -> Principal:
        if not principal.can(capability):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role {principal.role!r} lacks capability {capability!r}.",
            )
        return principal

    return _dependency


def audit(
    db: Session,
    principal: Principal,
    action: str,
    *,
    resource_type: str | None = None,
    resource_id: str | None = None,
    detail: dict[str, Any] | None = None,
    request: Request | None = None,
) -> None:
    """Record an auditable action.

    Reads of the opportunity feed are audited as well as writes: who looked at
    which opportunity is exactly the question that matters if a leak is ever
    investigated.
    """
    db.add(AuditLogEntry(
        actor=principal.email,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        detail=detail,
        ip_address=request.client.host if request and request.client else None,
    ))
    db.commit()


def authenticate_user(db: Session, email: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.email == email.lower().strip()))
    if user is None or not user.is_active:
        return None
    if not verify_password(password, user.password_hash):
        return None
    user.last_login_at = dt.datetime.now(dt.UTC)
    db.commit()
    return user
