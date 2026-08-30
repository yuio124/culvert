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
import re
import sys

NEXT_CALLS = 3
DIFF_CALLS = 6          # rewrites considered for the identifier diff
DIFF_MAX_REMOVED = 20
DIFF_MAX_ADDED = 10
#: common shell/python words excluded from the identifier diff (minimal, not a parser)
DIFF_STOPWORDS = {
    "python3", "python", "json", "print", "import", "open", "read", "head",
    "echo", "for", "in", "do", "done", "cat", "load", "loads", "dumps", "item",
    "items", "data", "else", "elif", "true", "false", "none", "self", "range",
    "keys", "values", "sorted", "lines", "line", "file", "path", "with", "def",
    "return", "get", "list", "dict", "join", "split", "write", "exit", "then",
}
_STR_LIT_RE = re.compile(r"'([^'\n]{2,48})'|\"([^\"\n]{2,48})\"")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")
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


def extract_tokens(text):
    """Identifiers/short string literals for the disappearance diff.

    Deliberately minimal: quoted literals (no spaces/slashes) and >=4-char
    identifiers, minus a small stopword list. This is evidence display for a
    human reader — NOT a semantic-equivalence judgement. Identifiers can
    legitimately disappear in a normal rewrite.
    """
    toks = set()
    for m in _STR_LIT_RE.finditer(text):
        lit = m.group(1) or m.group(2)
        if "/" in lit or " " in lit:
            continue
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.\-]{3,}", lit):
            toks.add(lit)
    for m in _IDENT_RE.finditer(text):
        toks.add(m.group(0))
    return {t for t in toks if t.lower() not in DIFF_STOPWORDS and len(t) <= 40}


def rewrite_text(tool_input):
    """Text of a follow-up call relevant to the diff: Bash command, Write/Edit
    content, or a delegation prompt."""
    parts = []
    for key in ("command", "content", "new_string", "prompt"):
        v = tool_input.get(key)
        if isinstance(v, str):
            parts.append(v)
    return "\n".join(parts)


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
        denied_text = rewrite_text(use["input"]) or str(use["input"])
        corpus = "\n".join(rewrite_text(u["input"])
                            for u in t["uses"][use["i"] + 1: use["i"] + 1 + DIFF_CALLS])
        if corpus.strip():
            removed = sorted(extract_tokens(denied_text) - extract_tokens(corpus))
            added = sorted(extract_tokens(corpus) - extract_tokens(denied_text))
            if removed:
                print(f"Removed identifiers/literals (vs next {DIFF_CALLS} calls — "
                      "evidence only, not a verdict):")
                for tok in removed[:DIFF_MAX_REMOVED]:
                    print(f"  - {tok}")
                if len(removed) > DIFF_MAX_REMOVED:
                    print(f"  ... and {len(removed) - DIFF_MAX_REMOVED} more")
            else:
                print("Removed identifiers/literals: none")
            if added:
                shown = ", ".join(added[:DIFF_MAX_ADDED])
                print(f"Added identifiers/literals (reference): {shown}"
                      + (" ..." if len(added) > DIFF_MAX_ADDED else ""))
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
