"""Teams (explicit groups) + group-based app access enforcement.

Groups were previously implicit (a user's AD `ad_groups` + free-text
`AppPermission.group_name`), and access wasn't enforced. This adds named Teams
with membership, folds team names into a user's effective groups, and enforces
AppPermission for end users — while staying backward compatible: an app with NO
permission records remains open to all published-app viewers.
"""
