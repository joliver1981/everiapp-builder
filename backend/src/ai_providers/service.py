import time
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ..secrets.models import Secret
from ..secrets.encryption import encryption_service
from ..secrets.models import AuditLog
from .schemas import AIProviderCreate, AIProviderUpdate, AIProviderResponse, AIProviderTestResult


class AIProviderService:
    """Manages AI providers stored as secrets with category='ai_provider'."""

    async def list_providers(self, db: AsyncSession) -> list[AIProviderResponse]:
        result = await db.execute(
            select(Secret).where(Secret.category == "ai_provider").order_by(Secret.name)
        )
        providers = result.scalars().all()
        return [self._to_response(p) for p in providers]

    async def get_provider(self, db: AsyncSession, provider_id: str) -> AIProviderResponse | None:
        result = await db.execute(select(Secret).where(Secret.id == provider_id, Secret.category == "ai_provider"))
        provider = result.scalar_one_or_none()
        return self._to_response(provider) if provider else None

    async def create_provider(self, db: AsyncSession, data: AIProviderCreate, user_id: str) -> AIProviderResponse:
        # If setting as default, unset other defaults
        if data.is_default_generation:
            await self._unset_defaults(db, "is_default_generation")
        if data.is_default_toggle:
            await self._unset_defaults(db, "is_default_toggle")

        encrypted_key = encryption_service.encrypt(data.api_key) if data.api_key else ""

        metadata = {
            "provider_type": data.provider_type,
            "default_model": data.default_model,
            "base_url": data.base_url,
            "is_active": True,
            "is_default_generation": data.is_default_generation,
            "is_default_toggle": data.is_default_toggle,
            "extra_config": data.extra_config,
            "last_verified": None,
        }

        secret = Secret(
            name=f"ai_provider_{data.name.lower().replace(' ', '_')}",
            category="ai_provider",
            description=f"{data.provider_type.title()} provider: {data.name}",
            encrypted_value=encrypted_key,
            metadata_json=metadata,
        )
        db.add(secret)
        await db.flush()  # generate secret.id before referencing it

        db.add(AuditLog(
            user_id=user_id,
            action="ai_provider.create",
            resource_type="ai_provider",
            resource_id=secret.id,
            details=f"Created AI provider '{data.name}' ({data.provider_type})",
        ))

        await db.commit()
        await db.refresh(secret)
        return self._to_response(secret)

    async def update_provider(self, db: AsyncSession, provider_id: str, data: AIProviderUpdate, user_id: str) -> AIProviderResponse | None:
        result = await db.execute(select(Secret).where(Secret.id == provider_id, Secret.category == "ai_provider"))
        secret = result.scalar_one_or_none()
        if not secret:
            return None

        meta = dict(secret.metadata_json or {})

        if data.name is not None:
            secret.description = f"{meta.get('provider_type', '').title()} provider: {data.name}"
        if data.api_key is not None:
            secret.encrypted_value = encryption_service.encrypt(data.api_key) if data.api_key else ""
        if data.base_url is not None:
            meta["base_url"] = data.base_url
        if data.default_model is not None:
            meta["default_model"] = data.default_model
        if data.is_active is not None:
            meta["is_active"] = data.is_active
        if data.is_default_generation is not None:
            if data.is_default_generation:
                await self._unset_defaults(db, "is_default_generation")
            meta["is_default_generation"] = data.is_default_generation
        if data.is_default_toggle is not None:
            if data.is_default_toggle:
                await self._unset_defaults(db, "is_default_toggle")
            meta["is_default_toggle"] = data.is_default_toggle
        if data.extra_config is not None:
            meta["extra_config"] = data.extra_config

        secret.metadata_json = meta
        secret.updated_at = datetime.now(timezone.utc)

        db.add(AuditLog(
            user_id=user_id,
            action="ai_provider.update",
            resource_type="ai_provider",
            resource_id=secret.id,
            details=f"Updated AI provider '{secret.name}'",
        ))

        await db.commit()
        await db.refresh(secret)
        return self._to_response(secret)

    async def delete_provider(self, db: AsyncSession, provider_id: str, user_id: str) -> bool:
        result = await db.execute(select(Secret).where(Secret.id == provider_id, Secret.category == "ai_provider"))
        secret = result.scalar_one_or_none()
        if not secret:
            return False

        db.add(AuditLog(
            user_id=user_id,
            action="ai_provider.delete",
            resource_type="ai_provider",
            resource_id=secret.id,
            details=f"Deleted AI provider '{secret.name}'",
        ))

        await db.delete(secret)
        await db.commit()
        return True

    async def test_provider(self, db: AsyncSession, provider_id: str, user_id: str) -> AIProviderTestResult:
        result = await db.execute(select(Secret).where(Secret.id == provider_id, Secret.category == "ai_provider"))
        secret = result.scalar_one_or_none()
        if not secret:
            return AIProviderTestResult(success=False, message="Provider not found")

        meta = secret.metadata_json or {}
        provider_type = meta.get("provider_type", "")
        model = meta.get("default_model", "")
        api_key = encryption_service.decrypt(secret.encrypted_value) if secret.encrypted_value else ""

        if not api_key:
            return AIProviderTestResult(success=False, message="No API key configured")

        try:
            import litellm
            start = time.time()
            response = await litellm.acompletion(
                model=f"{provider_type}/{model}" if provider_type != "openai" else model,
                messages=[{"role": "user", "content": "Say hello in exactly one word."}],
                api_key=api_key,
                base_url=meta.get("base_url") or None,
                max_tokens=10,
            )
            elapsed = int((time.time() - start) * 1000)

            # Update last_verified
            meta["last_verified"] = datetime.now(timezone.utc).isoformat()
            secret.metadata_json = meta
            await db.commit()

            return AIProviderTestResult(
                success=True,
                message=f"Connection successful: {response.choices[0].message.content}",
                model=model,
                response_time_ms=elapsed,
            )
        except Exception as e:
            return AIProviderTestResult(success=False, message=str(e))

    async def get_provider_config(self, db: AsyncSession, provider_id: str) -> dict | None:
        """Get a specific provider's config for litellm by provider ID."""
        result = await db.execute(
            select(Secret).where(Secret.id == provider_id, Secret.category == "ai_provider")
        )
        secret = result.scalar_one_or_none()
        if not secret:
            return None

        meta = secret.metadata_json or {}
        if not meta.get("is_active"):
            return None

        api_key = encryption_service.decrypt(secret.encrypted_value) if secret.encrypted_value else ""
        return {
            "provider_type": meta.get("provider_type"),
            "model": meta.get("default_model"),
            "api_key": api_key,
            "base_url": meta.get("base_url") or None,
        }

    async def get_default_provider_config(self, db: AsyncSession, purpose: str = "generation") -> dict | None:
        """Get the default provider config for litellm. purpose = 'generation' or 'toggle'."""
        field = "is_default_generation" if purpose == "generation" else "is_default_toggle"

        result = await db.execute(
            select(Secret).where(Secret.category == "ai_provider")
        )
        providers = result.scalars().all()

        for p in providers:
            meta = p.metadata_json or {}
            if meta.get(field) and meta.get("is_active"):
                api_key = encryption_service.decrypt(p.encrypted_value) if p.encrypted_value else ""
                return {
                    "provider_type": meta.get("provider_type"),
                    "model": meta.get("default_model"),
                    "api_key": api_key,
                    "base_url": meta.get("base_url") or None,
                }

        # Fallback: first active provider
        for p in providers:
            meta = p.metadata_json or {}
            if meta.get("is_active"):
                api_key = encryption_service.decrypt(p.encrypted_value) if p.encrypted_value else ""
                return {
                    "provider_type": meta.get("provider_type"),
                    "model": meta.get("default_model"),
                    "api_key": api_key,
                    "base_url": meta.get("base_url") or None,
                }

        return None

    async def _unset_defaults(self, db: AsyncSession, field: str) -> None:
        result = await db.execute(
            select(Secret).where(Secret.category == "ai_provider")
        )
        for p in result.scalars().all():
            meta = dict(p.metadata_json or {})
            if meta.get(field):
                meta[field] = False
                p.metadata_json = meta

    def _to_response(self, secret: Secret) -> AIProviderResponse:
        meta = secret.metadata_json or {}
        return AIProviderResponse(
            id=secret.id,
            name=secret.description.replace(f"{meta.get('provider_type', '').title()} provider: ", ""),
            provider_type=meta.get("provider_type", ""),
            is_active=meta.get("is_active", True),
            is_default_generation=meta.get("is_default_generation", False),
            is_default_toggle=meta.get("is_default_toggle", False),
            default_model=meta.get("default_model", ""),
            base_url=meta.get("base_url", ""),
            extra_config=meta.get("extra_config", {}),
            last_verified=meta.get("last_verified"),
            created_at=secret.created_at.isoformat(),
            updated_at=secret.updated_at.isoformat(),
        )


ai_provider_service = AIProviderService()
