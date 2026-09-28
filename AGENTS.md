# Maintenance instructions

This is a self-hosted Python FastAPI application, not a Sites project. Start with README.md and docs/ARCHITECTURE.md. Keep a single process/worker for the SQLite-backed engine and Telegram polling. Never package .env, credentials, runtime data or test-user secrets.

Maintain these invariants:
- Every private API is authenticated. Every browser mutation requires an exact Origin and session CSRF token.
- Preserve the owner's MASTER_KEY. Never regenerate it against an existing database.
- Models receive no credentials or shell. MCP tools require explicit agent grants and owner approval bound to the exact request, server and catalog. Do not bypass app/toolbus.py. The maintenance MCP writes proposals only, never live source files.
- Any new external action needs an explicit application permission design. Do not silently turn model output into side effects.
- Publication approval is bound to content and connector configuration; uncertain network outcomes must not retry automatically.
- Outbound HTTP uses app/net.py: public HTTPS, pinned DNS/IP, certificate verification, no redirects or proxy inheritance, capped bodies and timeouts.
- Keep frontend writes and API contracts consistent. Render untrusted text as text, never HTML. Polling must preserve open form contents.
- Token limits are execution controls, not a promised billing cap. Unknown usage must remain marked uncertain.

Run `python -m pytest -q` with requirements-dev.txt installed. For the DOM integration test, `npm ci` then `python tests/run_ui.py`. That test uses an isolated mock provider; it must never publish externally or reuse user credentials. Browser visual QA and real-account/VPS acceptance are separate gates. Update docs when capabilities or gates change.

Version 2.0: Russian interface and operator-facing errors are required. Preserve project agent/skill boundaries. JEV is advisory, never authorization. Restores must disable automations and integrations and invalidate tool approvals. Twenty is a separate API-integrated service; never claim the agent core is a fork of Twenty.
