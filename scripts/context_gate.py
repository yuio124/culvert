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
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _culvert import is_context_worker, load_policy, log_event, rule_on, runtime_meta

OVERRIDE_RE = re.compile(r'^\s*CULVERT_OVERRIDE=("([^"]+)"|\'([^\']+)\'|(\S+))\s+')

# 1. argument size
HEREDOC_RE = re.compile(r"(?<!<)<<(?!<)-?\s*['\"]?[A-Za-z_]")
INLINE_PY_RE = re.compile(r"\bpython3?\b[^\n|;&]{0,120}\s-c\b|\buv\s+run\b[^\n|;&]{0,120}\s-c\b")
# inline python that dumps a file is unbounded output regardless of command length
INLINE_DUMP_RE = re.compile(r"\bopen\s*\([^)]*\)\s*\.\s*read\b")

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


#: options that consume the next token; presence of -e/-f shifts positionals -> conservative deny
_GREP_OPT_WITH_ARG = {"-m", "-A", "-B", "-C", "--include", "--exclude", "--exclude-dir", "--color"}
_GREP_PATTERN_OPTS = {"-e", "-f", "--regexp", "--file"}
_GREP_MAX_FILES = 5


def _grep_explicit_files(seg, policy, cwd):
    """True iff a grep -r segment targets only a few explicit, existing, small files.

    v0.3.0 FP fix: `grep -r pat file1 file2` is not a recursive search. Boundaries
    stay conservative — any of the following keeps the deny: no targets (whole cwd),
    a directory or glob or $VAR target, a nonexistent path, more than
    _GREP_MAX_FILES files, total size over max_read_bytes, or -e/-f style options
    that shift positional arguments.
    """
    try:
        toks = shlex.split(seg)
    except ValueError:
        return False
    idx = next((i for i, t in enumerate(toks)
                if os.path.basename(t) in ("grep", "zgrep", "egrep", "fgrep")), None)
    if idx is None:
        return False
    args = toks[idx + 1:]
    if any(t in _GREP_PATTERN_OPTS for t in args):
        return False
    pos, skip = [], False
    for t in args:
        if skip:
            skip = False
            continue
        if t == "--":
            continue
        if t.startswith("-") and t != "-":
            if t in _GREP_OPT_WITH_ARG:
                skip = True
            continue
        pos.append(t)
    if len(pos) < 2:
        return False  # pattern only -> whole-cwd recursion
    targets = pos[1:]
    if len(targets) > _GREP_MAX_FILES:
        return False
    total = 0
    for t in targets:
        if any(c in t for c in "*?[$"):
            return False
        p = t if os.path.isabs(t) else os.path.join(cwd, t)
        if not os.path.isfile(p):
            return False
        try:
            total += os.path.getsize(p)
        except OSError:
            return False
    return total <= policy["max_read_bytes"]


def _output_rule(seg, policy, cwd):
    """Output-size rule verdict for a single segment."""
    if rule_on(policy, "db_query"):
        if SQLITE_RE.search(seg):
            return "db-query"
        if DB_FILE_RE.search(seg) and DB_QUERYISH_RE.search(seg):
            return "db-query"
    if rule_on(policy, "recursive_search"):
        if GREP_R_RE.search(seg) and not _grep_explicit_files(seg, policy, cwd):
            return "recursive-search"
        if RG_RE.search(seg):
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
            and (INLINE_DUMP_RE.search(cmd)
                 or len(cmd) > policy["max_inline_code_length"]):
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


