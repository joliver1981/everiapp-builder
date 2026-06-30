"""Scheduled backups + restore.

Reuses the same online-SQLite-backup approach as the `aihub backup` CLI, exposes
it over the admin API + a background loop, and adds a restore that's APPLIED at
next startup (so we never overwrite the live DB while it's open).
"""
