"""Base auth-provider abstraction.

Adapted from a production-tested auth-provider pattern — kept the AuthResult shape
but converted role to AIHub's string role model (admin/developer/user instead
of integer tiers).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AuthResult:
    """Result of an authentication attempt against an identity provider."""
    success: bool
    username: str = ""
    display_name: str = ""
    email: Optional[str] = None
    external_id: str = ""
    groups: list[str] = field(default_factory=list)
    error: Optional[str] = None


class BaseAuthProvider(ABC):
    @property
    @abstractmethod
    def provider_type(self) -> str:
        """e.g. 'ldap', 'mock', 'local'."""

    @abstractmethod
    def authenticate(self, username: str, password: str) -> AuthResult:
        """Validate credentials, returning AuthResult with attributes on success."""

    @abstractmethod
    def test_connection(self) -> tuple[bool, str]:
        """Test connectivity to the provider. Returns (ok, message)."""
