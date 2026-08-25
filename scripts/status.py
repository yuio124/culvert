#!/usr/bin/env python3
"""Context Governor status report — strictly read-only.

This script never writes any file and never appends to events.jsonl (a status
check must not pollute the log) — hence load_policy_ex() instead of
load_policy(): warnings go to the screen, not to the log.
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
    return m.group(1) if m else "?(no model in frontmatter — inherits session model)"


def project_root():
    return os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


def legacy_hook_wiring():
    """Is a project-level copy of the governor hooks still wired in settings?"""
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

    print("Context Governor status (read-only)")
    print(f"  Governor enabled  : {policy.get('enabled')}")
    print(f"  Plugin version    : {plugin_version()}")
    print("  Worker type       : context-governor:context-worker")
    if proj_worker:
        print("                      WARNING: a project-level .claude/agents/context-worker.md "
              "also exists — un-prefixed 'context-worker' calls go to that copy")
    print(f"  Worker model      : {worker_model()}")
    print(f"  Policy source     : {source}")
    print(f"  Event log         : {log_path} "
          f"({'absent' if recent is None else str(total_lines) + ' lines'})")
    if dup:
        print(f"  Duplicate hooks   : WARNING — governor hooks also wired in project settings "
              f"(double gating): {', '.join(dup)}")
    else:
        print("  Duplicate hooks   : none")
    if recent is None:
        print("  Recent decisions  : (no event log yet — hooks have not fired in this project)")
    else:
        counts, deny_rules = recent
        top = " / ".join(f"{r} {n}" for r, n in
                         sorted(deny_rules.items(), key=lambda x: -x[1])[:3])
        summary = " / ".join(f"{k} {v}" for k, v in sorted(counts.items()))
        print(f"  Recent decisions  : last {min(total_lines, RECENT_N)} — {summary or 'none'}"
              + (f" (top deny rules: {top})" if top else ""))
    for rule, detail in warnings:
        print(f"  Policy warning    : {rule}: {detail}")


if __name__ == "__main__":
    main()
