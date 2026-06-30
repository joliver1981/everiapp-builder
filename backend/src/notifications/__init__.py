"""Email / SMTP notifications for platform events.

Best-effort: every notify_* helper is gated on `smtp_enabled` + a per-event
toggle and never raises into the caller (a failed email must not break a publish
or a deploy). The thin `_smtp_send` transport runs in a thread executor.
"""
