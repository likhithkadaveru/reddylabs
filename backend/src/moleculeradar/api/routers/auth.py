"""Authentication endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from ...core.config import Settings, get_settings
from ...core.db import get_db
from ..schemas import LoginRequest, LoginResponse
from ..security import (
    DEV_PRINCIPAL,
    Principal,
    audit,
    authenticate_user,
    create_access_token,
    get_principal,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
def login(
    payload: LoginRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> LoginResponse:
    if not settings.auth_enabled:
        # Development convenience. Production boot refuses auth_enabled=false, so
        # this path cannot exist in a deployed environment.
        return LoginResponse(
            access_token="auth-disabled", role=DEV_PRINCIPAL.role,
            email=DEV_PRINCIPAL.email,
        )

    user = authenticate_user(db, payload.email, payload.password)
    if user is None:
        # One message for both unknown-user and wrong-password, so the endpoint
        # cannot be used to enumerate accounts.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )
    token = create_access_token(user, settings)
    audit(
        db, Principal(user_id=user.id, email=user.email, role=user.role),
        "auth:login", resource_type="user", resource_id=str(user.id), request=request,
    )
    return LoginResponse(access_token=token, role=user.role, email=user.email)


@router.get("/me", response_model=dict)
def me(principal: Principal = Depends(get_principal)) -> dict:
    return {
        "email": principal.email,
        "role": principal.role,
        "capabilities": sorted(principal.capabilities),
    }
