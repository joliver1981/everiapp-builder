"""Forward audit events to an external SIEM (HTTP push or syslog).

A background loop tails the `audit_logs` table from a persisted cursor and
pushes new rows to the configured endpoint. Endpoint/transport/auth are runtime
platform settings, so an operator can point this at Splunk / Sentinel / Elastic
without a restart. The archives written by audit_rotation remain the system of
record; this is the real-time feed.
"""
