import time
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ..secrets.models import Secret
from ..secrets.encryption import encryption_service
from ..secrets.models import AuditLog
from ..platform_settings.service import get_setting, set_setting
from .purposes import PURPOSES, PURPOSE_SETTING_PREFIX
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

        # Clear any purpose pins pointing at this provider — a dangling pin
        # degrades safely at resolution time but renders as a confusing blank
        # pin in the admin UI.
        for purpose in PURPOSES:
            pin = await get_setting(db, PURPOSE_SETTING_PREFIX + purpose)
            if isinstance(pin, dict) and pin.get("provider_id") == provider_id:
                await set_setting(db, PURPOSE_SETTING_PREFIX + purpose, None, commit=False)

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
            # Through the compat shim so a model that rejects a sampling param
            # still tests OK. Deliberately NOT metered in llm_usage: a 10-token
            # connectivity ping isn't app-attributable spend.
            from ..llm_compat import acompletion
            start = time.time()
            response = await acompletion(
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
        if not secret or not (secret.metadata_json or {}).get("is_active"):
            return None
        return self._config_from_secret(secret)

    async def get_default_provider_config(self, db: AsyncSession, purpose: str = "generation") -> dict | None:
        """Resolve the provider config for a purpose (see purposes.py for the chain)."""
        providers = await self._all_providers(db)
        config, _source, _secret = await self._resolve_purpose(db, purpose, providers)
        return config

    async def list_purpose_defaults(self, db: AsyncSession) -> list[dict]:
        """One row per catalog purpose: the stored pin (if any) plus the config
        that currently WOULD be used and where it came from — the admin UI shows
        both so 'unpinned' never reads as 'unconfigured'."""
        providers = await self._all_providers(db)
        rows = []
        for purpose, spec in PURPOSES.items():
            pin = await get_setting(db, PURPOSE_SETTING_PREFIX + purpose)
            pin = pin if isinstance(pin, dict) else {}
            # decrypt=False: display never needs the key, and decrypting here
            # would 500 this admin page exactly when the encryption key rotated
            # — the moment the admin needs it to re-enter keys.
            config, source, secret = await self._resolve_purpose(db, purpose, providers, decrypt=False)
            rows.append({
                "purpose": purpose,
                "label": spec["label"],
                "description": spec["description"],
                "provider_id": pin.get("provider_id"),
                "model": pin.get("model"),
                # Built from fields, never from `config` wholesale — config
                # carries the decrypted api_key, which must not reach the API.
                "effective": None if config is None else {
                    "provider_id": secret.id,
                    "provider_name": self._provider_name(secret),
                    "provider_type": config["provider_type"] or "",
                    "model": config["model"] or "",
                    "source": source,
                },
            })
        return rows

    async def set_purpose_default(
        self, db: AsyncSession, purpose: str, provider_id: str | None,
        model: str | None, user_id: str,
    ) -> None:
        """Pin (or clear, when provider_id is None) the provider for a purpose.

        Raises ValueError if the provider doesn't exist. An inactive provider
        may be pinned — resolution skips it until it's re-activated.
        """
        if provider_id:
            result = await db.execute(
                select(Secret).where(Secret.id == provider_id, Secret.category == "ai_provider")
            )
            secret = result.scalar_one_or_none()
            if not secret:
                raise ValueError("Provider not found")
            value = {"provider_id": provider_id, "model": (model or "").strip() or None}
            details = f"Pinned '{purpose}' to provider '{self._provider_name(secret)}'"
            if value["model"]:
                details += f" (model {value['model']})"
        else:
            value = None
            details = f"Cleared provider pin for '{purpose}'"

        # Single transaction: the pin must never land without its audit row.
        await set_setting(db, PURPOSE_SETTING_PREFIX + purpose, value, commit=False)
        db.add(AuditLog(
            user_id=user_id,
            action="ai_provider.purpose_default.set",
            resource_type="ai_provider_purpose",
            resource_id=purpose,
            details=details,
        ))
        await db.commit()

    async def _all_providers(self, db: AsyncSession) -> list[Secret]:
        result = await db.execute(select(Secret).where(Secret.category == "ai_provider"))
        return list(result.scalars().all())

    async def _resolve_purpose(
        self, db: AsyncSession, purpose: str, providers: list[Secret],
        decrypt: bool = True,
    ) -> tuple[dict | None, str | None, Secret | None]:
        """(config, source, provider) for a purpose. Source is one of
        pinned | legacy_default | inherited_generation | first_active.
        decrypt=False skips key decryption for display-only callers."""
        # 1. Explicit pin (skipped when the pinned provider is gone or inactive,
        #    so a deleted provider degrades instead of killing the purpose).
        pin = await get_setting(db, PURPOSE_SETTING_PREFIX + purpose)
        if isinstance(pin, dict) and pin.get("provider_id"):
            for p in providers:
                if p.id == pin["provider_id"] and (p.metadata_json or {}).get("is_active"):
                    return self._config_from_secret(
                        p, model_override=pin.get("model") or None, decrypt=decrypt,
                    ), "pinned", p

        # 2. Legacy default boolean (generation/toggle rows predate purpose pins)
        field = PURPOSES.get(purpose, {}).get("legacy_field")
        if field:
            for p in providers:
                meta = p.metadata_json or {}
                if meta.get(field) and meta.get("is_active"):
                    return self._config_from_secret(p, decrypt=decrypt), "legacy_default", p

        # 3. Everything that isn't generation inherits the generation default —
        #    only a REAL one (pin/boolean); otherwise fall through so the source
        #    label stays honest ("first_active", not "inherited").
        if purpose != "generation":
            config, source, p = await self._resolve_purpose(db, "generation", providers, decrypt=decrypt)
            if config is not None and source in ("pinned", "legacy_default"):
                return config, "inherited_generation", p

        # 4. Last resort: first active provider.
        for p in providers:
            if (p.metadata_json or {}).get("is_active"):
                return self._config_from_secret(p, decrypt=decrypt), "first_active", p

        return None, None, None

    def _config_from_secret(
        self, secret: Secret, model_override: str | None = None, decrypt: bool = True,
    ) -> dict:
        meta = secret.metadata_json or {}
        api_key = ""
        if decrypt and secret.encrypted_value:
            api_key = encryption_service.decrypt(secret.encrypted_value)
        return {
            "provider_type": meta.get("provider_type"),
            "model": model_override or meta.get("default_model"),
            "api_key": api_key,
            "base_url": meta.get("base_url") or None,
        }

    def _provider_name(self, secret: Secret) -> str:
        meta = secret.metadata_json or {}
        return secret.description.replace(f"{meta.get('provider_type', '').title()} provider: ", "")

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
            name=self._provider_name(secret),
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
