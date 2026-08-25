#!/usr/bin/env python3
"""v0.2.1 수용 테스트 A~G — override 병합·fail-safe·status 읽기전용.

pytest 불사용(스탠드얼론): python3 tests/test_v021.py
각 케이스는 GOVERNOR_DIR 를 임시 디렉터리로 재지정해 실 로그를 오염시키지 않는다.
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
    env = dict(os.environ, GOVERNOR_DIR=gdir)
    env.pop("GOVERNOR_POLICY", None)
    r = subprocess.run([sys.executable, GATE], input=json.dumps(payload),
                       capture_output=True, text=True, env=env, timeout=10)
    return r.stdout.strip()


def load_ex(gdir):
    env = dict(os.environ, GOVERNOR_DIR=gdir)
    env.pop("GOVERNOR_POLICY", None)
    code = ("import sys, json; sys.path.insert(0, %r); from _governor import load_policy_ex as L; "
            "p, s, w = L(); print(json.dumps({'p': p, 's': s, 'w': w}))"
            % os.path.join(PLUGIN, "scripts"))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    return json.loads(r.stdout)


LONG = {"tool_name": "Bash", "tool_input": {"command": "echo " + "z" * 600}}
DEFAULT = json.load(open(os.path.join(PLUGIN, "config", "policy.json")))


def main():
    base = tempfile.mkdtemp(prefix="gov-v021-")

    # A. override 없음 → default 와 완전 동일 + 600자 deny (v0.2 동작)
    d = os.path.join(base, "a"); os.makedirs(d)
    r = load_ex(d)
    check("A1 effective == default", {k: r["p"][k] for k in DEFAULT} == DEFAULT)
    check("A2 source == default", r["s"] == "default", r["s"])
    check("A3 warnings 없음", r["w"] == [])
    check("A4 600자 → deny:long-command", "long-command" in run_gate(LONG, d))

    # B. valid override → 지정 key 만 변경
    d = os.path.join(base, "b"); os.makedirs(d)
    json.dump({"max_command_length": 900, "rules": {"test_run": False}},
              open(os.path.join(d, "policy.json"), "w"))
    r = load_ex(d)
    check("B1 max_command_length=900", r["p"]["max_command_length"] == 900)
    check("B2 다른 threshold 는 default", r["p"]["max_read_bytes"] == DEFAULT["max_read_bytes"])
    check("B3 rules.test_run=False, 나머지 유지",
          r["p"]["rules"]["test_run"] is False and r["p"]["rules"]["heredoc"] is True)
    check("B4 600자 → allow (900 한도)", run_gate(LONG, d) == "")
    check("B5 pytest → allow (규칙 꺼짐)",
          run_gate({"tool_name": "Bash", "tool_input": {"command": "pytest -q"}}, d) == "")
    check("B6 sqlite3 → 여전히 deny",
          "db-query" in run_gate({"tool_name": "Bash", "tool_input": {"command": "sqlite3 x.db 'SELECT 1'"}}, d))

    # C. broken override → default 로 정상 동작
    d = os.path.join(base, "c"); os.makedirs(d)
    open(os.path.join(d, "policy.json"), "w").write("{ not json !!")
    r = load_ex(d)
    check("C1 default 로 동작", r["p"]["max_command_length"] == DEFAULT["max_command_length"])
    check("C2 broken 경고", any(w[0] == "broken-policy-override" for w in r["w"]))
    check("C3 600자 → deny (default)", "long-command" in run_gate(LONG, d))

    # D. wrong type → 해당 key 만 무시
    d = os.path.join(base, "d"); os.makedirs(d)
    json.dump({"max_command_length": "big", "max_result_bytes": 4096},
              open(os.path.join(d, "policy.json"), "w"))
    r = load_ex(d)
    check("D1 잘못된 key 는 default 유지", r["p"]["max_command_length"] == DEFAULT["max_command_length"])
    check("D2 올바른 key 는 적용", r["p"]["max_result_bytes"] == 4096)
    check("D3 타입 경고", any(w[0] == "bad-policy-type" for w in r["w"]))

    # E. unknown key → 경고 표면화 (load_policy 경유 시 events.jsonl 에 warn 1회)
    d = os.path.join(base, "e"); os.makedirs(d)
    json.dump({"max_foo": 1, "rules": {"no_such_rule": True}},
              open(os.path.join(d, "policy.json"), "w"))
    r = load_ex(d)
    check("E1 unknown key 경고", any(w[0] == "unknown-policy-key" for w in r["w"]))
    check("E2 unknown rule 경고", any(w[0] == "unknown-policy-rule" for w in r["w"]))
    run_gate({"tool_name": "Bash", "tool_input": {"command": "ls"}}, d)  # hook 경로 → warn 기록
    log = open(os.path.join(d, "events.jsonl")).read()
    check("E3 events.jsonl 에 warn", '"decision": "warn"' in log and "unknown-policy-key" in log)
    n1 = log.count('"decision": "warn"')
    run_gate({"tool_name": "Bash", "tool_input": {"command": "ls"}}, d)  # 두 번째 — 중복 억제
    n2 = open(os.path.join(d, "events.jsonl")).read().count('"decision": "warn"')
    check("E4 warn 중복 억제 (1회만)", n1 == n2, f"{n1}->{n2}")

    # F/G. status — 읽기 전용 · 8항목
    d = os.path.join(base, "e")  # E 의 로그가 있는 디렉터리 재사용
    before = os.path.getsize(os.path.join(d, "events.jsonl"))
    env = dict(os.environ, GOVERNOR_DIR=d); env.pop("GOVERNOR_POLICY", None)
    out = subprocess.run([sys.executable, STATUS], capture_output=True, text=True, env=env).stdout
    after = os.path.getsize(os.path.join(d, "events.jsonl"))
    check("F1 status 는 로그 불변", before == after)
    for label in ("Governor enabled", "Plugin version", "Worker type", "Worker model",
                  "Policy source", "Event log", "Legacy hook 중복", "Recent decisions"):
        check(f"G {label}", label in out)
    check("G 경고 표면화", "unknown-policy-key" in out)

    print(f"\n{'FAIL ' + str(len(FAILS)) if FAILS else 'ALL PASS'}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
