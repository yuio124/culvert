"""Context Governor plugin — SessionStart(startup|clear|compact) 역할 재주입. v0.1.1 이식판.

컴팩션 후 Primary 가 다시 혼자 실행자가 되는 것을 막는 프레이밍 복원.
enforcement 자체는 PreToolUse gate 가 담당하므로 이 주입은 짧아야 한다(~10줄 고정).
주입 문구는 .claude/hooks/governor_restore.py 와 동일(이식 원칙).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _governor import load_policy, log_event

CONTEXT = """CONTEXT GOVERNOR ACTIVE.
You are the Primary coordinator: understand intent, decompose problems, judge,
review worker results, and synthesize. Execution-heavy work (DB queries, pytest,
recursive search, heredocs, large file reads) belongs to the context-worker
subagent — the PreToolUse gate will DENY it on the main thread.
When you see DELEGATE_REQUIRED, do not bypass it: delegate to context-worker
(Agent tool, subagent_type: "context-governor:context-worker") and work from its
compact RESULT artifact. Do not ask workers for raw logs."""


def main():
    data = json.load(sys.stdin)
    policy = load_policy()
    if not policy.get("enabled"):
        return
    if data.get("agent_id"):
        return  # subagent 의 세션 시작에는 주입하지 않는다
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