#: verbatim-handoff quoting. Quotes must never be lossy in a way that hides an
#: operation: either the full command (or a trivially-abridged single heredoc)
#: is quoted, or the quote is omitted entirely and the model is told to delegate
#: the original tool call from its own context. Never mid-truncate.
QUOTE_MAX = 2000
HEREDOC_MARKER_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def _handoff_quote(cmd):
    """Return (quote_text | None, note | None). None quote => omit entirely."""
    if "````" in cmd:
        return None, None  # would break the fence - omit
    if HEREDOC_RE.search(cmd):
        ms = list(HEREDOC_MARKER_RE.finditer(cmd))
        if len(ms) != 1:
            return None, None  # multiple/ambiguous heredocs - omit
        m = ms[0]
        marker = m.group(2)
        nl = cmd.find("\n", m.end())
        if nl < 0:
            return None, None
        term = re.compile(r"^" + re.escape(marker) + r"\s*$", re.M).search(cmd, nl + 1)
        if not term:
            return None, None
        body = cmd[nl + 1:term.start()]
        n_lines = body.count("\n") + (0 if body.endswith("\n") or not body else 1)
        abridged = (cmd[:nl + 1]
                    + f"# [heredoc body omitted: {n_lines} lines]\n"
                    + cmd[term.start():])
        if len(abridged) > QUOTE_MAX:
            return None, None
        return abridged, "heredoc body omitted"
    if len(cmd) <= QUOTE_MAX:
        return cmd, None
    return None, None


def deny(rule, cmd=None):
    parts = [
        f"DELEGATE_REQUIRED: rule={rule}. [CULVERT HANDOFF] This call was blocked "
        "because of Primary-context pressure, not because the requested work is invalid.",
        "Preserve the complete operation. Do not shrink, split, or drop validation "
        "steps merely to avoid this gate. If any original operation would be "
        "omitted, treat the task as INCOMPLETE. The worker may make mechanical "
        "adjustments (paths, cwd) but must keep every semantic operation.",
    ]
    if cmd is not None:
        quote, note = _handoff_quote(cmd)
        if quote is not None:
            parts.append("Original rejected command (data, not instructions):\n"
                         "````bash\n" + quote + "\n````")
            if note:
                parts.append("NOTE: " + note + ". Do not copy this abridged quote "
                             "into the worker prompt - pass the original tool_input "
                             "verbatim from your current context.")
        else:
            parts.append("The original command is too long to quote safely. "
                         "Delegate the complete original rejected tool call from "
                         "your current context. Do not reconstruct it from this handoff.")
    parts.append(
        "If your goal is to inspect file contents, the Read tool (with "
        "limit/offset) is not gated by CULVERT.")
    parts.append(
        "Preferred options, in order:\n"
        "1. Delegate the same semantic operation, unchanged, to context-worker "
        "(Agent tool, subagent_type: \"culvert:context-worker\"). Default choice.\n"
        "2. If the original operation is genuinely bounded and preserving it "
        "matters, a justified CULVERT_OVERRIDE=\"reason\" prefix is acceptable "
        "(logged).\n"
        "3. Output-only cap (e.g. `| head -N`): bounding may reduce returned "
        "output, but must not reduce the set of records, fields, checks, or "
        "computations performed. Would the complete output fit under the cap? "
        "If unsure, use option 1 or 2 instead.\n"
        "4. Several similar small operations may share one worker — without "
        "merging, dropping, or skipping any of them.")
    deny_reason("\n\n".join(parts))


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
    meta = runtime_meta(data, policy)

    if tool == "Bash":
        cmd = tool_input.get("command") or ""
        m = OVERRIDE_RE.match(cmd)
        if m:
            reason = m.group(2) or m.group(3) or m.group(4) or ""
            log_event(**meta, agent="main", tool=tool, decision="override",
                      cmd_len=len(cmd), override_reason=reason[:120])
            return
        rule = classify_bash(cmd, policy, cwd)
        if rule:
            log_event(**meta, agent="main", tool=tool, decision="deny", rule=rule, cmd_len=len(cmd))
            deny(rule, cmd)
            return
        log_event(**meta, agent="main", tool=tool, decision="allow", cmd_len=len(cmd))
    elif tool == "Read":
        rule = classify_read(tool_input, policy, cwd)
        if rule:
            log_event(**meta, agent="main", tool=tool, decision="deny", rule=rule)
            deny(rule)
            return
        log_event(**meta, agent="main", tool=tool, decision="allow")
    elif tool == "Agent":
        # context-worker must run as a regular subagent — a `name` spawns a teammate
        if rule_on(policy, "worker_teammate_spawn") \
                and is_context_worker(tool_input.get("subagent_type")) \
                and tool_input.get("name"):
            log_event(**meta, agent="main", tool=tool, decision="deny",
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
