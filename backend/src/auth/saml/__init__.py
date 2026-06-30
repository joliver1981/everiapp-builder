"""SAML 2.0 Single Sign-On (SP side).

A browser-redirect flow distinct from the password-based provider chain:
  GET  /api/auth/saml/{id}/login    → AuthnRequest, redirect to the IdP
  POST /api/auth/saml/{id}/acs      → consume the SAMLResponse, provision, issue JWT
  GET  /api/auth/saml/{id}/metadata → SP metadata XML for the IdP admin

The crypto (signature validation, request signing) is handled by `python3-saml`,
an OPTIONAL extra (`pip install .[saml]`). All routes degrade to a clean 501 when
it isn't installed, mirroring how external DB drivers are opt-in. The non-crypto
logic (settings building, attribute→identity mapping, group→role) lives in
`settings_builder` with no heavy imports, so it's fully unit-testable.
"""
