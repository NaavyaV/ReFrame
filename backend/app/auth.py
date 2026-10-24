import time
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field

from .config import get_settings
from .ratelimit import SlidingWindow
from .stdb import SpacetimeClient, SpacetimeError

router = APIRouter(prefix="/auth", tags=["auth"])
_hasher = PasswordHasher()
_bearer = HTTPBearer(auto_error=False)
_auth_window: SlidingWindow | None = None
# Verified users, so each authenticated request doesn't need a SpacetimeDB lookup.
_USER_CACHE_TTL = 60.0
_user_cache: dict[str, tuple[float, "PublicUser"]] = {}


class RegisterBody(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=256)
    display_name: str = Field(min_length=1, max_length=80)


class LoginBody(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class PublicUser(BaseModel):
    user_id: str
    email: str
    display_name: str


class AuthResponse(BaseModel):
    token: str
    user: PublicUser


def _stdb(request: Request) -> SpacetimeClient:
    return request.app.state.stdb


def limit_auth(request: Request) -> None:
    global _auth_window
    if _auth_window is None:
        _auth_window = SlidingWindow(get_settings().auth_per_minute, 60)
    _auth_window.hit(request.client.host if request.client else "unknown")


def _issue_token(user_id: str) -> str:
    s = get_settings()
    now = datetime.now(timezone.utc)
    payload = {"sub": user_id, "iat": now, "exp": now + timedelta(hours=s.jwt_ttl_hours)}
    return jwt.encode(payload, s.jwt_secret, algorithm="HS256")


def _public(row: dict) -> PublicUser:
    return PublicUser(user_id=row["user_id"], email=row["email"], display_name=row["display_name"])


async def current_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> PublicUser:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")
    try:
        payload = jwt.decode(creds.credentials, get_settings().jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired")
    user_id = payload["sub"]
    cached = _user_cache.get(user_id)
    if cached and time.monotonic() - cached[0] < _USER_CACHE_TTL:
        return cached[1]
    row = await _stdb(request).find_user_by_id(user_id)
    if row is None:
        _user_cache.pop(user_id, None)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account not found")
    user = _public(row)
    if len(_user_cache) > 10_000:
        _user_cache.clear()
    _user_cache[user_id] = (time.monotonic(), user)
    return user


@router.post("/register", response_model=AuthResponse, dependencies=[Depends(limit_auth)])
async def register(body: RegisterBody, request: Request) -> AuthResponse:
    db = _stdb(request)
    email = body.email.lower()
    if await db.find_user_by_email(email):
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    user_id = str(uuid.uuid4())
    try:
        await db.call("create_user", user_id, email, body.display_name.strip(), _hasher.hash(body.password))
    except SpacetimeError as e:
        if "email_taken" in str(e):
            raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Database error")
    return AuthResponse(
        token=_issue_token(user_id),
        user=PublicUser(user_id=user_id, email=email, display_name=body.display_name.strip()),
    )


@router.post("/login", response_model=AuthResponse, dependencies=[Depends(limit_auth)])
async def login(body: LoginBody, request: Request) -> AuthResponse:
    db = _stdb(request)
    row = await db.find_user_by_email(body.email.lower())
    if row is None:
        _hasher.hash(body.password)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong email or password")
    try:
        _hasher.verify(row["password_hash"], body.password)
    except (VerificationError, InvalidHashError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong email or password")
    if _hasher.check_needs_rehash(row["password_hash"]):
        await db.call("update_password_hash", row["user_id"], _hasher.hash(body.password))
    return AuthResponse(token=_issue_token(row["user_id"]), user=_public(row))


@router.get("/me", response_model=PublicUser)
async def me(user: PublicUser = Depends(current_user)) -> PublicUser:
    return user
