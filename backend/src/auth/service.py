import json
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from .models import User, RefreshToken
from .ad_client import ad_client
from .passwords import hash_password, verify_password
from ..config import settings


class AuthService:
    def create_access_token(self, user_id: str, role: str) -> str:
        expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_access_token_expire_minutes)
        payload = {
            "sub": user_id,
            "role": role,
            "exp": expire,
            "type": "access",
        }
        return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)

    def create_refresh_token_value(self) -> str:
        return secrets.token_urlsafe(48)

    def hash_token(self, token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def decode_access_token(self, token: str) -> dict | None:
        try:
            payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
            if payload.get("type") != "access":
                return None
            return payload
        except jwt.PyJWTError:
            return None

    async def authenticate(self, db: AsyncSession, username: str, password: str) -> tuple[User, str, str] | None:
        """Authenticate user and return (user, access_token, refresh_token) or None.

        Order:
          1. Configured external providers (LDAP/SAML/OIDC) via the provider chain.
          2. Local account (username+password) — authoritative for its username.
          3. Mock/demo credentials — DEV ONLY (settings.debug). Never in a
             packaged/production install (the installer sets DEBUG=false), so the
             old hardcoded admin/password does NOT ship as a working login.
        """
        from .providers.chain import authenticate_external

        user: User | None = None

        # 1. External providers (LDAP/SAML/OIDC). Returns a provisioned User, or
        #    None if none authenticated.
        try:
            user = await authenticate_external(db, username, password)
        except Exception:
            user = None  # never let a provider error block local fallback

        # 2. Local account. If a user with this username has a password set, that
        #    account is authoritative — verify it and do NOT fall through to the
        #    mock fallback (so dev mock creds can't shadow a real local admin).
        if user is None:
            existing = (await db.execute(
                select(User).where(User.username == username)
            )).scalar_one_or_none()
            if existing is not None and existing.password_hash:
                if not existing.is_active:
                    return None
                if verify_password(password, existing.password_hash):
                    user = existing
                else:
                    return None

        # 3. Mock/demo auth — DEV ONLY.
        if user is None and settings.debug:
            ad_user = ad_client.authenticate(username, password)
            if not ad_user:
                return None
            role = ad_client.get_user_role(ad_user)
            result = await db.execute(select(User).where(User.username == username))
            user = result.scalar_one_or_none()
            if not user:
                user = User(
                    username=ad_user.username,
                    display_name=ad_user.display_name,
                    email=ad_user.email,
                    role=role,
                    ad_groups=json.dumps(ad_user.groups),
                    auth_provider="mock",
                    external_id=ad_user.username,
                )
                db.add(user)
                await db.flush()
            else:
                user.display_name = ad_user.display_name
                user.email = ad_user.email
                user.role = role
                user.ad_groups = json.dumps(ad_user.groups)
                user.updated_at = datetime.now(timezone.utc)

        if user is None:
            return None

        # Create tokens
        access_token = self.create_access_token(user.id, user.role)
        refresh_value = self.create_refresh_token_value()

        # Store refresh token
        refresh_token = RefreshToken(
            user_id=user.id,
            token_hash=self.hash_token(refresh_value),
            expires_at=datetime.now(timezone.utc) + timedelta(days=settings.jwt_refresh_token_expire_days),
        )
        db.add(refresh_token)
        await db.commit()

        return user, access_token, refresh_value

    async def refresh_access_token(self, db: AsyncSession, refresh_value: str) -> tuple[str, str] | None:
        """Validate refresh token and return new (access_token, refresh_value) or None."""
        token_hash = self.hash_token(refresh_value)

        result = await db.execute(
            select(RefreshToken).where(
                RefreshToken.token_hash == token_hash,
                RefreshToken.is_revoked == False,
                RefreshToken.expires_at > datetime.now(timezone.utc),
            )
        )
        refresh_token = result.scalar_one_or_none()

        if not refresh_token:
            return None

        # Revoke old token (rotation)
        refresh_token.is_revoked = True

        # Get user
        result = await db.execute(select(User).where(User.id == refresh_token.user_id))
        user = result.scalar_one_or_none()
        if not user or not user.is_active:
            return None

        # Issue new tokens
        new_access = self.create_access_token(user.id, user.role)
        new_refresh = self.create_refresh_token_value()

        new_refresh_token = RefreshToken(
            user_id=user.id,
            token_hash=self.hash_token(new_refresh),
            expires_at=datetime.now(timezone.utc) + timedelta(days=settings.jwt_refresh_token_expire_days),
        )
        db.add(new_refresh_token)
        await db.commit()

        return new_access, new_refresh

    async def revoke_refresh_token(self, db: AsyncSession, refresh_value: str) -> None:
        token_hash = self.hash_token(refresh_value)
        result = await db.execute(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        )
        token = result.scalar_one_or_none()
        if token:
            token.is_revoked = True
            await db.commit()

    async def get_user_by_id(self, db: AsyncSession, user_id: str) -> User | None:
        result = await db.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()

    # --- Local account management -------------------------------------------

    async def admin_exists(self, db: AsyncSession) -> bool:
        """True if any admin account exists. Gates first-run admin creation."""
        from sqlalchemy import func
        n = (await db.execute(
            select(func.count(User.id)).where(User.role == "admin")
        )).scalar_one()
        return int(n or 0) > 0

    async def get_user_by_username(self, db: AsyncSession, username: str) -> User | None:
        return (await db.execute(
            select(User).where(User.username == username)
        )).scalar_one_or_none()

    async def create_local_user(
        self, db: AsyncSession, *, username: str, password: str, role: str,
        display_name: str | None = None,
    ) -> User:
        """Create a username+password (local) account. Caller commits."""
        user = User(
            username=username,
            display_name=(display_name or username),
            email="",
            role=role,
            ad_groups="[]",
            auth_provider="local",
            external_id=username,
            password_hash=hash_password(password),
        )
        db.add(user)
        await db.flush()
        return user

    async def set_password(self, db: AsyncSession, user: User, password: str) -> None:
        """Set/replace a user's local password. Caller commits."""
        user.password_hash = hash_password(password)
        user.updated_at = datetime.now(timezone.utc)

    async def issue_session(self, db: AsyncSession, user: User) -> tuple[str, str]:
        """Mint access + refresh tokens for a user, persist the refresh, commit.
        Returns (access_token, refresh_value)."""
        access_token = self.create_access_token(user.id, user.role)
        refresh_value = self.create_refresh_token_value()
        db.add(RefreshToken(
            user_id=user.id,
            token_hash=self.hash_token(refresh_value),
            expires_at=datetime.now(timezone.utc) + timedelta(days=settings.jwt_refresh_token_expire_days),
        ))
        await db.commit()
        return access_token, refresh_value


auth_service = AuthService()
