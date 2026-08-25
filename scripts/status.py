#!/usr/bin/env python3
"""Context Governor 상태 보고 — 읽기 전용.

철칙: 이 스크립트는 어떤 파일도 쓰지 않는다. events.jsonl 에 기록하지 않는다
(상태 확인이 로그를 오염시키면 안 된다) — 그래서 load_policy() 가 아니라
load_policy_ex() 를 쓴다(경고를 로그 대신 화면으로).
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _governor import PLUGIN_ROOT, load_policy_ex, state_dir

RECENT_N = 200
LEGACY_SCRIPTS = ("context_gate.py", "validate_worker_result.py", "governor_restore.py")


def read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def plugin_version():
    raw = read(os.path.join(PLUGIN_ROOT, ".claude-plugin", "plugin.json"))
    try:
        return json.loads(raw)["version"]
    except Exception:
        return "?"


def worker_model():
    raw = read(os.path.join(PLUGIN_ROOT, "agents", "context-worker.md")) or ""
    m = re.search(r"^model:\s*(\S+)", raw, re.M)
    return m.group(1) if m else "?(frontmatter 에 model 없음 — 세션 모델 상속)"


def project_root():
    return os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


def legacy_hook_wiring():
    """프로젝트 settings 에 legacy governor hook 배선이 남아 있나."""
    hits = []
    for name in ("settings.json", "settings.local.json"):
        raw = read(os.path.join(project_root(), ".claude", name))
        if not raw:
            continue
        for s in LEGACY_SCRIPTS:
            if s in raw:
                hits.append(f"{name}:{s}")
    return hits


def recent_decisions(log_path):
    raw = read(log_path)
    if raw is None:
        return None, 0
    lines = raw.strip().splitlines()
    counts, deny_rules = {}, {}
    for ln in lines[-RECENT_N:]:
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        d = rec.get("decision", "?")
        counts[d] = counts.get(d, 0) + 1
        if d == "deny" and rec.get("rule"):
            deny_rules[rec["rule"]] = deny_rules.get(rec["rule"], 0) + 1
    return (counts, deny_rules), len(lines)


def main():
    policy, source, warnings = load_policy_ex()
    log_path = os.path.join(state_dir(), "events.jsonl")
    dup = legacy_hook_wiring()
    recent, total_lines = recent_decisions(log_path)
    proj_worker = os.path.isfile(
        os.path.join(project_root(), ".claude", "agents", "context-worker.md"))

    print("Context Governor status (읽기 전용)")
    print(f"  Governor enabled  : {policy.get('enabled')}")
    print(f"  Plugin version    : {plugin_version()}")
    line = "  Worker type       : context-governor:context-worker"
    print(line)
    if proj_worker:
        print("                      ⚠ 프로젝트 .claude/agents/context-worker.md 도 존재 — "
              "무접두 'context-worker' 호출은 그쪽(legacy)으로 간다")
    print(f"  Worker model      : {worker_model()}")
    print(f"  Policy source     : {source}")
    print(f"  Event log         : {log_path} "
          f"({'없음' if recent is None else str(total_lines) + '줄'})")
    if dup:
        print(f"  Legacy hook 중복  : ⚠ 배선 잔존 → gate 이중 실행 위험: {', '.join(dup)}")
    else:
        print("  Legacy hook 중복  : 없음")
    if recent is None:
        print("  Recent decisions  : (이벤트 로그 없음 — 이 프로젝트에서 아직 hook 이 안 돌았다)")
    else:
        counts, deny_rules = recent
        top = " · ".join(f"{r} {n}" for r, n in
                         sorted(deny_rules.items(), key=lambda x: -x[1])[:3])
        summary = " · ".join(f"{k} {v}" for k, v in sorted(counts.items()))
        print(f"  Recent decisions  : 최근 {min(total_lines, RECENT_N)}건 — {summary or '없음'}"
              + (f" (deny 상위: {top})" if top else ""))
    for rule, detail in warnings:
        print(f"  ⚠ policy 경고     : {rule}: {detail}")


if __name__ == "__main__":
    main()
