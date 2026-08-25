"""CULVERT — PreToolUse gate (Bash|Read|Agent). v0.2.3.

Criterion (explicit): not "computationally heavy" but
**"risk of leaving large tool arguments/results in the primary context"**.

Evaluation order (classify_bash):
  1. Argument-size rules — heredoc, long inline python. These signal that the
     primary thread is about to run exploratory execution inline (the follow-up
     iteration loop is the real context risk), so they are never exempted.
  2. test-run — Python (pytest/unittest) + Node (npm/pnpm/yarn test, node --test,
     npx jest/vitest). Conservatively never exempted (suite output and duration
     are not statically predictable). Other ecosystems (Go/Cargo/.NET) are out of
     scope for now — see README.
  3. Bounded-output exemption — per segment (split on && ; newline): if the final
     output is statically known to be small (head/tail <=100 lines, head -c
     <=10KB, wc, non-recursive grep -c, scalar aggregate sqlite), skip the
     output-size rules for that segment.
  4. Output-size rules — db-query, recursive-search, fan-out-read, big-file-dump.
     Fan-out patterns that span segments (for..cat, find -exec cat, xargs cat)
     are also checked against the whole command.
  5. long-command backstop — fires only when the command exceeds
     max_command_length AND at least one segment's output could not be statically
     bounded. Length alone is not a context risk: the command string is already
     in the primary context when this hook runs.

Script errors fail open (exit 0). Worker name matching: _culvert.is_context_worker.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _culvert import is_context_worker, load_policy, log_event, rule_on

OVERRIDE_RE = re.compile(r'^\s*CULVERT_OVERRIDE=("([^"]+)"|\'([^\']+)\'|(\S+))\s+')

# 1. argument size
HEREDOC_RE = re.compile(r"(?<!<)<<(?!<)-?\s*['\"]?[A-Za-z_]")
INLINE_PY_RE = re.compile(r"\bpython3?\b[^\n|;&]{0,120}\s-c\b|\buv\s+run\b[^\n|;&]{0,120}\s-c\b")

# 2. test runners (Python + Node — the supported scope)
TEST_RE = re.compile(
    r"(?:^|[\s;&|])(?:\S*/)?pytest(?=\s|$)"
    r"|\bpython3?\s+-m\s+(pytest|unittest)\b"
    r"|(?:^|[\s;&|])(?:npm|pnpm|yarn)\s+(?:run\s+)?test\b"
    r"|\bnode\s+--test\b"
    r"|\bnpx\s+(?:jest|vitest)\b"
)

# 4. output size
SQLITE_RE = re.compile(r"\bsqlite3\b")
DB_FILE_RE = re.compile(r"\S+\.db\b")
DB_QUERYISH_RE = re.compile(r"\b(select|SELECT|insert|INSERT|connect|cursor|PRAGMA|pragma)\b")
GREP_R_RE = re.compile(r"\bz?grep\b[^\n|;&]*\s-[A-Za-z]*[rR]")
RG_RE = re.compile(r"\brg\b")
FIND_RE = re.compile(r"\bfind\b\s+(\S+)")
CAT_RE = re.compile(r"^\s*cat\s+((?:-\S+\s+)*)([^|;&<>]+?)\s*$")
# fan-out read — shell patterns that pour many file contents into the primary context
FANOUT_FOR_CAT_RE = re.compile(r"\bfor\b[^\n]*?\bdo\b[^\n]*?\bcat\b", re.S)
FANOUT_FIND_EXEC_RE = re.compile(r"\bfind\b[^\n]*?-exec\s+cat\b")
FANOUT_XARGS_RE = re.compile(r"\bxargs\s+(?:-\S+\s+)*cat\b")
FANOUT_CAT_GLOB_RE = re.compile(r"(?:^|[;&|]\s*)cat\s+(?:-\S+\s+)*[^|;&<>]*[*?]")

# 3. bounded output — only shapes whose small output is statically certain
SEG_SPLIT_RE = re.compile(r"&&|\|\||;|\n")
CAP_LINES_RE = re.compile(r"\|\s*(?:head|tail)\s+(?:-n\s*)?-?(\d+)\s*$")
CAP_BYTES_RE = re.compile(r"\|\s*head\s+-c\s*(\d+)\s*$")
WC_FINAL_RE = re.compile(r"\|\s*wc(?:\s+-[lwcm]+)?\s*$")
WC_CMD_RE = re.compile(r"^\s*wc\s+")
GREP_COUNT_RE = re.compile(r"^\s*z?grep\s+(?:-[A-Za-z]*c[A-Za-z]*\s|-c\s)")
SQL_SELECT_LIST_RE = re.compile(r"select\s+(.+?)\s+from", re.I | re.S)
SQL_AGG_RE = re.compile(r"^\s*(count|sum|avg|min|max|total)\s*\(.*\)\s*$", re.I | re.S)
BOUNDED_MAX_LINES = 100
BOUNDED_MAX_BYTES = 10240


def _sqlite_scalar(seg):
    """Scalar iff every SELECT list is aggregates only and there is no GROUP BY.

    Anything not statically certain is treated as non-exempt.
    """
    if not SQLITE_RE.search(seg):
        return False
    low = seg.lower()
    if "group by" in low:
        return False
    lists = SQL_SELECT_LIST_RE.findall(low)
    if not lists:
        return False  # cannot read the SELECT list -> stay conservative
    for lst in lists:
        for part in lst.split(","):
            if not SQL_AGG_RE.match(part):
                return False
    return True


def _bounded(seg):
    m = CAP_LINES_RE.search(seg)
    if m and int(m.group(1)) <= BOUNDED_MAX_LINES:
        return True
    m = CAP_BYTES_RE.search(seg)
    if m and int(m.group(1)) <= BOUNDED_MAX_BYTES:
        return True
    if WC_FINAL_RE.search(seg) or WC_CMD_RE.match(seg):
        return True
    if GREP_COUNT_RE.match(seg) and not GREP_R_RE.search(seg):
        return True
    if _sqlite_scalar(seg):
        return True
    return False


def _output_rule(seg, policy, cwd):
    """Output-size rule verdict for a single segment."""
    if rule_on(policy, "db_query"):
        if SQLITE_RE.search(seg):
            return "db-query"
        if DB_FILE_RE.search(seg) and DB_QUERYISH_RE.search(seg):
            return "db-query"
    if rule_on(policy, "recursive_search"):
        if GREP_R_RE.search(seg) or RG_RE.search(seg):
            return "recursive-search"
        m = FIND_RE.search(seg)
        if m and ("-exec" in seg or os.path.isdir(os.path.join(cwd, m.group(1)))):
            return "recursive-search"
    if rule_on(policy, "fan_out_read") and FANOUT_CAT_GLOB_RE.search(seg):
        return "fan-out-read"
    if rule_on(policy, "big_file_dump"):
        m = CAT_RE.match(seg)
        if m:
            for tok in m.group(2).split():
                p = tok if os.path.isabs(tok) else os.path.join(cwd, tok)
                try:
                    if os.path.getsize(p) > policy["max_read_bytes"]:
                        return "big-file-dump"
                except OSError:
                    pass
    return None


def classify_bash(cmd, policy, cwd):
    """Return the name of the rule that fires, or None. Order: module docstring."""
    # 1. argument size — never exempted
    if rule_on(policy, "heredoc") and HEREDOC_RE.search(cmd):
        return "heredoc"
    if rule_on(policy, "long_inline_python") and INLINE_PY_RE.search(cmd) \
            and len(cmd) > policy["max_inline_code_length"]:
        return "long-inline-python"
    # 2. test runners — never exempted (conservative)
    if rule_on(policy, "test_run") and TEST_RE.search(cmd):
        return "test-run"
    # 4'. fan-out shapes that span segments: check against the whole command first
    if rule_on(policy, "fan_out_read") and (
            FANOUT_FOR_CAT_RE.search(cmd) or FANOUT_FIND_EXEC_RE.search(cmd)
            or FANOUT_XARGS_RE.search(cmd)):
        return "fan-out-read"
    # 3+4. per segment: bounded output exempts the output-size rules
    exempt = rule_on(policy, "bounded_output_exemption")
    any_opaque = False
    for seg in SEG_SPLIT_RE.split(cmd):
        if not seg.strip():
            continue
        if exempt and _bounded(seg):
            continue
        rule = _output_rule(seg, policy, cwd)
        if rule:
            return rule
        any_opaque = True  # not bounded and no specific rule -> output statically unknown
    # 5. long-command, v0.2.3: no longer a standalone argument-size rule. The
    # command string is already in the primary context when this hook runs, so
    # length alone saves nothing (measured: 6/6 false denies). It now fires only
    # as a backstop: the command is long AND contains at least one segment whose
    # output could not be statically bounded.
    if any_opaque and rule_on(policy, "long_command") \
            and len(cmd) > policy["max_command_length"]:
        return "long-command"
    return None


def classify_read(tool_input, policy, cwd):
    if not rule_on(policy, "big_read"):
        return None
    if tool_input.get("limit") is not None or tool_input.get("offset") is not None:
        return None
    p = tool_input.get("file_path") or ""
    if not os.path.isabs(p):
        p = os.path.join(cwd, p)
    try:
        if os.path.getsize(p) > policy["max_read_bytes"]:
            return "big-read"
    except OSError:
        pass
    return None


def deny_reason(reason):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))


def deny(rule):
    deny_reason(
        f"DELEGATE_REQUIRED: rule={rule}. This call risks flooding the primary context "
        "with large arguments/results. "
        "Delegate it to context-worker (Agent tool, subagent_type: \"culvert:context-worker\") "
        "and synthesize from its compact RESULT artifact. "
        "If this is a false positive that the Primary must run directly, prefix the command with "
        "CULVERT_OVERRIDE=\"reason\" (the override is logged)."
    )


def main():
    data = json.load(sys.stdin)
    policy = load_policy()
    if not policy.get("enabled"):
        return  # OFF baseline: no verdicts, no logs
    if data.get("agent_id"):
        return  # subagents are exempt
    tool = data.get("tool_name")
    tool_input = data.get("tool_input") or {}
    cwd = data.get("cwd") or os.getcwd()

    if tool == "Bash":
        cmd = tool_input.get("command") or ""
        m = OVERRIDE_RE.match(cmd)
        if m:
            reason = m.group(2) or m.group(3) or m.group(4) or ""
            log_event(agent="main", tool=tool, decision="override",
                      cmd_len=len(cmd), override_reason=reason[:120])
            return
        rule = classify_bash(cmd, policy, cwd)
        if rule:
            log_event(agent="main", tool=tool, decision="deny", rule=rule, cmd_len=len(cmd))
            deny(rule)
            return
        log_event(agent="main", tool=tool, decision="allow", cmd_len=len(cmd))
    elif tool == "Read":
        rule = classify_read(tool_input, policy, cwd)
        if rule:
            log_event(agent="main", tool=tool, decision="deny", rule=rule)
            deny(rule)
            return
        log_event(agent="main", tool=tool, decision="allow")
    elif tool == "Agent":
        # context-worker must run as a regular subagent — a `name` spawns a teammate
        if rule_on(policy, "worker_teammate_spawn") \
                and is_context_worker(tool_input.get("subagent_type")) \
                and tool_input.get("name"):
            log_event(agent="main", tool=tool, decision="deny",
                      rule="worker-teammate-spawn")
            deny_reason(
                "DELEGATE_SUBAGENT_REQUIRED:\n"
                "context-worker must run as a regular subagent.\n"
                "Remove the `name` parameter and retry."
            )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)  # fail-open
