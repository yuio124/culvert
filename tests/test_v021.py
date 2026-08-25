#!/usr/bin/env python3
"""Acceptance tests A~G — policy override merge, fail-safe behavior, and the
read-only status script.

Standalone (no pytest): python3 tests/test_v021.py
Every case redirects CULVERT_DIR to a temp directory so real logs stay clean.
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
GATE = os.path.join(PLUGIN, "scripts", "context_gate.py")
STATUS = os.path.join(PLUGIN, "scripts", "status.py")
sys.path.insert(0, os.path.join(PLUGIN, "scripts"))

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def run_gate(payload, gdir):
    env = dict(os.environ, CULVERT_DIR=gdir)
    env.pop("CULVERT_POLICY", None)
    r = subprocess.run([sys.executable, GATE], input=json.dumps(payload),
                       capture_output=True, text=True, env=env, timeout=10)
    return r.stdout.strip()


def load_ex(gdir):
    env = dict(os.environ, CULVERT_DIR=gdir)
    env.pop("CULVERT_POLICY", None)
    code = ("import sys, json; sys.path.insert(0, %r); from _culvert import load_policy_ex as L; "
            "p, s, w = L(); print(json.dumps({'p': p, 's': s, 'w': w}))"
            % os.path.join(PLUGIN, "scripts"))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    return json.loads(r.stdout)


LONG = {"tool_name": "Bash", "tool_input": {"command": "echo " + "z" * 600}}
DEFAULT = json.load(open(os.path.join(PLUGIN, "config", "policy.json")))


def main():
    base = tempfile.mkdtemp(prefix="gov-v021-")

    # A. no override -> identical to defaults, 600-char command denied
    d = os.path.join(base, "a"); os.makedirs(d)
    r = load_ex(d)
    check("A1 effective == default", {k: r["p"][k] for k in DEFAULT} == DEFAULT)
    check("A2 source == default", r["s"] == "default", r["s"])
    check("A3 no warnings", r["w"] == [])
    check("A4 600 chars -> deny:long-command", "long-command" in run_gate(LONG, d))

    # B. valid override -> only the given keys change
    d = os.path.join(base, "b"); os.makedirs(d)
    json.dump({"max_command_length": 900, "rules": {"test_run": False}},
              open(os.path.join(d, "policy.json"), "w"))
    r = load_ex(d)
    check("B1 max_command_length=900", r["p"]["max_command_length"] == 900)
    check("B2 other thresholds stay default", r["p"]["max_read_bytes"] == DEFAULT["max_read_bytes"])
    check("B3 rules.test_run=False, others kept",
          r["p"]["rules"]["test_run"] is False and r["p"]["rules"]["heredoc"] is True)
    check("B4 600 chars -> allow (limit 900)", run_gate(LONG, d) == "")
    check("B5 pytest -> allow (rule disabled)",
          run_gate({"tool_name": "Bash", "tool_input": {"command": "pytest -q"}}, d) == "")
    check("B6 sqlite3 -> still deny",
          "db-query" in run_gate({"tool_name": "Bash", "tool_input": {"command": "sqlite3 x.db 'SELECT 1'"}}, d))

    # C. broken override -> defaults keep working
    d = os.path.join(base, "c"); os.makedirs(d)
    open(os.path.join(d, "policy.json"), "w").write("{ not json !!")
    r = load_ex(d)
    check("C1 defaults effective", r["p"]["max_command_length"] == DEFAULT["max_command_length"])
    check("C2 broken warning", any(w[0] == "broken-policy-override" for w in r["w"]))
    check("C3 600 chars -> deny (default)", "long-command" in run_gate(LONG, d))

    # D. wrong type -> only that key ignored
    d = os.path.join(base, "d"); os.makedirs(d)
    json.dump({"max_command_length": "big", "max_result_bytes": 4096},
              open(os.path.join(d, "policy.json"), "w"))
    r = load_ex(d)
    check("D1 bad key keeps default", r["p"]["max_command_length"] == DEFAULT["max_command_length"])
    check("D2 good key applied", r["p"]["max_result_bytes"] == 4096)
    check("D3 type warning", any(w[0] == "bad-policy-type" for w in r["w"]))

    # E. unknown key -> warning surfaced (one warn event via the hook path)
    d = os.path.join(base, "e"); os.makedirs(d)
    json.dump({"max_foo": 1, "rules": {"no_such_rule": True}},
              open(os.path.join(d, "policy.json"), "w"))
    r = load_ex(d)
    check("E1 unknown key warning", any(w[0] == "unknown-policy-key" for w in r["w"]))
    check("E2 unknown rule warning", any(w[0] == "unknown-policy-rule" for w in r["w"]))
    run_gate({"tool_name": "Bash", "tool_input": {"command": "ls"}}, d)  # hook path -> writes warn
    log = open(os.path.join(d, "events.jsonl")).read()
    check("E3 warn in events.jsonl", '"decision": "warn"' in log and "unknown-policy-key" in log)
    n1 = log.count('"decision": "warn"')
    run_gate({"tool_name": "Bash", "tool_input": {"command": "ls"}}, d)  # second run -> deduplicated
    n2 = open(os.path.join(d, "events.jsonl")).read().count('"decision": "warn"')
    check("E4 warn deduplicated (once)", n1 == n2, f"{n1}->{n2}")

    # F/G. status — read-only, all report fields present
    d = os.path.join(base, "e")  # reuse the directory that has a log
    before = os.path.getsize(os.path.join(d, "events.jsonl"))
    env = dict(os.environ, CULVERT_DIR=d); env.pop("CULVERT_POLICY", None)
    out = subprocess.run([sys.executable, STATUS], capture_output=True, text=True, env=env).stdout
    after = os.path.getsize(os.path.join(d, "events.jsonl"))
    check("F1 status leaves the log unchanged", before == after)
    for label in ("CULVERT enabled", "Plugin version", "Worker type", "Worker model",
                  "Policy source", "Event log", "Duplicate hooks", "Recent decisions"):
        check(f"G {label}", label in out)
    check("G warning surfaced", "unknown-policy-key" in out)

    print(f"\n{'FAIL ' + str(len(FAILS)) if FAILS else 'ALL PASS'}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
