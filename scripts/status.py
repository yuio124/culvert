#!/usr/bin/env python3
"""CULVERT status report — strictly read-only.

This script never writes any file and never appends to events.jsonl (a status
check must not pollute the log) — hence load_policy_ex() instead of
load_policy(): warnings go to the screen, not to the log.

Conflict detection: other plugins that play the same gate/worker role can answer
a blocked command before CULVERT does. Hook execution order across plugins is
not guaranteed by the harness, so findings are reported as *potential* conflicts
— nothing is disabled or deleted automatically. The plugin registry
(~/.claude/plugins/installed_plugins.json) is an internal store and is treated
as best-effort: if it is missing or unreadable, registry-based checks are
skipped with a NOTE and everything else still runs.
"""
import glob
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _culvert import PLUGIN_ROOT, load_policy_ex, state_dir

RECENT_N = 200
OWN_NAME = "culvert"
#: the only predecessor CULVERT knows by name; everything else is judged structurally
KNOWN_PREDECESSORS = ("context-governor",)
#: gate/validator/restore script names that indicate a same-role plugin
ROLE_SCRIPTS = ("context_gate.py", "validate_worker_result.py",
                "culvert_restore.py", "governor_restore.py")
LEGACY_STATE_DIRNAME = os.path.join(".claude", "governor-plugin")


def read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _json_or_none(path):
    raw = read(path)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
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


def merged_enabled_plugins():
    """enabledPlugins merged across scopes: user < project < local (later wins)."""
    merged = {}
    for path in (os.path.expanduser("~/.claude/settings.json"),
                 os.path.join(project_root(), ".claude", "settings.json"),
                 os.path.join(project_root(), ".claude", "settings.local.json")):
        d = _json_or_none(path)
        if isinstance(d, dict) and isinstance(d.get("enabledPlugins"), dict):
            merged.update(d["enabledPlugins"])
    return merged


def plugin_registry():
    """Best-effort read of the internal install registry.

    Returns (entries, available). entries: [{key, name, scope, projectPath,
    installPath}]. Any missing file, broken JSON, or unexpected schema returns
    ([], False) — callers must degrade gracefully, never fail or over-warn.
    """
    d = _json_or_none(os.path.expanduser("~/.claude/plugins/installed_plugins.json"))
    if not isinstance(d, dict):
        return [], False
    plugins = d.get("plugins", d)
    if not isinstance(plugins, dict):
        return [], False
    entries = []
    try:
        for key, v in plugins.items():
            for e in (v if isinstance(v, list) else [v]):
                if isinstance(e, dict):
                    entries.append({
                        "key": key,
                        "name": key.split("@")[0],
                        "scope": e.get("scope"),
                        "projectPath": e.get("projectPath"),
                        "installPath": e.get("installPath"),
                    })
    except Exception:
        return [], False
    return entries, True


def _same_role_install(install_path):
    """Structural check: does this plugin ship a context-worker or our gate scripts?"""
    if not install_path or not os.path.isdir(install_path):
        return False
    if os.path.isfile(os.path.join(install_path, "agents", "context-worker.md")):
        return True
    hooks = read(os.path.join(install_path, "hooks", "hooks.json")) or ""
    return any(s in hooks for s in ROLE_SCRIPTS)


def settings_hook_wiring():
    """Predecessor-style hooks wired directly in project settings (double gating)."""
    hits = []
    for name in ("settings.json", "settings.local.json"):
        raw = read(os.path.join(project_root(), ".claude", name))
        if not raw:
            continue
        for s in ROLE_SCRIPTS:
            if s in raw:
                hits.append(f"{name}:{s}")
    return hits


def conflict_findings():
    """Returns (warnings, notes) — read-only, no execution-order guesses."""
    warnings, notes = [], []
    enabled = merged_enabled_plugins()
    registry, registry_ok = plugin_registry()

    for key, on in sorted(enabled.items()):
        name = key.split("@")[0]
        if not on or name == OWN_NAME:
            continue
        if name in KNOWN_PREDECESSORS:
            warnings.append(f"{key} is enabled for this project (predecessor of CULVERT)")
        elif registry_ok:
            paths = [e["installPath"] for e in registry if e["key"] == key]
            if any(_same_role_install(p) for p in paths):
                warnings.append(f"{key} is enabled and provides a same-role gate/worker")

    if registry_ok:
        proj = project_root()
        enabled_true = {k for k, v in enabled.items() if v}
        other = {}
        for e in registry:
            if e["name"] in KNOWN_PREDECESSORS and e["key"] not in enabled_true \
                    and e["projectPath"] != proj:
                other[e["key"]] = other.get(e["key"], 0) + 1
        for key, n in sorted(other.items()):
            notes.append(f"{key} installed for {n} other project(s) (not this one)")
    else:
        notes.append("plugin registry unavailable — registry-based checks skipped")

    for hit in settings_hook_wiring():
        warnings.append(f"predecessor-style hooks wired directly in project settings ({hit})")

    if os.path.isfile(os.path.join(project_root(), ".claude", "agents", "context-worker.md")):
        notes.append("project-level .claude/agents/context-worker.md exists — "
                     "un-prefixed 'context-worker' calls resolve there, not to culvert:context-worker")

    legacy_dir = os.path.join(project_root(), LEGACY_STATE_DIRNAME)
    if os.path.isdir(legacy_dir):
        log = os.path.join(legacy_dir, "events.jsonl")
        try:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(log)))
            notes.append(f"legacy state dir {LEGACY_STATE_DIRNAME}/ exists (events last written {when})")
        except OSError:
            notes.append(f"legacy state dir {LEGACY_STATE_DIRNAME}/ exists")

    for pj in glob.glob(os.path.join(project_root(), ".claude", "skills", "*",
                                     ".claude-plugin", "plugin.json")):
        d = _json_or_none(pj)
        if isinstance(d, dict) and d.get("name") in (OWN_NAME,) + KNOWN_PREDECESSORS:
            notes.append(f"shadow skills-dir copy: {os.path.dirname(os.path.dirname(pj))}")

    return warnings, notes


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
    recent, total_lines = recent_decisions(log_path)
    conf_warn, conf_note = conflict_findings()

    print("CULVERT status (read-only)")
    print(f"  CULVERT enabled   : {policy.get('enabled')}")
    print(f"  Plugin version    : {plugin_version()}")
    print("  Worker type       : culvert:context-worker")
    print(f"  Worker model      : {worker_model()}")
    print(f"  Policy source     : {source}")
    print(f"  Event log         : {log_path} "
          f"({'absent' if recent is None else str(total_lines) + ' lines'})")
    if not conf_warn and not conf_note:
        print("  Conflicting installs: none")
    else:
        print("  Conflicting installs:")
        for w in conf_warn:
            print(f"    WARNING {w}")
        for n in conf_note:
            print(f"    NOTE    {n}")
        if conf_warn:
            print("    CULVERT may not be the hook that handles a blocked command first.")
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
