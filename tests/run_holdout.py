#!/usr/bin/env python3
"""RC2 holdout 러너 — holdout_v022.json (규칙 수정 전에 라벨 고정) 을 gate 에 대조.

혼동행렬 정의: 라벨 deny:* = positive. TP=deny 예측·deny 라벨(rule 무관 아님 — rule 도 일치해야 PASS,
행렬에는 deny/allow 만 반영). 사용: python3 tests/run_holdout.py
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
GATE = os.path.join(PLUGIN, "scripts", "context_gate.py")


def run_gate(cmd, tmp, cwd):
    env = dict(os.environ)
    env.pop("GOVERNOR_POLICY", None)
    env["GOVERNOR_DIR"] = tmp
    payload = {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": cwd}
    r = subprocess.run([sys.executable, GATE], input=json.dumps(payload),
                       capture_output=True, text=True, env=env, timeout=10)
    out = r.stdout.strip()
    if not out:
        return "allow"
    reason = json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
    return "deny:" + reason.split("rule=")[1].split(".")[0]


def main():
    cases = json.load(open(os.path.join(HERE, "holdout_v022.json"), encoding="utf-8"))
    tmp = tempfile.mkdtemp(prefix="governor-holdout-")
    big = os.path.join(tmp, "big.txt")
    open(big, "w").write("x" * 300000)
    # cwd 픽스처: find/glob 규칙용 실재 디렉터리·파일
    for d in ("src", "test", "scripts"):
        os.makedirs(os.path.join(tmp, d), exist_ok=True)
    open(os.path.join(tmp, "src", "a.js"), "w").write("x")
    open(os.path.join(tmp, "README.md"), "w").write("hi")
    open(os.path.join(tmp, "app.log"), "w").write("l\n")

    tp = fp = fn = tn = fails = 0
    for group, lst in cases.items():
        if group.startswith("_"):
            continue
        for c in lst:
            got = run_gate(c["cmd"].replace("__BIGFILE__", big), tmp, tmp)
            exp = c["expect"]
            ok = got == exp
            fails += (not ok)
            e_deny, g_deny = exp.startswith("deny"), got.startswith("deny")
            if e_deny and g_deny:
                tp += 1
            elif e_deny and not g_deny:
                fn += 1
            elif not e_deny and g_deny:
                fp += 1
            else:
                tn += 1
            print(f"  {'PASS' if ok else 'FAIL'}  {c['id']:<4} expect={exp:<24} got={got:<24} {c['cmd'][:56]}")
    n = tp + fp + fn + tn
    prec = tp / (tp + fp) if tp + fp else float("nan")
    rec = tp / (tp + fn) if tp + fn else float("nan")
    print(f"\nN={n}  TP={tp} FP={fp} FN={fn} TN={tn}")
    print(f"precision={prec:.0%} recall={rec:.0%} "
          f"FPR={fp / (fp + tn):.0%} FNR={fn / (fn + tp):.0%}")
    print("RULE-MISMATCH+판정 FAIL 합:", fails)
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
