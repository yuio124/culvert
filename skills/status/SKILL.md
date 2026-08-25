---
name: status
description: Context Governor 상태 확인(읽기 전용) — enabled·plugin 버전·worker 타입/모델·policy 출처·이벤트 로그·legacy hook 중복·최근 deny/approve 집계를 한 화면으로 보여준다.
---

Context Governor 의 현재 상태를 보여주는 스킬이다. **읽기 전용** — 어떤 파일도 쓰거나 바꾸지 않는다.

할 일은 하나뿐이다:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/status.py"
```

를 실행하고 출력을 **그대로** 사용자에게 보여줘라. 요약하거나 재구성하지 마라 —
경고(⚠) 줄이 있으면 그 줄이 곧 사용자가 봐야 할 내용이다.

`${CLAUDE_PLUGIN_ROOT}` 가 치환되지 않은 채 보인다면, 이 스킬 파일 위치 기준
`../../scripts/status.py` (= plugin 루트의 `scripts/status.py`)를 실행하면 된다.
