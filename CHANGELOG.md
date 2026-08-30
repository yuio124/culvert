# Changelog

## 0.3.1 (unreleased)

Corrective patch after the first natural-workload audit. Adjusted CULVERT's
DENY handoff after an observed case where the Primary narrowed the requested
operation (13 metadata keys -> 5) while attempting to make execution bounded.
One observed failure case; it partially falsified the assumption that a
verbatim quote plus an explicit anti-shrink instruction preserves the
operation. The blocker mechanism and the classifier are unchanged.

- DENY handoff v3: escape valves reordered by semantic-preservation safety —
  (0) a note that the Read tool (limit/offset) is not gated, for plain file
  inspection; (1) context-worker delegation of the unchanged operation as the
  default; (2) a justified CULVERT_OVERRIDE when the operation is genuinely
  bounded and preserving it matters (no longer framed as a costly last
  resort); (3) output-only caps, now defined explicitly: bounding may reduce
  returned output but must not reduce the set of records, fields, checks, or
  computations performed — with a self-check ("would the complete output fit
  under the cap?"); (4) batching without merging or dropping operations.
- Offline auditor: identifier/literal disappearance diff between a denied call
  and its follow-up rewrites (Bash commands, Write/Edit content, delegation
  prompts). Evidence display only — identifiers can legitimately disappear in
  a normal rewrite; no automatic semantic-loss verdict.
- Synthetic regression fixture reproducing the 13->5 narrowing shape
  (including the Write-mediated rewrite) as a hard release gate.
- long-inline-python is unchanged: the incident showed narrowing after the
  deny, not that the deny itself was a false positive (the original command
  never ran, so its output size is censored). Queued for replay/shadow audit.

## 0.3.0 (unreleased)

Observability release. Success criterion: an incident where a CULVERT deny led
the primary agent to rewrite, shrink, or drop part of the original work must be
discoverable afterwards from events.jsonl plus the session transcript alone.

- events.jsonl gains join/version metadata: `tool_use_id`, `prompt_id`,
  `session_id`, `transcript_path`, `permission_mode`, `culvert_version` (read
  from the loaded plugin copy itself), `policy_hash` (sha256 of the effective
  merged policy, sort_keys). All fields fail-open; events stay content-free.
- Verbatim handoff v2 deny message: quotes the rejected command inside a
  4-backtick fence; a single trivially-parsed heredoc may have its body
  abridged (marked, with an instruction not to copy the abridged quote into
  the worker prompt); anything ambiguous or over-long omits the quote and
  instructs delegating the original tool call verbatim — never mid-truncated.
- FP corrections: `grep -r pattern file1 file2` over a few explicit, existing,
  small files is no longer recursive-search (no-target, directory/glob/$VAR
  targets, nonexistent paths, -e/-f option shifting, >5 files, or oversized
  totals all keep the deny); inline-python threshold raised 200 -> 400 with a
  new unbounded file-dump guard (`open(...).read` denies at any length).
- New read-only offline auditor `tools/analyze_session.py`: joins events with
  transcripts on tool_use_id, prints one human-reviewable packet per deny
  (rejected call, next 3 tool calls, same-prompt events, worker delegation
  prompt head), and always surfaces join failures. No automatic behavior
  classification. Audit output can quote private command content — keep local.
- No changes to routing, the RESULT validator, or the blocker mechanism.

## 0.2.5 (unreleased)

- `/culvert:status` now shows `Loaded version` (the plugin copy this session
  actually runs) and `Installed version` (the record for this project) as
  separate lines, and warns when a session is running a stale loaded copy —
  observed in real use after updating the plugin mid-session. A newer loaded
  copy than installed is reported plainly as a version mismatch, without
  guessing the cause. Unparseable versions never crash the report.
- The install record is selected only from enabled culvert entries whose
  projectPath matches this project (or user scope); records of other projects
  are never picked. If the plugin registry is missing or unreadable,
  `Installed version : unavailable` is shown and the rest of status still runs.
- Status remains read-only: no auto-update, no reload, no cache changes.
  No changes to the classifier, routing, policy defaults, or the validator.

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
