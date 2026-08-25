"""Context Governor — SubagentStop validator (context-worker only).

Keeps the worker from leaking raw logs into the primary context:
- last_assistant_message over max_result_bytes (default 8KB) -> block (ask to rewrite)
- missing RESULT / status: schema -> block (with schema hint)
- stop_hook_active=true -> never block (loop guard) — log the violation and pass
Output schema: {"decision": "block", "reason": "..."}.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _governor import is_context_worker, load_policy, log_event

STATUS_RE = re.compile(r"\bstatus:\s*(SUCCESS|PARTIAL|BLOCKED)\b")

SCHEMA_HINT = (
    "RESULT\n"
    "status: SUCCESS | PARTIAL | BLOCKED\n"
    "conclusion: ...\n"
    "evidence: / measurements: / files_changed: / rejected_hypotheses: / unresolved:\n"
    "raw_artifacts: (file paths only)"
)


def violations(msg, policy):
    v = []
    size = len(msg.encode("utf-8"))
    if size > policy["max_result_bytes"]:
        v.append(f"result is {size} bytes (limit {policy['max_result_bytes']})")
    if "RESULT" not in msg or not STATUS_RE.search(msg):
        v.append("missing RESULT schema (RESULT header + status: line)")
    return v


def main():
    data = json.load(sys.stdin)
    policy = load_policy()
    if not policy.get("enabled"):
        return
    agent_type = data.get("agent_type") or ""
    if not is_context_worker(agent_type):
        return  # do not interfere with other subagents
    msg = data.get("last_assistant_message")
    if msg is None:
        log_event(agent=agent_type, tool="SubagentStop", decision="approve",
                  rule="no-last-message")
        return
    v = violations(msg, policy)
    if not v:
        log_event(agent=agent_type, tool="SubagentStop", decision="approve",
                  result_bytes=len(msg.encode("utf-8")))
        return
    if data.get("stop_hook_active"):
        # already blocked once — avoid loops: pass, but record the violation
        log_event(agent=agent_type, tool="SubagentStop", decision="approve",
                  rule="loop-guard", violation="; ".join(v))
        return
    log_event(agent=agent_type, tool="SubagentStop", decision="block",
              violation="; ".join(v))
    print(json.dumps({
        "decision": "block",
        "reason": (
            "GOVERNOR: your final message violates the worker result contract ("
            + "; ".join(v) + "). Rewrite your FINAL message as a compact artifact "
            "under " + str(policy["max_result_bytes"]) + " bytes, exactly in this schema, "
            "with no raw logs or step-by-step narration:\n" + SCHEMA_HINT
        ),
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)  # fail-open
