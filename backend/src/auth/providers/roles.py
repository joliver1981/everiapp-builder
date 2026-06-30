"""Shared group→role resolution (highest-rank-wins), used by SAML + OIDC.

Mirrors the LDAP provider's policy so all external providers map directory
groups to AIHub roles the same way.
"""
from __future__ import annotations

_ROLE_RANK = {"user": 1, "developer": 2, "admin": 3}


def _group_cn(group: str) -> str:
    """Directory groups are usually plain names, but tolerate AD DNs (CN=Foo,...)."""
    if group.upper().startswith("CN="):
        return group.split(",", 1)[0][3:]
    return group


def resolve_role(groups: list[str], group_role_mapping: dict | None,
                 default_role: str = "user") -> str:
    """Return the highest-ranked role among matched groups, else the default."""
    if not group_role_mapping or not groups:
        return default_role or "user"
    names = {_group_cn(g) for g in groups} | set(groups)
    best = default_role or "user"
    best_rank = _ROLE_RANK.get(best, 0)
    for group_name, role in group_role_mapping.items():
        if group_name in names and _ROLE_RANK.get(role, 0) > best_rank:
            best, best_rank = role, _ROLE_RANK.get(role, 0)
    return best
