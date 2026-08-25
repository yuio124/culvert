# Changelog

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
- `/context-governor:status` read-only status skill
- Marketplace installation (`.claude-plugin/marketplace.json` included)
