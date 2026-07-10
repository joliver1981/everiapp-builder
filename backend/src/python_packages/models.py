import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


class PythonPackage(Base):
    """An admin-installed Python package for the server-function environment.

    One row is both the MANIFEST entry (what the environment should contain —
    rebuilds re-install exactly these) and the JOB record (status/error of the
    last install/uninstall touching it) — the deployments row-as-job precedent.
    The bundled curated set has no rows; it's laid down by the installer and
    immutable here.
    """

    __tablename__ = "python_packages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # PEP 503 normalized ("Scikit_Learn" → "scikit-learn") — the collision key.
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    # What the admin typed, for display: "scikit-learn==1.5.0" or "tabulate".
    requested_spec: Mapped[str] = mapped_column(String(300))
    # Parsed pin; "" = latest. This is what a rebuild re-resolves.
    pinned_version: Mapped[str] = mapped_column(String(100), default="")
    # From the dist-info scan after a successful install.
    installed_version: Mapped[str] = mapped_column(String(100), default="")
    # pending → installing → installed; uninstalling; failed
    status: Mapped[str] = mapped_column(String(20), default="pending")
    error: Mapped[str] = mapped_column(Text, default="")
    requested_by: Mapped[str] = mapped_column(String(36), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
