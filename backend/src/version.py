"""The platform version — single source of truth for the backend.

Bump BOTH this and frontend/package.json "version" together whenever a
user-visible change lands (minor for features, patch for fixes). The sidebar
shows UI vs API versions side by side and flags a mismatch, so a stale SPA
bundle or a stale backend process is visible at a glance instead of being a
debugging session.
"""

PLATFORM_VERSION = "0.3.0"
