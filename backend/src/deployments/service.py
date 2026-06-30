import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..apps.models import App, AppVersion
from ..config import settings
from ..database import async_session
from ..secrets.encryption import encryption_service
from ..secrets.models import AuditLog, Secret
from . import builder
from .deployers import get_deployer
from .deployers.base import HealthResult, TargetInfo
from .models import ACTIVE_DEPLOYMENT_STATUSES, Deployment, DeploymentTarget

logger = logging.getLogger(__name__)


# Which Secret category a target kind expects in its credential slot.
_REQUIRED_CREDENTIAL_CATEGORY = {
    "agent": "agent_token",
    "ssh": "ssh_private_key",
}


def _require_credential(kind: str, credential_secret_id: str | None) -> None:
    """Raise ValueError if `kind` needs a credential and none is linked.

    Both `agent` and `ssh` kinds talk to a remote daemon that requires auth — there's
    no useful "no-credential" mode for either. Catching it at create/update time
    means the user sees the problem in the form, not in a confusing Test error later.
    """
    if kind in _REQUIRED_CREDENTIAL_CATEGORY and not credential_secret_id:
        category = _REQUIRED_CREDENTIAL_CATEGORY[kind]
        raise ValueError(
            f"This {kind} target needs a credential. "
            f"Create a Secret with category '{category}' "
            f"in Admin → Secrets, then pick it in the Credential dropdown."
        )


