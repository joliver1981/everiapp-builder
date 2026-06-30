"""Code-review approval workflow for app publishing.

When the platform setting `require_publish_approval` is on, developers can no
longer publish directly — they submit a publish request that an admin reviews
(seeing the security-scan posture) and approves or rejects. Approval performs
the actual version snapshot, crediting the original requester as author.
"""
