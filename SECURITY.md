# Security policy

## Reporting a problem

Please report security problems privately, not in a public issue:

1. Open this repository's **Security** tab and select **Report a vulnerability** (GitHub's private
   vulnerability reporting).
2. Describe what you found and how to reproduce it, with the plugin version (*"local status"*
   shows it) and your Windows, Python and Ollama versions. Leave out real personal data and real
   file contents.

This is a personal open-source project, so responses are best-effort. Fixes ship in a new version,
and you'll be credited if you'd like.

## Supported versions

| Version | Supported |
|---|---|
| 0.4.x | Yes |
| Older | No; please update |

## What counts as a security problem

- Any way the model's work could leave the computer: a prompt sent anywhere other than Ollama on
  loopback, a cloud model accepted, or a local-only check that passes while cloud features are on.
- A model run that starts without the user's approval through the tools, beyond the known limits
  below.
- Reading a file outside the allowed folders, or one that the never-read list should block.
- A known key format reaching the model or the logs without being blanked.
- The connector or worker running, keeping or sending more than [docs/PRIVACY.md](docs/PRIVACY.md)
  describes.

## Known limits, by design

- **The connector asks for approval but can't enforce it.** Every model run requires
  `user_approved=true`, but the connector can't tell whether you actually said yes: the assistant
  sets that flag. Where your app's tool permissions allow it, have it ask before every
  `local_run` call.
- **Tool results go to Claude.** Anything a tool returns becomes part of your conversation. Use
  file-only mode for private material.
- **File contents are untrusted input.** A file can contain text that tries to steer the model,
  such as "ignore your instructions". The model's output is only ever a draft or an extraction,
  checked by scripts where possible. It is never executed, and the connector takes no action
  based on it. Review drafts before you use them.
- **Ollama's own security is Ollama's.** Setup keeps it on loopback with cloud features off; keep
  it updated.
- **It runs with your account's rights.** The allowed folders and the never-read list limit what
  the connector reads, not what your account can reach.
- **Redaction is a safety net.** It blanks known key formats; it can't recognize every secret, so
  don't point the connector at secrets.

## Supply chain

Standard-library Python only. No packages are installed, nothing is downloaded at run time, and
there is no compiled or minified code: every file is readable source.
