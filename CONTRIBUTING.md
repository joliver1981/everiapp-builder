# Contributing

Thanks for your interest in EveriApp Builder. Contributions are welcome —
bug reports, fixes, and features alike.

## Before you start

- **License.** The platform (`backend/`, `frontend/`, `aihub-agent/`, `deploy/`,
  `scripts/`, and the installers) is licensed under the
  [Business Source License 1.1](LICENSE). `app-sdk/` and `app-template/` are
  licensed under Apache 2.0. Read [`LICENSE`](LICENSE) before contributing.
- **Contributor License Agreement.** We require every contributor to agree to
  the [Contributor License Agreement](CLA.md) before a pull request can be
  merged. The CLA lets EveriAI LLC keep a single, clean set of rights to the
  codebase — including the ability to relicense it, which the Business Source
  License's Change Date depends on. You agree once; it covers all your future
  contributions.

  To agree, include this line in the description of your first pull request:

  > I have read and agree to the Contributor License Agreement in CLA.md.

  A CLA check may also be enforced automatically on pull requests.

## How to contribute

1. Open an issue first for anything larger than a small fix, so we can agree
   on the approach before you invest time.
2. Fork the repository and create a branch from `main`.
3. Follow the existing code style; match the surrounding code's naming,
   comment density, and idiom.
4. Add tests. Backend HTTP endpoints get an integration test that hits the real
   route through FastAPI's `TestClient` — see `CLAUDE.md` for the pattern and
   the reasons behind it.
5. Make sure the full gate passes before opening the pull request:

   ```
   .venv/Scripts/python.exe .claude/hooks/green-gate.py
   ```

   That runs the backend tests, the agent tests, and the frontend type-check.
6. Open the pull request against `main` with a clear description of what
   changed and why.

## Reporting security issues

Please do not open public issues for security vulnerabilities. See
[`SECURITY.md`](SECURITY.md) for how to report them privately.
