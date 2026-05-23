# Security Policy

## Threat model

Bill Generator is designed as a **single-user, local-first** desktop
application. The default packaging launches a Flask + waitress server
bound to `127.0.0.1` and renders the UI inside a `pywebview` window. In
that mode there is no remote attack surface — only processes already on
your machine can talk to the app.

If you change the bind address (`BG_BIND_HOST=0.0.0.0`) so the server
becomes reachable on your LAN, you are stepping outside the supported
threat model. **Read the rest of this document first.**

## Known limitations

These are accepted trade-offs in the local-first model. They become real
risks the moment the app is exposed beyond `127.0.0.1`:

- **No authentication.** Any HTTP client that can reach the port can
  read every customer, edit invoices, change Supabase credentials, or
  trigger a backup. This is by design for a local desktop app.
- **CSRF protection is partial.** Only the bill create / edit forms
  carry a token. Other state-changing routes (config, accounting,
  delete, recover, supabase sync) currently rely on the loopback bind
  for safety. Adding global CSRF (e.g. via Flask-WTF) is on the
  roadmap.
- **No encryption at rest.** The SQLite database, `info.json`, and
  Supabase credentials are stored as plain files in your OS-specific
  application data directory.
- **Supabase URL is user-supplied.** The app validates the URL is HTTPS
  and not pointing at obvious internal hosts (RFC1918 / loopback), but
  treats it as trusted otherwise.
- **`file_location` for backup mirroring** must be inside the user's
  home directory; we reject paths outside it.

## Hardening defaults shipped on this branch

- A random 32-byte `SECRET_KEY` is generated and persisted to
  `DATA_DIR/secret.key` on first launch (env `SECRET_KEY` overrides).
- The desktop launcher and the `__main__` entrypoint bind to
  `127.0.0.1` by default.
- `session.persistent_notice` is rendered through Jinja's autoescape
  rather than `|safe`.

## If you need multi-user / network access

This app is **not the right tool** for that. Please run an alternative
or fork it and add proper authentication (Flask-Login + CSRF +
per-user data isolation) before exposing it.

## Reporting a vulnerability

Please open a private security advisory on GitHub or email the
maintainer. Don't file public issues for security bugs.
