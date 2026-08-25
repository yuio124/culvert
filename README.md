# CULVERT

**C**ontext **U**nbounded-**L**oad **V**alidation, **E**xecution **R**outing & **T**riage — a Claude Code plugin that keeps your **primary thread as a coordinator** and pushes
execution-heavy work into an **isolated context-worker subagent**.

## Why

Long agentic sessions die by context bloat: raw DB dumps, test logs, and recursive
search output pile into the main thread until compaction erases what mattered.
CULVERT enforces a division of labor — the primary thread decides,
an isolated worker executes and returns a compact `RESULT` artifact.

Two kinds of claims, with different strength of evidence:

**Enforced by code** (hook behavior, re-verified by the bundled tests on every change):
- Heavy execution is denied on the primary thread and routed to the worker.
- context-worker always runs as a **regular subagent** — teammate-style spawns
  (the source of cross-agent idle/coordination noise) are denied.
- Worker replies are forced into a compact `RESULT` artifact (8KB cap).

**Observed once** (a single real workload — a multi-DB log investigation — not a
benchmark): primary-thread context growth was 83K tokens without isolation
discipline and 50K with the governor, and agent-coordination noise went from 22
deliveries to 0 after regular-subagent enforcement. Workload size differed
between runs, so treat these as one observation, not a promised ratio.
**Actual effect depends on your workload** — sessions that never touch heavy
execution will see little difference.

## What it enforces

| Hook | Enforcement |
|---|---|
| `PreToolUse` (Bash/Read/Agent) | Denies **context-risk** calls on the primary thread — the criterion is not computational cost but the risk of flooding the primary context with large tool arguments/results: DB queries, test runs (Python: pytest/unittest · Node: npm/pnpm/yarn test, node --test, npx jest/vitest — other ecosystems not yet covered), recursive grep/rg/find, fan-out reads (`for … cat`, `cat <glob>`, `find -exec cat`, `xargs cat`), heredocs, inline python over 200 chars, commands over 500 chars, reads/dumps of files over 200KB. Subagents are exempt. **Bounded-output exemption**: commands whose final output is statically small (`\| head/tail -N` ≤100 lines, `wc`, non-recursive `grep -c`, scalar aggregate sqlite queries) are allowed; argument-size rules and test runs are never exempted. |
| `PreToolUse` (Agent) | context-worker must run as a **regular subagent** — calls with a `name` parameter (teammate spawn) are denied. |
| `SubagentStop` | Worker's final message must be a `RESULT` artifact under 8KB — oversized or schema-less replies are sent back for rewriting (with a loop guard). |
| `SessionStart` | Re-injects the coordinator framing on startup / clear / compact. |

The worker agent (`culvert:context-worker`) is pinned to the Opus model
via frontmatter. Every decision is logged to `.claude/governor-plugin/events.jsonl`
in your project (rule name and lengths only — never command contents).

## Install

```bash
claude plugin marketplace add <path-or-repo-of-this-plugin>
claude plugin install culvert@culvert --scope project
```

`--scope project` records the install in your project's `.claude/settings.json`,
so teammates get it from git automatically (make sure that file is not gitignored).
For a quick trial without installing: `claude --plugin-dir <path-to-this-plugin>`.

## Verify

```bash
claude plugin list        # culvert ✔ enabled
```

Inside a session, run `/culvert:status` — a read-only report:
enabled state, version, worker type/model, policy source, event-log path,
duplicate-hook warnings, and recent deny/approve counts.
Then ask for something heavy (`sqlite3 x.db 'SELECT 1'`) — you should see
`DELEGATE_REQUIRED: rule=db-query`.

## Configure (override)

The bundled `config/policy.json` is the immutable default — don't edit it.
Put only the keys you want to change in
`<your-project>/.claude/governor-plugin/policy.json`:

```jsonc
{ "max_command_length": 900 }          // adjust a threshold
{ "rules": { "test_run": false } }     // disable one rule
```

- No override file → defaults apply (works out of the box)
- Broken JSON → override ignored, defaults keep working (the governor never dies on config)
- Wrong type → that key is ignored; unknown keys are **warned**, not silently dropped
  (one `warn` event in the log, surfaced by `/culvert:status`)
- Env escape hatches: `GOVERNOR_POLICY=<file>` replaces the whole policy (testing),
  `GOVERNOR_DIR=<dir>` relocates state/logs
- False positive on one command? Prefix it: `GOVERNOR_OVERRIDE="reason" <command>` (logged)

## Uninstall

```bash
claude plugin uninstall culvert --scope project   # match the scope you installed with
```

Nothing else to clean up — the plugin never modifies your project besides its
own log/override directory (`.claude/governor-plugin/`), which you may delete.

## Known limits

- Heavy-command detection is regex-based: unusual quoting or wrappers can slip
  through, and the length rules can flag legitimate long commands (use
  `GOVERNOR_OVERRIDE=` for those).
- The gate reads `agent_id` to exempt subagents — behavior verified on
  Claude Code 2.1.243–2.1.245; harness changes may require re-validation.
- `SessionStart` re-injection covers startup/clear/compact; other entry paths
  (e.g. resume) rely on the conversation itself.
- Enforcement is per-project and advisory to the model: the model is told *why*
  a call was denied and how to delegate — it cannot be prevented from asking the
  user to disable the governor.

## Tests

```bash
python3 tests/run_cases.py    # hook decisions vs expected (26 cases)
python3 tests/test_v021.py    # override/fail-safe/status acceptance (30 checks)
```

## License

MIT — see [LICENSE](LICENSE). Changes in [CHANGELOG.md](CHANGELOG.md).
