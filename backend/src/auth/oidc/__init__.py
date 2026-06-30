"""OpenID Connect SSO (Authorization Code + PKCE).

Lighter than SAML — pure httpx + PyJWT, no XML/xmlsec — so the whole flow is
testable against a mock IdP. Shape mirrors the SAML module:
  GET /api/auth/oidc/{id}/login     → redirect to the IdP authorize endpoint
  GET /api/auth/oidc/{id}/callback  → exchange code, validate id_token, provision, issue JWT
  GET /api/auth/oidc/providers      → public list for login-page buttons

Per-request state (nonce + PKCE verifier + return_to) rides in a signed,
short-lived cookie, so there's no server-side session store.
"""
