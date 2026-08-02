"""
hook 부트스트랩

모든 hook 스크립트가 첫 줄에서 import 하여 plugin 루트를 sys.path 에 넣는다.

이 파일은 의도적으로 단순하게 유지한다.
- 표준 라이브러리만 사용
- 모듈 레벨에서 예외를 던질 수 있는 코드를 두지 않는다
- 복잡한 문법(f-string 중첩, 최신 문법)을 쓰지 않는다

이유: hook 스크립트가 import 단계에서 죽으면 exit code 가 1 이 되고,
      Claude Code 는 exit 2 만 차단으로 처리하므로 경계가 열린다.
      따라서 부트스트랩은 절대 실패하지 않아야 한다.
"""

import json
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent

if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))


def read_hook_payload():
    """
    stdin 에서 hook 입력 JSON 을 읽는다

    BOM(utf-8-sig), CRLF, 잉여 공백을 허용한다.
    Claude Code 는 보통 순수 UTF-8 을 보내지만, 셸 파이프나 편집기를 거치면
    BOM 이 섞일 수 있으므로 관대하게 처리한다.

    Returns:
        파싱된 딕셔너리

    Raises:
        ValueError: JSON 으로 해석할 수 없는 경우
    """
    raw = sys.stdin.buffer.read()
    if not raw:
        raise ValueError("hook 입력이 비어 있습니다")

    for encoding in ('utf-8-sig', 'utf-8', 'cp949', 'latin-1'):
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        text = text.strip().lstrip('﻿')
        if not text:
            continue
        try:
            payload = json.loads(text)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload

    raise ValueError("hook 입력을 JSON 객체로 해석할 수 없습니다")
