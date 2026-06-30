from datetime import datetime, timezone
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from .models import Secret, AuditLog
from .encryption import encryption_service
from .schemas import SecretCreate, SecretUpdate


class SecretsService:
    async def list_secrets(self, db: AsyncSession, category: str | None = None) -> list[Secret]:
        query = select(Secret).order_by(Secret.category, Secret.name)
        if category:
            query = query.where(Secret.category == category)
        result = await db.execute(query)
        return list(result.scalars().all())

    async def get_secret(self, db: AsyncSession, secret_id: str) -> Secret | None:
        result = await db.execute(select(Secret).where(Secret.id == secret_id))
        return result.scalar_one_or_none()

    async def get_secret_by_name(self, db: AsyncSession, name: str) -> Secret | None:
        result = await db.execute(select(Secret).where(Secret.name == name))
        return result.scalar_one_or_none()

    async def create_secret(self, db: AsyncSession, data: SecretCreate, user_id: str) -> Secret:
        encrypted_value = ""
        if data.value:
            encrypted_value = encryption_service.encrypt(data.value)

        secret = Secret(
            name=data.name,
            category=data.category,
            description=data.description,
            encrypted_value=encrypted_value,
            metadata_json=data.metadata_json,
        )
        db.add(secret)
        await db.flush()  # populate secret.id before referencing it in AuditLog

        # Audit log
        db.add(AuditLog(
            user_id=user_id,
            action="secret.create",
            resource_type="secret",
            resource_id=secret.id,
            details=f"Created secret '{data.name}' in category '{data.category}'",
        ))

        await db.commit()
        await db.refresh(secret)
        return secret

    async def update_secret(self, db: AsyncSession, secret_id: str, data: SecretUpdate, user_id: str) -> Secret | None:
        secret = await self.get_secret(db, secret_id)
        if not secret:
            return None

        if data.description is not None:
            secret.description = data.description
        if data.value is not None:
            secret.encrypted_value = encryption_service.encrypt(data.value) if data.value else ""
        if data.metadata_json is not None:
            secret.metadata_json = data.metadata_json
        secret.updated_at = datetime.now(timezone.utc)

        db.add(AuditLog(
            user_id=user_id,
            action="secret.update",
            resource_type="secret",
            resource_id=secret.id,
            details=f"Updated secret '{secret.name}'",
        ))

        await db.commit()
        await db.refresh(secret)
        return secret

    async def delete_secret(self, db: AsyncSession, secret_id: str, user_id: str) -> bool:
        secret = await self.get_secret(db, secret_id)
        if not secret:
            return False

        db.add(AuditLog(
            user_id=user_id,
            action="secret.delete",
            resource_type="secret",
            resource_id=secret.id,
            details=f"Deleted secret '{secret.name}'",
        ))

        await db.execute(delete(Secret).where(Secret.id == secret_id))
        await db.commit()
        return True

    async def decrypt_secret_value(self, db: AsyncSession, secret_id: str, user_id: str) -> str | None:
        secret = await self.get_secret(db, secret_id)
        if not secret or not secret.encrypted_value:
            return None

        db.add(AuditLog(
            user_id=user_id,
            action="secret.read",
            resource_type="secret",
            resource_id=secret.id,
            details=f"Read secret value '{secret.name}'",
        ))
        await db.commit()

        return encryption_service.decrypt(secret.encrypted_value)


secrets_service = SecretsService()
