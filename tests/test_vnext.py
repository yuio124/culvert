#!/usr/bin/env python3
"""v0.3.0 acceptance — events metadata, verbatim handoff v2, offline analyzer.

Standalone (no pytest): python3 tests/test_vnext.py
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
GATE = os.path.join(PLUGIN, "scripts", "context_gate.py")
ANALYZER = os.path.join(PLUGIN, "tools", "analyze_session.py")
VERSION = json.load(open(os.path.join(PLUGIN, ".claude-plugin", "plugin.json")))["version"]

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
    if not r.stdout.strip():
        return None
    return json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]


def bash(cmd, gdir, extra=None):
    p = {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": gdir}
    p.update(extra or {})
    return run_gate(p, gdir)


def main():
    base = tempfile.mkdtemp(prefix="culvert-vnext-")

    # ---- E. events metadata ----
    g = os.path.join(base, "ev"); os.makedirs(g)
    bash("rg x", g, {"tool_use_id": "toolu_T1", "prompt_id": "p1", "session_id": "s1",
                     "transcript_path": "/tmp/t.jsonl", "permission_mode": "default"})
    line = json.loads(open(os.path.join(g, "events.jsonl")).read().splitlines()[-1])
    check("E1 join fields logged", line.get("tool_use_id") == "toolu_T1" and line.get("prompt_id") == "p1")
    check("E2 loaded version logged", line.get("culvert_version") == VERSION)
    check("E3 policy_hash 12 hex", isinstance(line.get("policy_hash"), str) and len(line["policy_hash"]) == 12)
    bash("rg x", g)  # fields absent -> fail-open
    line2 = json.loads(open(os.path.join(g, "events.jsonl")).read().splitlines()[-1])
    check("E4 fail-open without fields", "tool_use_id" not in line2 and line2["decision"] == "deny")
    # policy_hash reflects effective override
    ov = os.path.join(base, "ev2"); os.makedirs(ov)
    json.dump({"max_command_length": 999}, open(os.path.join(ov, "policy.json"), "w"))
    bash("rg x", ov)
    line3 = json.loads(open(os.path.join(ov, "events.jsonl")).read().splitlines()[-1])
    check("E5 hash changes with override", line3["policy_hash"] != line["policy_hash"])

    # ---- H. handoff v2 ----
    g = os.path.join(base, "h"); os.makedirs(g)
    r = bash("rg pattern", g)
    check("H1 rule parseable", r.startswith("DELEGATE_REQUIRED: rule=recursive-search."))
    check("H2 full verbatim quote", "````bash\nrg pattern\n````" in r)
    check("H3 invariant present", "Do not shrink, split, or drop validation" in r)
    r = bash("rg pat && echo ````raw````", g)
    check("H4 4-backtick cmd -> quote omitted (would break the fence)",
          "too long to quote safely" in r and "raw" not in r)
    r = bash("rg pat && echo ```three```", g)
    check("H4b 3 backticks are safe inside a 4-backtick fence", "```three```" in r)
    hd = "cat > /tmp/x.py <<EOF\n" + "print(1)\n" * 5 + "EOF\nrg after"
    r = bash(hd, g)
    check("H5 single heredoc abridged", "heredoc body omitted: 5 lines" in r and "rg after" in r)
    check("H6 do-not-copy note", "pass the original tool_input verbatim" in r)
    hd2 = "cat <<A\nx\nA\ncat <<B\ny\nB\nrg z"
    r = bash(hd2, g)
    check("H7 multi-heredoc -> omitted", "too long to quote safely" in r and "print(1)" not in r)
    long_cmd = "rg needle " + "&& stat -c %s file-" * 300
    r = bash(long_cmd, g)
    check("H8 long cmd -> omitted, no mid-truncation", "too long to quote safely" in r
          and "stat -c" not in r)
    r = bash("pytest -q", g)
    check("H9 other rules use handoff too", "[CULVERT HANDOFF]" in r and "rule=test-run." in r)

    # ---- A. analyzer smoke ----
    g = os.path.join(base, "an"); os.makedirs(g)
    tpath = os.path.join(g, "transcript.jsonl")
    rows = [
        {"version": "9.9.9-test",
         "message": {"model": "claude-test", "content": [
             {"type": "tool_use", "id": "toolu_D1", "name": "Bash",
              "input": {"command": "grep -rn secret ."}}]}},
        {"message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_D1",
             "content": "DELEGATE_REQUIRED: rule=recursive-search. ..."}]}},
        {"message": {"content": [
            {"type": "tool_use", "id": "toolu_N1", "name": "Bash",
             "input": {"command": "ls -la"}},
            {"type": "tool_use", "id": "toolu_N2", "name": "Agent",
             "input": {"subagent_type": "culvert:context-worker",
                       "prompt": "Run exactly: grep -rn secret . and report counts only " + "x" * 600}}]}},
    ]
    open(tpath, "w").write("\n".join(json.dumps(r) for r in rows))
    epath = os.path.join(g, "events.jsonl")
    evs = [
        {"ts": "t", "tool": "Bash", "decision": "deny", "rule": "recursive-search",
         "tool_use_id": "toolu_D1", "prompt_id": "pX", "transcript_path": tpath,
         "culvert_version": VERSION, "policy_hash": "abcabcabcabc", "cmd_len": 20},
        {"ts": "t", "tool": "Bash", "decision": "allow", "prompt_id": "pX",
         "tool_use_id": "toolu_N1", "transcript_path": tpath, "cmd_len": 6},
        {"ts": "t", "tool": "Bash", "decision": "deny", "rule": "db-query",
         "tool_use_id": "toolu_MISSING", "transcript_path": tpath, "cmd_len": 10},
    ]
    open(epath, "w").write("\n".join(json.dumps(x) for x in evs))
    out = subprocess.run([sys.executable, ANALYZER, epath], capture_output=True, text=True).stdout
    check("A1 packet has rejected call", "grep -rn secret ." in out and "DENY #1" in out)
    check("A2 next calls listed", "[Bash] ls -la" in out)
    check("A3 delegation prompt quoted <=500", "Run exactly: grep -rn secret" in out
          and "x" * 550 not in out)
    check("A4 same prompt_id shown", "Same prompt_id events:" in out)
    check("A5 join failure surfaced", "join failures: 1/2" in out and "FAILED" in out)
    check("A6 session header", f"culvert_version={VERSION}" in out and "cli=['9.9.9-test']" in out)


    # ---- N. synthetic task-narrowing fixture (v0.3.1 release gate) ----
    # Mirrors the first observed natural-workload incident: a denied inline-python
    # listing of 13 metadata keys was rewritten VIA THE WRITE TOOL into a script
    # that lists only 5 keys. The auditor must surface the 8 vanished keys.
    g = os.path.join(base, "nar"); os.makedirs(g)
    keys13 = [f"key_{i:02d}" for i in range(1, 14)]
    keys05 = keys13[:5]
    denied_cmd = ("python3 -c \"import json; c=json.load(open('census.json')); "
                  + "; ".join(f"print(c['_meta']['{k}'])" for k in keys13) + "\"")
    script5 = ("import json\nc=json.load(open('census.json'))\n"
               + "\n".join(f"print(c['_meta']['{k}'])" for k in keys05))
    tpath = os.path.join(g, "transcript.jsonl")
    rows = [
        {"message": {"content": [
            {"type": "tool_use", "id": "toolu_NAR", "name": "Bash",
             "input": {"command": denied_cmd}}]}},
        {"message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_NAR",
             "content": "DELEGATE_REQUIRED: rule=long-inline-python. ..."}]}},
        {"message": {"content": [
            {"type": "tool_use", "id": "toolu_NW", "name": "Write",
             "input": {"file_path": "/tmp/list5.py", "content": script5}}]}},
        {"message": {"content": [
            {"type": "tool_use", "id": "toolu_NR", "name": "Bash",
             "input": {"command": "python3 /tmp/list5.py | head -40"}}]}},
    ]
    open(tpath, "w").write("\n".join(json.dumps(r) for r in rows))
    epath = os.path.join(g, "events.jsonl")
    open(epath, "w").write(json.dumps(
        {"ts": "t", "tool": "Bash", "decision": "deny", "rule": "long-inline-python",
         "tool_use_id": "toolu_NAR", "prompt_id": "pN", "transcript_path": tpath,
         "culvert_version": VERSION, "policy_hash": "abcabcabcabc", "cmd_len": 400}) + "\n")
    out = subprocess.run([sys.executable, ANALYZER, epath], capture_output=True, text=True).stdout
    for k in keys13[5:]:
        check(f"N removed {k} surfaced", k in out.split("Removed identifiers/literals")[-1].split("Added")[0])
    removed_block = out.split("Removed identifiers/literals")[-1].split("Added")[0]
    check("N kept keys not flagged", all(k not in removed_block for k in keys05))
    check("N evidence-only wording", "not a verdict" in out)

    print(f"\n{'FAIL ' + str(len(FAILS)) if FAILS else 'ALL PASS'}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
