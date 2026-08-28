#!/usr/bin/env python3
"""CULVERT offline audit v1 — strictly read-only.

Joins CULVERT events.jsonl with Claude session transcripts on tool_use_id and
prints one human-reviewable packet per DENY. It does NOT classify behavior
(delegated / rewritten / bypassed / abandoned) — a human judges that from the
packet. No reuse/retention analysis. Join failures are counted and printed,
never silently dropped.

Usage:
    python3 tools/analyze_session.py <events.jsonl> [transcript.jsonl ...]

If no transcript paths are given, the transcript_path fields recorded in the
events are used. Multiple transcripts are accepted (resume/continue can split
one piece of work across session files).

Audit packets can quote commands, tool arguments, and delegation prompts.
Treat the output as private data — keep it local, never publish it.
"""
import json
import os
import sys

NEXT_CALLS = 3
PROMPT_QUOTE = 500
INPUT_QUOTE = 160


def load_events(path):
    events = []
    for ln in open(path, encoding="utf-8"):
        ln = ln.strip()
        if not ln:
            continue
        try:
            events.append(json.loads(ln))
        except ValueError:
            pass
    return events


def load_transcript(path):
    """Ordered tool_use list: [{i, id, name, input}]. Also session meta."""
    uses, models, cli = [], set(), set()
    try:
        fh = open(path, encoding="utf-8")
    except OSError:
        return None
    for ln in fh:
        try:
            row = json.loads(ln)
        except ValueError:
            continue
        if row.get("version"):
            cli.add(str(row["version"]))
        msg = row.get("message") or {}
        if msg.get("model"):
            models.add(msg["model"])
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                uses.append({"i": len(uses), "id": block.get("id"),
                             "name": block.get("name"),
                             "input": block.get("input") or {}})
    return {"uses": uses, "by_id": {u["id"]: u for u in uses},
            "models": sorted(models), "cli": sorted(cli)}


def brief(tool_input):
    if "command" in tool_input:
        s = str(tool_input["command"])
    else:
        s = json.dumps(tool_input, ensure_ascii=False)
    s = s.replace("\n", " ")
    return s[:INPUT_QUOTE] + ("..." if len(s) > INPUT_QUOTE else "")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    events = load_events(sys.argv[1])
    t_paths = sys.argv[2:] or sorted(
        {e.get("transcript_path") for e in events if e.get("transcript_path")})
    transcripts = {}
    for p in t_paths:
        t = load_transcript(p)
        if t is None:
            print(f"WARNING: transcript unreadable: {p}")
        else:
            transcripts[p] = t

    denies = [e for e in events if e.get("decision") == "deny"]
    print(f"events: {len(events)} | denies: {len(denies)} | transcripts: {len(transcripts)}")
    header = [e for e in events if e.get("culvert_version")]
    if header:
        h = header[0]
        print(f"session header: culvert_version={h.get('culvert_version')} "
              f"policy_hash={h.get('policy_hash')} permission_mode={h.get('permission_mode')}")
    for t_path, t in transcripts.items():
        print(f"transcript {os.path.basename(t_path)}: models={t['models']} cli={t['cli']}")

    join_fail = 0
    for n, e in enumerate(denies, 1):
        print(f"\n=== DENY #{n} ===")
        print(f"rule: {e.get('rule')} | tool: {e.get('tool')} | ts: {e.get('ts')} "
              f"| prompt_id: {e.get('prompt_id')} | cmd_len: {e.get('cmd_len')}")
        tid = e.get("tool_use_id")
        hit = None
        for t in transcripts.values():
            if tid and tid in t["by_id"]:
                hit = (t, t["by_id"][tid])
                break
        if hit is None:
            join_fail += 1
            print("Join: FAILED (tool_use_id not found in any transcript)")
            continue
        t, use = hit
        print("Join: OK")
        print(f"Rejected call [{use['name']}]: {brief(use['input'])}")
        following = t["uses"][use["i"] + 1: use["i"] + 1 + NEXT_CALLS]
        print(f"Next {NEXT_CALLS} tool calls:")
        for k, u in enumerate(following, 1):
            print(f"  {k}. [{u['name']}] {brief(u['input'])}")
        same = [x for x in events
                if x.get("prompt_id") and x.get("prompt_id") == e.get("prompt_id")
                and x.get("tool_use_id") != tid]
        if same:
            print("Same prompt_id events:")
            for x in same:
                print(f"  - {x.get('tool')} {x.get('decision')} rule={x.get('rule')} "
                      f"cmd_len={x.get('cmd_len')}")
        delegation = next((u for u in t["uses"][use["i"] + 1:]
                           if "subagent_type" in u["input"]), None)
        if delegation:
            prompt = str(delegation["input"].get("prompt", ""))
            print(f"Worker delegation [{delegation['input'].get('subagent_type')}]:")
            print("  " + prompt[:PROMPT_QUOTE].replace("\n", "\n  "))
        else:
            print("Worker delegation: none observed after this deny")

    print(f"\njoin failures: {join_fail}/{len(denies)}")
    if join_fail:
        print("NOTE: failed joins are listed above — do not treat them as absent denies.")


if __name__ == "__main__":
    main()
