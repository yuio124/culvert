# Changelog

## 0.2.4 (unreleased)

- `/culvert:status` now detects conflicting installs instead of only grepping
  project settings for hook script names: enabledPlugins merged across
  user/project/local scopes, the known predecessor plugin (context-governor),
  structurally same-role plugins (ship a context-worker agent or the same gate
  scripts), project-level context-worker.md, legacy state dirs, and shadow
  skills-dir copies. Hook execution order is not guessed — findings are
  reported as potential conflicts; nothing is disabled or deleted.
- The plugin registry (~/.claude/plugins/installed_plugins.json) is treated as
  best-effort: if missing or unreadable, registry checks are skipped with a
  NOTE and status keeps working.
- No changes to the classifier, routing, policy defaults, or the RESULT validator.

## 0.2.3 (rc3, unreleased)

- long-command is no longer a standalone argument-length rule. Measured on real
  sessions, every standalone firing was a false deny (6/6) — the command string
  is already in the primary context when the hook runs. It now fires only as a
  backstop: command over max_command_length AND at least one segment whose
  output could not be statically bounded.
- max_command_length default raised 500 -> 1200 (conservative initial value,
  not a tuned constant).
- heredoc / long-inline-python are intentionally unchanged: they signal inline
  exploratory execution, where the follow-up iteration loop is the real risk.

## 0.2.2 (rc2, unreleased)

- Classifier semantics documented: the gate targets *context risk* (large tool
  arguments/results reaching the primary thread), not computational cost
- Bounded-output exemption: statically small outputs (final `head`/`tail` ≤100
  lines, `wc`, non-recursive `grep -c`, scalar aggregate sqlite queries) are
  allowed even when a heavy pattern matches (argument-size rules and test runs
  are never exempted)
- Test-runner detection extended to Node: `npm/pnpm/yarn [run] test`,
  `node --test`, `npx jest|vitest` (supported scope: Python + Node only)
- New `fan-out-read` rule: `for … cat`, `cat <glob>`, `find -exec cat`,
  `find | xargs cat` are delegated like recursive search
- Metric cleanup: routing-enforcement events separated from classifier metrics

## 0.2.1

- Standalone plugin (extracted from the original project-local implementation)
- Regular-subagent enforcement for context-worker (teammate spawn denied)
- Opus worker model pinned via agent frontmatter
- Per-project policy override with fail-safe merge and warnings
- `/culvert:status` read-only status skill
- Marketplace installation (`.claude-plugin/marketplace.json` included)
