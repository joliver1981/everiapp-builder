"""Iframe embedding: per-app embed config, signed embed tokens, and a framed
bootstrap endpoint that declares `frame-ancestors` for the allowed parents.

The platform itself sets no X-Frame-Options, so same-origin embedding already
works; this module adds opt-in cross-origin embedding with an explicit allow-list.
"""
