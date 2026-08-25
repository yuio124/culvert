---
name: status
description: Read-only Context Governor status — enabled state, plugin version, worker type/model, policy source, event log, duplicate-hook warnings, and recent deny/approve counts in one screen.
---

This skill shows the current state of Context Governor. It is **read-only** — it
must not write or change any file.

There is exactly one thing to do:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/status.py"
```

Run it and show the output to the user **verbatim**. Do not summarize or
restructure — if there is a WARNING line, that line is exactly what the user
needs to see.

If `${CLAUDE_PLUGIN_ROOT}` appears unsubstituted, run `../../scripts/status.py`
relative to this skill file (i.e. `scripts/status.py` at the plugin root).
