"""CULVERT shared helpers — policy loading (with override merge) and
events.jsonl logging.

Policy precedence:
  1. env GOVERNOR_POLICY — replaces the whole policy file (for testing, no merge)
  2. default (<plugin_root>/config/policy.json, immutable) + override merge
     override = <state_dir>/policy.json — absent means defaults only
- Unknown keys and type errors are not silently ignored: they are collected as
  warnings; on the hook path they are written to events.jsonl once (deduplicated
  by a content-hash marker) and surfaced by the status skill.
- Fail-safe: a broken override file is ignored entirely; a bad individual key is
  skipped; any other exception falls back to fail-open (a governor bug must
  never lock the user out).
Logs never contain command contents, DB rows, or personal data — rule names and
lengths only.
"""
import hashlib
import json
import os
import datetime

DEFAULT_POLICY = {
    "enabled": False,
    "max_command_length": 500,
    "max_inline_code_length": 200,
    "max_read_bytes": 204800,
    "max_result_bytes": 8192,
    "rules": {},
}

#: key/type schema used to validate overrides. Valid rule names are the ones the
#: gate/validator actually read.
POLICY_TYPES = {
    "enabled": bool,
    "max_command_length": int,
    "max_inline_code_length": int,
    "max_read_bytes": int,
    "max_result_bytes": int,
    "rules": dict,
}
KNOWN_RULES = {
    "heredoc", "long_inline_python", "db_query", "test_run", "recursive_search",
    "long_command", "big_file_dump", "big_read", "worker_teammate_spawn",
    "fan_out_read", "bounded_output_exemption",
}

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_policy_path():
    return os.path.join(PLUGIN_ROOT, "config", "policy.json")


def override_policy_path():
    return os.path.join(state_dir(), "policy.json")


def state_dir():
    d = os.environ.get("GOVERNOR_DIR")
    if d:
        return d
    root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    return os.path.join(root, ".claude", "governor-plugin")


def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _merge_override(base, ov, warnings):
    """Merge ov onto base with key validation; skip bad keys and record warnings."""
    for k, v in ov.items():
        if k not in POLICY_TYPES:
            warnings.append(("unknown-policy-key", k))
            continue
        want = POLICY_TYPES[k]
        # bool is a subtype of int in Python — treat True in an int slot as a type error
        if want is int and isinstance(v, bool) or not isinstance(v, want):
            warnings.append(("bad-policy-type", f"{k}={type(v).__name__}"))
            continue
        if k == "rules":
            merged_rules = dict(base.get("rules", {}))
            for rk, rv in v.items():
                if rk not in KNOWN_RULES:
                    warnings.append(("unknown-policy-rule", rk))
                    continue
                if not isinstance(rv, bool):
                    warnings.append(("bad-policy-type", f"rules.{rk}={type(rv).__name__}"))
                    continue
                merged_rules[rk] = rv
            base["rules"] = merged_rules
        else:
            base[k] = v
    return base


def load_policy_ex():
    """Return (policy, source description, warnings). Never writes logs (for status)."""
    warnings = []
    env_p = os.environ.get("GOVERNOR_POLICY")
    if env_p:
        try:
            p = _read_json(env_p)
            merged = dict(DEFAULT_POLICY)
            merged.update(p)
            return merged, f"env GOVERNOR_POLICY ({env_p})", warnings
        except Exception:
            return dict(DEFAULT_POLICY), \
                f"env GOVERNOR_POLICY ({env_p}) — load failed, built-in defaults (disabled)", warnings

    try:
        base = dict(DEFAULT_POLICY)
        base.update(_read_json(default_policy_path()))
    except Exception:
        return dict(DEFAULT_POLICY), "default load failed — built-in defaults (disabled)", warnings

    ov_path = override_policy_path()
    if not os.path.isfile(ov_path):
        return base, "default", warnings
    try:
        ov = _read_json(ov_path)
        if not isinstance(ov, dict):
            raise ValueError("override root must be an object")
    except Exception:
        warnings.append(("broken-policy-override", ov_path))
        return base, f"default (override unreadable — ignored: {ov_path})", warnings
    base = _merge_override(base, ov, warnings)
    return base, f"default + override ({ov_path})", warnings


def _warn_once(warnings):
    """Write a given warning set to events.jsonl only once (content-hash marker)."""
    try:
        digest = hashlib.sha256(json.dumps(sorted(warnings)).encode()).hexdigest()[:16]
        marker = os.path.join(state_dir(), ".policy_warned")
        try:
            with open(marker, encoding="utf-8") as f:
                if f.read().strip() == digest:
                    return
        except OSError:
            pass
        for rule, detail in warnings:
            log_event(agent="policy", tool="PolicyLoad", decision="warn",
                      rule=rule, violation=str(detail)[:120])
        os.makedirs(state_dir(), exist_ok=True)
        with open(marker, "w", encoding="utf-8") as f:
            f.write(digest)
    except Exception:
        pass


def load_policy():
    """Hook path — logs warnings to events.jsonl (deduplicated), returns the policy."""
    try:
        policy, _source, warnings = load_policy_ex()
        if warnings:
            _warn_once(warnings)
        return policy
    except Exception:
        return dict(DEFAULT_POLICY)  # fail-open


def rule_on(policy, name):
    return policy.get("rules", {}).get(name, True)


def is_context_worker(agent_type):
    """Plugin agents may carry a namespace (<plugin>:context-worker) — same worker."""
    t = agent_type or ""
    return t == "context-worker" or t.endswith(":context-worker")


def log_event(**fields):
    try:
        d = state_dir()
        os.makedirs(d, exist_ok=True)
        rec = {"ts": datetime.datetime.now().isoformat(timespec="seconds")}
        rec.update({k: v for k, v in fields.items() if v is not None})
        with open(os.path.join(d, "events.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass  # logging failure must never block a verdict
