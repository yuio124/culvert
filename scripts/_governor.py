"""Context Governor plugin 공용 — policy 로드(override 병합) · events.jsonl 로깅.

정책 우선순위 (v0.2.1):
  1. env GOVERNOR_POLICY — 파일 전체 대체 (테스트용, 병합 없음)
  2. default(<plugin_root>/config/policy.json, immutable) + override 병합
     override = <state_dir>/policy.json — 없으면 default 단독(v0.2 와 동일 동작)
- unknown key·타입 오류는 조용히 무시하지 않는다: warn 으로 수집되고, hook 경로에서는
  events.jsonl 에 기록(같은 내용은 1회만 — 마커 해시로 중복 억제), status 스킬이 표면화한다.
- fail-safe: override 파싱 실패 → override 전체 무시 · 개별 키 오류 → 그 키만 무시 ·
  그 외 예외 → 기존 fail-open 원칙 유지 (governor 버그가 사용자를 막으면 안 된다).
로그에는 command 전문·DB row·개인정보를 넣지 않는다 — 규칙명과 길이만.
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

#: override 검증에 쓰는 키·타입 스키마. rules 의 유효 이름은 gate/validator 가 실제로 보는 것들.
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
    """base 위에 ov 를 키 검증하며 병합. 문제 키는 건너뛰고 warnings 에 적는다."""
    for k, v in ov.items():
        if k not in POLICY_TYPES:
            warnings.append(("unknown-policy-key", k))
            continue
        want = POLICY_TYPES[k]
        # bool 은 int 의 하위형이라 순서 주의: int 자리에 True 가 오면 타입 오류로 본다
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
    """(policy, source 설명, warnings[(rule, detail)]) — 로그를 남기지 않는다 (status 용)."""
    warnings = []
    env_p = os.environ.get("GOVERNOR_POLICY")
    if env_p:
        try:
            p = _read_json(env_p)
            merged = dict(DEFAULT_POLICY)
            merged.update(p)
            return merged, f"env GOVERNOR_POLICY ({env_p})", warnings
        except Exception:
            return dict(DEFAULT_POLICY), f"env GOVERNOR_POLICY ({env_p}) — 로드 실패, 내장 기본값(disabled)", warnings

    try:
        base = dict(DEFAULT_POLICY)
        base.update(_read_json(default_policy_path()))
    except Exception:
        return dict(DEFAULT_POLICY), "default 로드 실패 — 내장 기본값(disabled)", warnings

    ov_path = override_policy_path()
    if not os.path.isfile(ov_path):
        return base, "default", warnings
    try:
        ov = _read_json(ov_path)
        if not isinstance(ov, dict):
            raise ValueError("override root must be an object")
    except Exception:
        warnings.append(("broken-policy-override", ov_path))
        return base, f"default (override 파싱 실패 — 무시: {ov_path})", warnings
    base = _merge_override(base, ov, warnings)
    return base, f"default + override ({ov_path})", warnings


def _warn_once(warnings):
    """같은 경고 묶음은 events.jsonl 에 1회만 — 내용 해시 마커로 억제."""
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
    """hook 용 — 경고를 events.jsonl 에 기록(중복 억제)하고 policy 만 돌려준다."""
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
    """plugin 제공 agent 는 이름공간이 붙을 수 있다(<plugin>:context-worker) — 둘 다 같은 워커다."""
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
        pass  # 로깅 실패가 판정을 막으면 안 된다
