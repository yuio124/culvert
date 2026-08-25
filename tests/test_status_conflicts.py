#!/usr/bin/env python3
"""Acceptance tests for /culvert:status conflict detection (A~H).

Each case builds a fake HOME + project so no real user state is touched, then
runs status.py with HOME/CLAUDE_PROJECT_DIR/CULVERT_DIR pointed at the fixture.
Usage: python3 tests/test_status_conflicts.py
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
STATUS = os.path.join(PLUGIN, "scripts", "status.py")

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def build(home, project, user_enabled=None, proj_enabled=None, local_enabled=None,
          registry=None, worker_md=False, legacy_dir=False):
    os.makedirs(os.path.join(home, ".claude", "plugins"), exist_ok=True)
    os.makedirs(os.path.join(project, ".claude"), exist_ok=True)
    if user_enabled is not None:
        json.dump({"enabledPlugins": user_enabled},
                  open(os.path.join(home, ".claude", "settings.json"), "w"))
    if proj_enabled is not None:
        json.dump({"enabledPlugins": proj_enabled},
                  open(os.path.join(project, ".claude", "settings.json"), "w"))
    if local_enabled is not None:
        json.dump({"enabledPlugins": local_enabled},
                  open(os.path.join(project, ".claude", "settings.local.json"), "w"))
    if registry is not None:
        with open(os.path.join(home, ".claude", "plugins", "installed_plugins.json"), "w") as f:
            f.write(registry if isinstance(registry, str) else json.dumps({"plugins": registry}))
    if worker_md:
        os.makedirs(os.path.join(project, ".claude", "agents"), exist_ok=True)
        open(os.path.join(project, ".claude", "agents", "context-worker.md"), "w").write("---\nname: context-worker\n---\n")
    if legacy_dir:
        os.makedirs(os.path.join(project, ".claude", "governor-plugin"), exist_ok=True)
        open(os.path.join(project, ".claude", "governor-plugin", "events.jsonl"), "w").write("{}\n")


def run_status(home, project, gdir):
    env = dict(os.environ, HOME=home, CLAUDE_PROJECT_DIR=project, CULVERT_DIR=gdir)
    env.pop("CULVERT_POLICY", None)
    r = subprocess.run([sys.executable, STATUS], capture_output=True, text=True, env=env, timeout=10)
    return r.stdout, r.returncode


def main():
    base = tempfile.mkdtemp(prefix="culvert-status-")

    def room(tag, **kw):
        home = os.path.join(base, tag, "home")
        proj = os.path.join(base, tag, "proj")
        gdir = os.path.join(base, tag, "state")
        os.makedirs(gdir, exist_ok=True)
        build(home, proj, **kw)
        return home, proj, gdir

    # A. CULVERT alone -> no conflict
    h, p, g = room("a", proj_enabled={"culvert@culvert": True}, registry={})
    out, rc = run_status(h, p, g)
    check("A1 no conflict line", "Conflicting installs: none" in out)
    check("A2 exit 0", rc == 0)

    # B. predecessor enabled=true -> WARNING
    h, p, g = room("b", proj_enabled={"culvert@culvert": True, "context-governor@somewhere": True}, registry={})
    out, _ = run_status(h, p, g)
    check("B1 WARNING", "WARNING context-governor@somewhere is enabled" in out)
    check("B2 tail line", "may not be the hook" in out)

    # C. predecessor enabled=false -> no WARNING
    h, p, g = room("c", proj_enabled={"culvert@culvert": True, "context-governor@somewhere": False}, registry={})
    out, _ = run_status(h, p, g)
    check("C1 no WARNING", "WARNING" not in out)

    # D. installed for another project only -> NOTE, no WARNING
    h, p, g = room("d", proj_enabled={"culvert@culvert": True},
                   registry={"context-governor@somewhere": [
                       {"scope": "project", "projectPath": "/elsewhere", "installPath": "/nonexistent"}]})
    out, _ = run_status(h, p, g)
    check("D1 no WARNING", "WARNING" not in out)
    check("D2 NOTE other project", "installed for 1 other project(s)" in out)

    # E. detection across user / project / local scopes
    h, p, g = room("e1", user_enabled={"context-governor@u": True}, proj_enabled={}, registry={})
    out, _ = run_status(h, p, g)
    check("E1 user scope", "WARNING context-governor@u is enabled" in out)
    h, p, g = room("e2", proj_enabled={"context-governor@p": True}, registry={})
    out, _ = run_status(h, p, g)
    check("E2 project scope", "WARNING context-governor@p is enabled" in out)
    h, p, g = room("e3", local_enabled={"context-governor@l": True}, registry={})
    out, _ = run_status(h, p, g)
    check("E3 local scope", "WARNING context-governor@l is enabled" in out)
    h, p, g = room("e4", user_enabled={"context-governor@u": True},
                   local_enabled={"context-governor@u": False}, registry={})
    out, _ = run_status(h, p, g)
    check("E4 local False overrides user True", "WARNING" not in out)

    # F. project-level context-worker.md -> NOTE
    h, p, g = room("f", proj_enabled={}, registry={}, worker_md=True)
    out, _ = run_status(h, p, g)
    check("F1 worker.md NOTE", "context-worker.md exists" in out)
    check("F2 no WARNING for residue alone", "WARNING" not in out)

    # G. registry missing / broken -> status still works, single NOTE
    h, p, g = room("g1", proj_enabled={"culvert@culvert": True})  # no registry file
    out, rc = run_status(h, p, g)
    check("G1 missing registry ok", rc == 0 and "plugin registry unavailable" in out)
    h, p, g = room("g2", proj_enabled={"culvert@culvert": True}, registry="{ broken json !!")
    out, rc = run_status(h, p, g)
    check("G2 broken registry ok", rc == 0 and "plugin registry unavailable" in out)
    check("G3 no WARNING spam", "WARNING" not in out)

    # H. status leaves events.jsonl byte-identical
    h, p, g = room("h", proj_enabled={"context-governor@x": True}, registry={}, legacy_dir=True)
    log = os.path.join(g, "events.jsonl")
    open(log, "w").write('{"seed": 1}\n')
    before = os.path.getsize(log)
    run_status(h, p, g)
    check("H1 events.jsonl unchanged", os.path.getsize(log) == before)
    # legacy state dir NOTE (from the H fixture's project)
    out, _ = run_status(h, p, g)
    check("H2 legacy state dir NOTE", "legacy state dir" in out)

    print(f"\n{'FAIL ' + str(len(FAILS)) if FAILS else 'ALL PASS'}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
