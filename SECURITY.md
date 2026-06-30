# Security Policy

## Reporting a vulnerability

If you discover a security vulnerability in AIHub, please report it
**privately**. Do not open a public GitHub issue for security problems.

- Use GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
  ("Report a vulnerability" under the repository's **Security** tab), or
- email the maintainers (see the repository owner's profile).

Please include steps to reproduce, affected versions/commit, and impact. We aim
to acknowledge reports promptly and will coordinate a fix and disclosure timeline
with you.

## Handling secrets

AIHub never requires you to commit secrets. Configuration comes from a
git-ignored `.env` file (see `.env.example`) and the built-in, Fernet-encrypted
secrets manager. Set strong, unique values for `JWT_SECRET_KEY` and
`MASTER_ENCRYPTION_KEY` in any non-local deployment.

## Supported versions

This project is in early development (0.x). Security fixes are applied to the
latest release on the default branch.
