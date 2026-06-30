"""First-run setup wizard endpoints.

`GET /api/setup/status` is public (just a needs_setup boolean) so the SPA can
decide whether to route a fresh admin into onboarding. The richer state and the
completion flag require admin.
"""
