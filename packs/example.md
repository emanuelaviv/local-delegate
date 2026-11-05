# Example rule sheet

A rule sheet ("pack") gives the local model your standing rules when it drafts or checks text.
To make your own, copy this file under a new name in the same folder (for example
`my_rules.md`), edit it, and ask Claude to use it by name: "draft this reply with my_rules".
Packs live in `%LOCALAPPDATA%\local-delegate\packs`, and updates never overwrite them.

The `rules` block below is checked exactly by a script on every draft and every rules check:

```rules
forbid: OldProductName
forbid@website: FormerEmployer
forbid_near: Languages | Esperanto
require_near: French | B2
warn: synergy
```

What each kind of rule means:

- `forbid: term`: the term must not appear anywhere.
- `forbid@target: term`: the term must not appear when the draft's target is `target`
  (for example `website` or `cv`).
- `forbid_near: anchor | term`: on lines that contain the anchor, the term must not appear.
- `require_near: anchor | term`: on lines that contain the anchor, the term must appear.
- `require: term`: the term must appear somewhere.
- `warn: term`: flagged for you, not treated as an error.

Everything outside the block is guidance the model reads:

## Tone

- Short sentences and plain words.
- One clear request per message.
- Drafts only: you review and send everything yourself.
