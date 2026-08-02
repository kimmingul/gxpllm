"""
셸 명령 감사 기록 hook (PostToolUse, Bash)

hook 을 통과해 실제로 실행된 셸 명령을 감사 로그에 기록한다.
guard_bash 가 차단한 것은 access_blocked 로, 통과한 것은 여기서 command_executed 로 남는다.
둘을 합치면 "이 세션에서 무엇을 시도했고 무엇이 실행됐는가"가 완전해진다.

정책
- 절대 차단하지 않는다. 항상 exit 0
- 기록 실패는 조용히 무시한다 (작업을 방해하지 않는다)
- 명령 본문은 500자로 절단한다
"""

from _bootstrap import read_hook_payload  # sys.path 설정 + 견고한 입력 파서

import sys
import os

# ============================================================================
# 설정 영역 - 여기서 수정하세요
# ============================================================================

COMMAND_MAX_LENGTH = 500
OUTPUT_PREVIEW_LENGTH = 200
EXIT_OK = 0

# 이 패턴에 걸리는 명령은 기록하지 않는다 (잡음 감소)
SKIP_PATTERNS = ('cd ', 'ls', 'dir', 'pwd', 'echo ', 'cls', 'clear')


# ============================================================================
# 메인 로직
# ============================================================================

def should_skip(command):
    """
    감사 기록을 생략할 명령인지 판단한다

    Args:
        command: 셸 명령 문자열

    Returns:
        생략하면 True
    """
    stripped = command.strip().lower()
    if not stripped:
        return True
    return any(stripped == p.strip() or stripped.startswith(p) for p in SKIP_PATTERNS)


def summarize_response(tool_response):
    """
    도구 응답에서 감사용 요약을 추출한다

    출력 본문에 데이터 값이 섞일 수 있으므로 길이를 제한하고
    성공 여부와 앞부분만 남긴다

    Args:
        tool_response: PostToolUse 가 전달한 tool_response

    Returns:
        요약 딕셔너리
    """
    if isinstance(tool_response, dict):
        return {
            'success': not tool_response.get('is_error', False),
            'preview_sha256': None,
            'interrupted': bool(tool_response.get('interrupted', False)),
        }
    return {'success': True, 'preview_sha256': None, 'interrupted': False}


def main():
    """메인 함수"""
    try:
        payload = read_hook_payload()
    except Exception:
        sys.exit(EXIT_OK)

    try:
        from gxpllm.core import find_study_root, append_audit, sha256_text

        command = (payload.get('tool_input') or {}).get('command', '')
        if not isinstance(command, str) or should_skip(command):
            sys.exit(EXIT_OK)

        cwd = payload.get('cwd') or os.getcwd()
        study_root, _ = find_study_root(cwd)
        if study_root is None:
            sys.exit(EXIT_OK)

        summary = summarize_response(payload.get('tool_response'))

        append_audit(study_root, {
            'event': 'command_executed',
            'tool': 'Bash',
            'command': command[:COMMAND_MAX_LENGTH],
            'command_sha256': sha256_text(command),
            'cwd': str(cwd),
            'session_id': payload.get('session_id'),
            'success': summary['success'],
        })

    except Exception:
        # 감사 기록 실패가 작업을 막아서는 안 된다
        pass

    sys.exit(EXIT_OK)


if __name__ == "__main__":
    main()
