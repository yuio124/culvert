#!/usr/bin/env python3
"""Standalone hook 판정 테스트 — parity_cases.json 의 기대값과 이 plugin 의 실판정을 대조.

(원 프로젝트의 run_parity.py 는 legacy 구현과의 비교라 legacy 저장소가 필요했다.
standalone 에서는 기대값 대조만 남긴다 — 케이스 데이터는 동일.)
사용: python3 tests/run_cases.py
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)

SCRIPTS = {
    "pretooluse": "context_gate.py",
    "subagentstop": "validate_worker_result.py",
    "sessionstart": "governor_restore.py",
}


def run_hook(hook, payload, tmp):
    env = dict(os.environ)
    env.pop("GOVERNOR_POLICY", None)
    env["GOVERNOR_DIR"] = tmp
    script = os.path.join(PLUGIN, "scripts", SCRIPTS[hook])
    r = subprocess.run([sys.executable, script], input=json.dumps(payload),
                       capture_output=True, text=True, env=env, cwd=PLUGIN, timeout=10)
    return r.stdout.strip()


def classify(hook, out):
    if not out:
        return {"pretooluse": "allow", "subagentstop": "approve", "sessionstart": "silent"}[hook]
    j = json.loads(out)
    if hook == "pretooluse":
        h = j.get("hookSpecificOutput", {})
        if h.get("permissionDecision") == "deny":
            reason = h.get("permissionDecisionReason", "")
            if "rule=" in reason:
                return "deny:" + reason.split("rule=")[1].split(".")[0]
            if "DELEGATE_SUBAGENT_REQUIRED" in reason:
                return "deny:worker-teammate-spawn"
        return "other"
    if hook == "subagentstop":
        return "block" if j.get("decision") == "block" else "other"
    return "inject" if j.get("hookSpecificOutput", {}).get("additionalContext") else "other"


def main():
    cases = json.load(open(os.path.join(HERE, "parity_cases.json"), encoding="utf-8"))
    tmp = tempfile.mkdtemp(prefix="governor-cases-")
    big = os.path.join(tmp, "big.txt")
    open(big, "w").write("x" * 300000)
    os.makedirs(os.path.join(tmp, "docs"), exist_ok=True)  # find-dir 규칙은 cwd 기준 실재 디렉터리를 본다
    subst = {"__BIGFILE__": big, "__PAD9K__": "y" * 9000,
             "__LONG600__": "echo " + "z" * 600, "__TMP__": tmp}

    fails = 0
    for hook, lst in cases.items():
        if hook.startswith("_"):
            continue
        for c in lst:
            raw = json.dumps(c["in"])
            for k, v in subst.items():
                raw = raw.replace(k, v)
            got = classify(hook, run_hook(hook, json.loads(raw), tmp))
            ok = got == c["expect"]
            fails += (not ok)
            print(f"  {'PASS' if ok else 'FAIL'}  {c['id']:<20} expect={c['expect']} got={got}")
    print(f"\n{'FAIL ' + str(fails) if fails else 'ALL PASS'}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
