"""CULVERT — SessionStart (startup|clear|compact) role re-injection.

Restores the coordinator framing so the primary thread does not silently become
a solo executor again after compaction. Enforcement itself lives in the
PreToolUse gate, so this injection stays short (~10 fixed lines).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _governor import load_policy, log_event

CONTEXT = """CULVERT ACTIVE (Context Unbounded-Load Validation, Execution Routing & Triage).
You are the Primary coordinator: understand intent, decompose problems, judge,
review worker results, and synthesize. Execution-heavy work (DB queries, pytest,
recursive search, heredocs, large file reads) belongs to the context-worker
subagent — the PreToolUse gate will DENY it on the main thread.
When you see DELEGATE_REQUIRED, do not bypass it: delegate to context-worker
(Agent tool, subagent_type: "culvert:context-worker") and work from its
compact RESULT artifact. Do not ask workers for raw logs."""


def main():
    data = json.load(sys.stdin)
    policy = load_policy()
    if not policy.get("enabled"):
        return
    if data.get("agent_id"):
        return  # do not inject into subagent session starts
    source = data.get("source") or ""
    log_event(agent="main", tool="SessionStart", decision="inject", rule=source)
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": CONTEXT,
        }
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)  # fail-open