class DeploymentsService:
    # ---------- Targets ----------

    async def list_targets(self, db: AsyncSession) -> list[DeploymentTarget]:
        result = await db.execute(select(DeploymentTarget).order_by(DeploymentTarget.name))
        return list(result.scalars().all())

    async def get_target(self, db: AsyncSession, target_id: str) -> DeploymentTarget | None:
        return (await db.execute(select(DeploymentTarget).where(DeploymentTarget.id == target_id))).scalar_one_or_none()

    async def create_target(self, db: AsyncSession, data, user_id: str) -> DeploymentTarget:
        if data.port_range_end < data.port_range_start:
            raise ValueError("port_range_end must be >= port_range_start")
        _require_credential(data.kind, data.credential_secret_id)
        target = DeploymentTarget(
            name=data.name, kind=data.kind, host=data.host, port=data.port,
            ssh_user=data.ssh_user,
            port_range_start=data.port_range_start,
            port_range_end=data.port_range_end,
            environment=data.environment,
            credential_secret_id=data.credential_secret_id,
            extra_config=data.extra_config or {},
            is_active=data.is_active,
        )
        db.add(target)
        await db.flush()  # populate target.id (default=uuid runs at flush, not __init__)
        db.add(AuditLog(
            user_id=user_id, action="deployment_target.create",
            resource_type="deployment_target", resource_id=target.id,
            details=f"Created target '{data.name}' (kind={data.kind}, host={data.host})",
        ))
        await db.commit()
        await db.refresh(target)
        return target

    async def update_target(self, db: AsyncSession, target_id: str, data, user_id: str) -> DeploymentTarget | None:
        target = await self.get_target(db, target_id)
        if not target:
            return None
        for field in ("name", "host", "port", "ssh_user", "port_range_start",
                      "port_range_end", "environment", "credential_secret_id",
                      "extra_config", "is_active"):
            value = getattr(data, field, None)
            if value is not None:
                setattr(target, field, value)
        # Validate the post-update state — kind may not have changed but
        # the credential field could have been cleared.
        _require_credential(target.kind, target.credential_secret_id)
        target.updated_at = datetime.now(timezone.utc)
        db.add(AuditLog(
            user_id=user_id, action="deployment_target.update",
            resource_type="deployment_target", resource_id=target.id,
            details=f"Updated target '{target.name}'",
        ))
        await db.commit()
        await db.refresh(target)
        return target

    async def delete_target(self, db: AsyncSession, target_id: str, user_id: str) -> bool:
        target = await self.get_target(db, target_id)
        if not target:
            return False
        active = await db.execute(
            select(Deployment).where(
                Deployment.target_id == target_id,
                Deployment.status.in_(ACTIVE_DEPLOYMENT_STATUSES),
            )
        )
        if active.scalars().first():
            raise ValueError("Cannot delete target with active deployments — stop them first")
        db.add(AuditLog(
            user_id=user_id, action="deployment_target.delete",
            resource_type="deployment_target", resource_id=target.id,
            details=f"Deleted target '{target.name}'",
        ))
        await db.delete(target)
        await db.commit()
        return True

    async def test_target(self, db: AsyncSession, target_id: str) -> TargetInfo:
        target = await self.get_target(db, target_id)
        if not target:
            return TargetInfo(ok=False, detail="Target not found")
        credential = await self._resolve_credential(db, target)
        deployer = get_deployer(target, credential)
        info = await deployer.test_connection()
        target.last_seen_at = datetime.now(timezone.utc)
        target.last_seen_status = "ok" if info.ok else "error"
        target.agent_version = info.agent_version or target.agent_version
        await db.commit()
        return info

    # ---------- Deployments ----------

    async def list_deployments(self, db: AsyncSession, app_id: str) -> list[Deployment]:
        result = await db.execute(
            select(Deployment)
            .where(Deployment.app_id == app_id)
            .order_by(Deployment.started_at.desc())
        )
        return list(result.scalars().all())

    async def get_deployment(self, db: AsyncSession, deployment_id: str) -> Deployment | None:
        return (
            await db.execute(select(Deployment).where(Deployment.id == deployment_id))
        ).scalar_one_or_none()

    async def deploy(self, db: AsyncSession, app_id: str, version: int, target_id: str, user_id: str) -> Deployment:
        # Verify the version exists on disk
        result = await db.execute(
            select(AppVersion).where(AppVersion.app_id == app_id, AppVersion.version == version)
        )
        if not result.scalar_one_or_none():
            raise ValueError(f"App {app_id} has no version v{version}")

        target = await self.get_target(db, target_id)
        if not target:
            raise ValueError("Target not found")
        if not target.is_active:
            raise ValueError("Target is inactive")

        # If there's already an active (running / mid-flight) deployment of
        # this app on this target, stop it first and reuse its port. This
        # keeps the user-facing URL stable across version bumps — bookmarks,
        # shared links, and external integrations don't break when v2 ships.
        prior_port: int | None = None
        prior_result = await db.execute(
            select(Deployment).where(
                Deployment.app_id == app_id,
                Deployment.target_id == target_id,
                Deployment.status.in_(ACTIVE_DEPLOYMENT_STATUSES),
            )
        )
        for old in prior_result.scalars().all():
            if prior_port is None and old.allocated_port is not None:
                prior_port = old.allocated_port
            await self.stop(db, old.id, user_id)
            logger.info(
                "deploy: stopped prior deployment %s of app=%s on target=%s (was port %s)",
                old.id, app_id, target_id, old.allocated_port,
            )

        # Allocate a port atomically with the new Deployment row.
        # Prefer the port the prior deployment held so the URL stays the same.
        port = await self._allocate_port(db, target, preferred=prior_port)

        deployment = Deployment(
            app_id=app_id, version=version, target_id=target_id,
            allocated_port=port, status="building",
            deployed_by=user_id,
        )
        db.add(deployment)
        await db.flush()  # populate deployment.id before referencing it in AuditLog
        db.add(AuditLog(
            user_id=user_id, action="deployment.deploy",
            resource_type="deployment", resource_id=deployment.id,
            details=f"Deploy app={app_id} v{version} → target={target.name}",
        ))
        await db.commit()
        await db.refresh(deployment)

        # Run the rest in the background so the API call returns immediately.
        asyncio.create_task(self._run_deploy(deployment.id, app_id, version, target_id))
        return deployment

    async def _run_deploy(self, deployment_id: str, app_id: str, version: int, target_id: str) -> None:
        async with async_session() as db:
            try:
                artifact = await builder.build_app(app_id, version)
                await db.execute(
                    update(Deployment).where(Deployment.id == deployment_id)
                    .values(status="uploading", build_artifact_path=str(artifact))
                )
                await db.commit()

                target = await self.get_target(db, target_id)
                deployment = await self.get_deployment(db, deployment_id)
                if not target or not deployment:
                    return
                credential = await self._resolve_credential(db, target)
                deployer = get_deployer(target, credential)
                public_url = await deployer.deploy(deployment, artifact, deployment.allocated_port)

                await db.execute(
                    update(Deployment).where(Deployment.id == deployment_id)
                    .values(status="running", public_url=public_url,
                            last_health_at=datetime.now(timezone.utc),
                            last_health_status="ok")
                )
                await db.commit()
                logger.info("Deployment %s running at %s", deployment_id, public_url)
            except Exception as e:
                logger.exception("Deployment %s failed", deployment_id)
                await db.execute(
                    update(Deployment).where(Deployment.id == deployment_id)
                    .values(status="failed", error=str(e)[:1000],
                            stopped_at=datetime.now(timezone.utc))
                )
                await db.commit()
                # Best-effort failure notification (never masks the original error).
                try:
                    from ..notifications.service import notify_deploy_failed
                    target = await self.get_target(db, target_id)
                    await notify_deploy_failed(
                        db, app_id, target.name if target else target_id, str(e)[:1000])
                except Exception:
                    logger.exception("deploy-failure notification failed")

    async def stop(self, db: AsyncSession, deployment_id: str, user_id: str) -> Deployment | None:
        deployment = await self.get_deployment(db, deployment_id)
        if not deployment:
            return None
        target = await self.get_target(db, deployment.target_id)
        try:
            if target:
                credential = await self._resolve_credential(db, target)
                deployer = get_deployer(target, credential)
                await deployer.stop(deployment)
        except Exception as e:
            logger.warning("Stop request failed for %s: %s", deployment_id, e)
        deployment.status = "stopped"
        deployment.stopped_at = datetime.now(timezone.utc)
        db.add(AuditLog(
            user_id=user_id, action="deployment.stop",
            resource_type="deployment", resource_id=deployment.id,
            details=f"Stopped deployment of app={deployment.app_id} v{deployment.version}",
        ))
        await db.commit()
        await db.refresh(deployment)
        return deployment

    async def get_logs(self, db: AsyncSession, deployment_id: str, n: int = 200) -> list[str]:
        deployment = await self.get_deployment(db, deployment_id)
        if not deployment:
            return []
        target = await self.get_target(db, deployment.target_id)
        if not target:
            return []
        credential = await self._resolve_credential(db, target)
        deployer = get_deployer(target, credential)
        return await deployer.tail_logs(deployment, n)

    async def health_check(self, db: AsyncSession, deployment_id: str) -> HealthResult:
        deployment = await self.get_deployment(db, deployment_id)
        if not deployment:
            return HealthResult(ok=False, detail="Deployment not found")
        target = await self.get_target(db, deployment.target_id)
        if not target:
            return HealthResult(ok=False, detail="Target gone")
        credential = await self._resolve_credential(db, target)
        deployer = get_deployer(target, credential)
        result = await deployer.health(deployment)
        deployment.last_health_at = datetime.now(timezone.utc)
        if result.ok:
            deployment.last_health_status = "ok"
            deployment.consecutive_health_failures = 0
        else:
            deployment.last_health_status = "error"
            deployment.consecutive_health_failures = (deployment.consecutive_health_failures or 0) + 1
        await db.commit()
        return result

    async def maybe_auto_rollback(self, db: AsyncSession, deployment_id: str) -> Deployment | None:
        """If a running deployment has failed health probes past the configured
        threshold, redeploy the most recent *healthy* prior version to the same
        target. Returns the new deployment, or None if no action was taken.

        Safe against loops: candidates must currently be marked healthy
        (`last_health_status == 'ok'`), so a version that's known-bad is never a
        rollback target; redeploying creates a fresh row whose failure counter
        starts at zero.
        """
        from ..platform_settings.service import get_setting

        if not bool(await get_setting(db, "auto_rollback_enabled")):
            return None
        threshold = int(await get_setting(db, "auto_rollback_fail_threshold") or 3)

        d = await self.get_deployment(db, deployment_id)
        if not d or d.status != "running":
            return None
        if (d.consecutive_health_failures or 0) < threshold:
            return None

        candidate = (await db.execute(
            select(Deployment).where(
                Deployment.app_id == d.app_id,
                Deployment.target_id == d.target_id,
                Deployment.id != d.id,
                Deployment.version != d.version,
                Deployment.last_health_status == "ok",
            ).order_by(Deployment.started_at.desc())
        )).scalars().first()
        if not candidate:
            logger.warning(
                "auto-rollback: app=%s target=%s v%s unhealthy but no healthy "
                "prior version to roll back to", d.app_id, d.target_id, d.version,
            )
            return None

        logger.warning(
            "auto-rollback: app=%s target=%s v%s unhealthy (%s consecutive "
            "failures) → redeploying v%s",
            d.app_id, d.target_id, d.version, d.consecutive_health_failures,
            candidate.version,
        )
        # deploy() stops the still-running failing deployment and reuses its port,
        # so the public URL stays stable across the rollback.
        new_dep = await self.deploy(db, d.app_id, candidate.version, d.target_id, d.deployed_by)
        db.add(AuditLog(
            user_id=d.deployed_by, action="deployment.auto_rollback",
            resource_type="deployment", resource_id=new_dep.id,
            details=(
                f"Auto-rollback: app={d.app_id} v{d.version} unhealthy after "
                f"{d.consecutive_health_failures} consecutive failures → "
                f"redeployed healthy v{candidate.version}"
            ),
        ))
        await db.commit()
        return new_dep

    async def blue_green_deploy(self, db: AsyncSession, app_id: str, version: int,
                                target_id: str, user_id: str, *,
                                health_attempts: int = 3, health_interval: float = 0.5) -> dict:
        """Deploy a new version (green) ALONGSIDE the current one (blue), health-
        check it, then cut over — or abort and leave blue serving if green is
        unhealthy. Zero-downtime: blue keeps serving until green is proven good.
        """
        if not (await db.execute(
            select(AppVersion).where(AppVersion.app_id == app_id, AppVersion.version == version)
        )).scalar_one_or_none():
            raise ValueError(f"App {app_id} has no version v{version}")
        target = await self.get_target(db, target_id)
        if not target:
            raise ValueError("Target not found")
        if not target.is_active:
            raise ValueError("Target is inactive")

        # Current live deployment (blue) — left running through the cutover.
        blue = (await db.execute(
            select(Deployment).where(
                Deployment.app_id == app_id, Deployment.target_id == target_id,
                Deployment.status == "running",
            ).order_by(Deployment.started_at.desc())
        )).scalars().first()

        # Fresh port (the allocator already excludes ports held by active
        # deployments, so blue's port is never reused).
        port = await self._allocate_port(db, target)
        green = Deployment(app_id=app_id, version=version, target_id=target_id,
                           allocated_port=port, status="building", deployed_by=user_id)
        db.add(green)
        await db.flush()
        green_id = green.id
        db.add(AuditLog(user_id=user_id, action="deployment.blue_green.start",
                        resource_type="deployment", resource_id=green_id,
                        details=f"Blue/green deploy of app={app_id} v{version} → {target.name}"))
        await db.commit()

        # Build + deploy green synchronously so we can gate the cutover on it.
        try:
            artifact = await builder.build_app(app_id, version)
            await db.execute(update(Deployment).where(Deployment.id == green_id)
                             .values(status="uploading", build_artifact_path=str(artifact)))
            await db.commit()
            credential = await self._resolve_credential(db, target)
            deployer = get_deployer(target, credential)
            green = await self.get_deployment(db, green_id)
            public_url = await deployer.deploy(green, artifact, port)
            await db.execute(update(Deployment).where(Deployment.id == green_id)
                             .values(status="running", public_url=public_url,
                                     last_health_at=datetime.now(timezone.utc),
                                     last_health_status="ok"))
            await db.commit()
        except Exception as e:
            logger.exception("blue/green: green deploy failed for %s", green_id)
            await db.execute(update(Deployment).where(Deployment.id == green_id)
                             .values(status="failed", error=str(e)[:1000],
                                     stopped_at=datetime.now(timezone.utc)))
            await db.commit()
            return {"switched": False, "reason": "green_deploy_failed",
                    "green": green_id, "error": str(e)[:300]}

        # Probe green a few times before committing the cutover.
        credential = await self._resolve_credential(db, target)
        deployer = get_deployer(target, credential)
        healthy = False
        green = await self.get_deployment(db, green_id)
        for _ in range(max(1, health_attempts)):
            try:
                if (await deployer.health(green)).ok:
                    healthy = True
                    break
            except Exception:
                pass
            await asyncio.sleep(health_interval)

        if not healthy:
            # Abort: retire green, leave blue serving.
            try:
                await deployer.stop(green)
            except Exception:
                pass
            await db.execute(update(Deployment).where(Deployment.id == green_id)
                             .values(status="failed", error="failed health checks after deploy",
                                     stopped_at=datetime.now(timezone.utc)))
            db.add(AuditLog(user_id=user_id, action="deployment.blue_green.aborted",
                            resource_type="deployment", resource_id=green_id,
                            details=f"Green v{version} unhealthy → kept blue "
                                    f"{'v'+str(blue.version) if blue else '(none)'} live"))
            await db.commit()
            return {"switched": False, "reason": "green_unhealthy",
                    "green": green_id, "blue": blue.id if blue else None}

        # Cutover: stop blue (if any), green is now the sole live deployment.
        if blue and blue.id != green_id:
            await self.stop(db, blue.id, user_id)
        db.add(AuditLog(user_id=user_id, action="deployment.blue_green.switch",
                        resource_type="deployment", resource_id=green_id,
                        details=f"Cut over to v{version}; retired "
                                f"{'v'+str(blue.version) if blue else '(none)'}"))
        await db.commit()
        return {"switched": True, "green": green_id, "green_version": version,
                "retired_blue": blue.id if blue else None}

    # After this many consecutive failed probes with no healthy fallback, stop
    # probing a deployment — it's dead (e.g. the agent no longer has it). Keeps
    # the health loop (and logs) from flooding 404s forever.
    GIVE_UP_FAILURES = 8

    async def maybe_give_up(self, db: AsyncSession, deployment_id: str) -> bool:
        """Mark a chronically-unhealthy deployment 'stopped' so it's no longer probed."""
        d = await self.get_deployment(db, deployment_id)
        if not d or d.status != "running":
            return False
        if (d.consecutive_health_failures or 0) < self.GIVE_UP_FAILURES:
            return False
        d.status = "stopped"
        d.stopped_at = datetime.now(timezone.utc)
        d.error = ((d.error + " | ") if d.error else "") + "gave up after repeated health failures"
        db.add(AuditLog(
            user_id=d.deployed_by, action="deployment.gave_up",
            resource_type="deployment", resource_id=d.id,
            details=f"Stopped probing after {d.consecutive_health_failures} consecutive "
                    f"health failures (app={d.app_id} v{d.version})",
        ))
        await db.commit()
        logger.warning("health: gave up on deployment %s after %s failures",
                       d.id, d.consecutive_health_failures)
        return True

    async def get_active_deployment_for_version(self, db: AsyncSession, app_id: str, version: int) -> Deployment | None:
        result = await db.execute(
            select(Deployment)
            .where(
                Deployment.app_id == app_id,
                Deployment.version == version,
                Deployment.status == "running",
            )
            .order_by(Deployment.started_at.desc())
        )
        return result.scalars().first()

    # ---------- Helpers ----------

    async def _allocate_port(
        self,
        db: AsyncSession,
        target: DeploymentTarget,
        *,
        preferred: int | None = None,
    ) -> int:
        """Pick a port for a new deployment.

        If `preferred` is in the target's range and currently unused, return it.
        Otherwise fall back to the lowest free port. Used by the deploy() flow
        to keep a stable URL across version bumps: stop the old deployment,
        capture its port, hand it back to the new one.
        """
        result = await db.execute(
            select(Deployment.allocated_port).where(
                Deployment.target_id == target.id,
                Deployment.status.in_(ACTIVE_DEPLOYMENT_STATUSES),
            )
        )
        used = {p for (p,) in result.all() if p is not None}

        if preferred is not None \
                and target.port_range_start <= preferred <= target.port_range_end \
                and preferred not in used:
            return preferred

        for candidate in range(target.port_range_start, target.port_range_end + 1):
            if candidate not in used:
                return candidate
        raise RuntimeError(
            f"No free ports in target '{target.name}' "
            f"({target.port_range_start}-{target.port_range_end})"
        )

    async def _resolve_credential(self, db: AsyncSession, target: DeploymentTarget) -> str | None:
        if not target.credential_secret_id:
            return None
        result = await db.execute(select(Secret).where(Secret.id == target.credential_secret_id))
        secret = result.scalar_one_or_none()
        if not secret or not secret.encrypted_value:
            return None
        return encryption_service.decrypt(secret.encrypted_value)


deployments_service = DeploymentsService()


# ---------- Background health loop ----------

async def health_loop(interval: float = 30.0) -> None:
    """Periodically probe every running deployment so the UI stays accurate."""
    while True:
        try:
            async with async_session() as db:
                result = await db.execute(
                    select(Deployment).where(Deployment.status == "running")
                )
                deployments = list(result.scalars().all())
            for d in deployments:
                async with async_session() as db:
                    try:
                        res = await deployments_service.health_check(db, d.id)
                        if not res.ok:
                            rolled = await deployments_service.maybe_auto_rollback(db, d.id)
                            if not rolled:
                                await deployments_service.maybe_give_up(db, d.id)
                    except Exception:
                        logger.exception("health probe failed for %s", d.id)
        except Exception:
            logger.exception("health_loop iteration failed")
        await asyncio.sleep(interval)
