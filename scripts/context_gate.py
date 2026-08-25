"""Context Governor — PreToolUse gate (Bash|Read|Agent). v0.2.2 (RC2).

판정 기준(명문화): "computationally heavy" 가 아니라
**「Primary context 에 큰 tool argument / tool result 를 남길 위험」**이다.

평가 순서 (classify_bash):
  ① 인자 크기 규칙 — heredoc · long-inline-python · long-command. 출력을 캡해도
     인자 자체가 컨텍스트에 실리므로 bounded 면제 대상이 아니다.
  ② test-run — Python(pytest/unittest) + Node(npm/pnpm/yarn test·node --test·npx jest/vitest).
     보수적으로 bounded 면제 비적용(스위트 출력·소요는 정적 예측 불가). Go/Cargo/.NET 등은
     아직 범위 밖 — README 에 명시.
  ③ bounded-output 면제 — 세그먼트(&&·;·개행 분리)마다 최종 출력이 정적으로 작음이
     명백하면(head/tail ≤100줄·head -c ≤10KB·wc·비재귀 grep -c·scalar 집계 sqlite) 그
     세그먼트는 출력 규칙을 건너뛴다.
  ④ 출력 크기 규칙 — db-query · recursive-search · fan-out-read · big-file-dump.
     fan-out(for…cat·cat <glob>·find -exec cat·xargs cat)은 전체 명령 기준으로도 본다.

스크립트 오류는 fail-open(exit 0). worker 이름 매칭은 _governor.is_context_worker.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _governor import is_context_worker, load_policy, log_event, rule_on

OVERRIDE_RE = re.compile(r'^\s*GOVERNOR_OVERRIDE=("([^"]+)"|\'([^\']+)\'|(\S+))\s+')

# ① 인자 크기
HEREDOC_RE = re.compile(r"(?<!<)<<(?!<)-?\s*['\"]?[A-Za-z_]")
INLINE_PY_RE = re.compile(r"\bpython3?\b[^\n|;&]{0,120}\s-c\b|\buv\s+run\b[^\n|;&]{0,120}\s-c\b")

# ② 테스트 러너 (Python + Node — 지원 범위는 이 둘뿐)
TEST_RE = re.compile(
    r"(?:^|[\s;&|])(?:\S*/)?pytest(?=\s|$)"
    r"|\bpython3?\s+-m\s+(pytest|unittest)\b"
    r"|(?:^|[\s;&|])(?:npm|pnpm|yarn)\s+(?:run\s+)?test\b"
    r"|\bnode\s+--test\b"
    r"|\bnpx\s+(?:jest|vitest)\b"
)

# ④ 출력 크기
SQLITE_RE = re.compile(r"\bsqlite3\b")
DB_FILE_RE = re.compile(r"\S+\.db\b")
DB_QUERYISH_RE = re.compile(r"\b(select|SELECT|insert|INSERT|connect|cursor|PRAGMA|pragma)\b")
GREP_R_RE = re.compile(r"\bz?grep\b[^\n|;&]*\s-[A-Za-z]*[rR]")
RG_RE = re.compile(r"\brg\b")
FIND_RE = re.compile(r"\bfind\b\s+(\S+)")
CAT_RE = re.compile(r"^\s*cat\s+((?:-\S+\s+)*)([^|;&<>]+?)\s*$")
# fan-out read — 여러 파일 내용을 Primary 로 쏟는 셸 패턴
FANOUT_FOR_CAT_RE = re.compile(r"\bfor\b[^\n]*?\bdo\b[^\n]*?\bcat\b", re.S)
FANOUT_FIND_EXEC_RE = re.compile(r"\bfind\b[^\n]*?-exec\s+cat\b")
FANOUT_XARGS_RE = re.compile(r"\bxargs\s+(?:-\S+\s+)*cat\b")
FANOUT_CAT_GLOB_RE = re.compile(r"(?:^|[;&|]\s*)cat\s+(?:-\S+\s+)*[^|;&<>]*[*?]")

# ③ bounded-output — 정적으로 출력이 작음이 명백한 꼴만
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
    """모든 SELECT 목록이 집계함수뿐이고 GROUP BY 가 없으면 scalar — 정적 확신이 없으면 False."""
    if not SQLITE_RE.search(seg):
        return False
    low = seg.lower()
    if "group by" in low:
        return False
    lists = SQL_SELECT_LIST_RE.findall(low)
    if not lists:
        return False  # SELECT 목록을 못 읽으면 보수적으로 비면제
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
    """세그먼트 하나에 대한 출력 크기 규칙 판정."""
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
    """히트한 규칙명을 돌려준다. 없으면 None. 순서는 모듈 docstring 참조."""
    # ① 인자 크기 — bounded 면제 불가
    if rule_on(policy, "heredoc") and HEREDOC_RE.search(cmd):
        return "heredoc"
    if rule_on(policy, "long_inline_python") and INLINE_PY_RE.search(cmd) \
            and len(cmd) > policy["max_inline_code_length"]:
        return "long-inline-python"
    if rule_on(policy, "long_command") and len(cmd) > policy["max_command_length"]:
        return "long-command"
    # ② 테스트 러너 — bounded 면제 불가 (보수)
    if rule_on(policy, "test_run") and TEST_RE.search(cmd):
        return "test-run"
    # ④' fan-out 중 세그먼트를 가로지르는 꼴은 전체 명령 기준으로 먼저 본다
    if rule_on(policy, "fan_out_read") and (
            FANOUT_FOR_CAT_RE.search(cmd) or FANOUT_FIND_EXEC_RE.search(cmd)
            or FANOUT_XARGS_RE.search(cmd)):
        return "fan-out-read"
    # ③+④ 세그먼트별: bounded 면 출력 규칙 면제
    exempt = rule_on(policy, "bounded_output_exemption")
    for seg in SEG_SPLIT_RE.split(cmd):
        if not seg.strip():
            continue
        if exempt and _bounded(seg):
            continue
        rule = _output_rule(seg, policy, cwd)
        if rule:
            return rule
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
        "Delegate it to context-worker (Agent tool, subagent_type: \"context-governor:context-worker\") "
        "and synthesize from its compact RESULT artifact. "
        "If this is a false positive that the Primary must run directly, prefix the command with "
        "GOVERNOR_OVERRIDE=\"reason\" (the override is logged)."
    )


def main():
    data = json.load(sys.stdin)
    policy = load_policy()
    if not policy.get("enabled"):
        return  # OFF 베이스라인: 판정도 로그도 없음
    if data.get("agent_id"):
        return  # subagent 면제
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
        # context-worker 는 반드시 일반 subagent 로 — name 이 있으면 teammate 로 스폰된다
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
