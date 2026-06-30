from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ..auth.models import User


class AdminService:
    async def list_users(self, db: AsyncSession) -> list[User]:
        result = await db.execute(select(User).order_by(User.username))
        return list(result.scalars().all())

    async def update_user_role(self, db: AsyncSession, user_id: str, role: str) -> User | None:
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if not user:
            return None
        user.role = role
        user.updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(user)
        return user

    async def toggle_user_active(self, db: AsyncSession, user_id: str) -> User | None:
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if not user:
            return None
        user.is_active = not user.is_active
        user.updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(user)
        return user


admin_service = AdminService()
