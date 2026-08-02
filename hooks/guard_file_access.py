"""
임상 데이터 접근 차단 hook (PreToolUse, 전체 도구)

Claude Code(Opus)가 임상 데이터에 접근하는 것을 차단한다.

정책: study 루트 안은 기본 거부(default-deny)
  읽기 허용 : docs/ programs/ macros/ templates/ output/tables/ output/figures/ audit/ .gxpllm/
  읽기 거부 : data/ output/listings/ logs/ (manifest.json, assertions.json 만 예외)

검색 도구(Grep/Glob)는 범위를 허용 디렉터리로 명시해야 통과한다.
범위를 지정하지 않거나 study 루트를 지정하면 data/ 를 훑게 되므로 거부한다.

도구 이름이 아니라 tool_input 의 모든 문자열을 재귀 탐색해 경로를 찾는다.
도구가 추가되거나 인자 이름이 바뀌어도(file_path vs filePath) 경계가 유지된다.

정책
- 차단 시 exit code 2, 사유는 stderr 로 출력 (Claude 에게 전달됨)
- 예외가 발생해도 차단한다 (fail-closed)
- 차단 사실은 감사 로그에 access_blocked 로 기록한다
"""

from _bootstrap import read_hook_payload  # sys.path 설정 + 견고한 입력 파서

import sys
import os

# ============================================================================
# 설정 영역 - 여기서 수정하세요
# ============================================================================

EXIT_ALLOW = 0
EXIT_BLOCK = 2

# 검색 도구 (범위 명시 강제)
SEARCH_TOOLS = ('grep', 'glob', 'search', 'find')

# 읽기 전용이 확실한 도구 — **정확히 일치**해야 한다
#
# 쓰기 도구를 나열하는 대신 읽기 도구를 나열하고 나머지를 쓰기로 본다.
# 모르는 도구는 쓰기로 취급하는 것이 fail-closed 다.
#
# **부분 문자열 매칭을 쓰면 안 된다.**
# 'read' 가 부분 일치하면 ReadWrite / catalog_write(cat) / ViewEdit(view) 처럼
# 이름에 읽기 단어가 섞인 쓰기 도구가 읽기 모드로 판정되어
# .gxpllm/ audit/ output/ 에 쓸 수 있게 된다.
READ_ONLY_TOOLS = frozenset({
    'read', 'grep', 'glob', 'ls', 'list',
    'notebookread', 'notebook_read',
    'readfile', 'read_file',
    'listdir', 'list_dir', 'listfiles', 'list_files',
    'view', 'viewfile', 'view_file',
    'search', 'searchfiles', 'search_files',
    'find', 'findfiles', 'find_files',
    'cat', 'head', 'tail', 'stat',
})

# 이름에 이것이 들어가면 읽기 목록에 있어도 쓰기로 본다 (이중 안전장치)
WRITE_MARKERS = (
    'write', 'edit', 'replace', 'patch', 'save', 'create', 'update',
    'delete', 'remove', 'move', 'rename', 'append', 'insert', 'modify',
)

# 경로가 아닌 것이 확실한 키 (재귀 탐색에서 제외)
NON_PATH_KEYS = frozenset({
    'content', 'new_string', 'old_string', 'prompt', 'description',
    'command',  # Bash 는 guard_bash.py 가 담당
    'body', 'message', 'text', 'query', 'instructions',
})

# 검색 도구에서 경로가 아닌 키 (패턴은 별도로 scope 검사를 거친다)
SEARCH_NON_PATH_KEYS = frozenset({'pattern', 'glob', 'include', 'exclude', 'type'})

# 경로로 볼 최소 조건
PATH_HINT_CHARS = ('/', '\\')
MAX_PATH_LENGTH = 4096
MAX_RECURSION_DEPTH = 8

GUIDANCE = """
  임상 데이터는 Opus 가 직접 읽을 수 없습니다.

  읽을 수 있는 위치
    docs/            Protocol, SAP, DMP, Data Dictionary
    programs/        SAS / Python / R 코드
    output/tables/   집계 표
    output/figures/  그림
    logs/runs/*/manifest.json, assertions.json

  읽을 수 없는 위치
    data/                 원본 및 파생 데이터셋
    output/listings/      피험자 단위 목록
    logs/runs/*/stdout.txt, stderr.txt, execution.log, execution.lst
                          프로그램 출력에 피험자 데이터가 포함될 수 있음

  데이터가 필요한 작업은 이렇게 하십시오.
    1. 로컬 LLM(MCP: local-coder)에게 SAS/Python/R 코드를 작성시킨다
    2. runner 로 실행한다
         python scripts/run_sas.py    --program programs/sas/t_dm.sas
         python scripts/run_python.py --program programs/python/t_ae.py
         python scripts/run_r.py      --program programs/r/f_km.R
    3. assertion 결과와 집계 산출물을 확인한다
         logs/runs/{run_id}/assertions.json
         output/tables/, output/figures/
"""


# ============================================================================
# 메인 로직
# ============================================================================

def looks_like_path(value):
    """
    문자열이 경로로 보이는지 판단한다

    Args:
        value: 검사할 문자열

    Returns:
        경로 후보면 True
    """
    if not isinstance(value, str):
        return False

    stripped = value.strip()
    if not stripped or len(stripped) > MAX_PATH_LENGTH:
        return False

    # 경로 구분자가 있거나, 확장자를 가진 파일명 형태
    if any(ch in stripped for ch in PATH_HINT_CHARS):
        return True

    if '.' in stripped and not stripped.startswith('.') and ' ' not in stripped:
        return True

    return False


def collect_path_candidates(node, depth=0, key=None, skip_keys=NON_PATH_KEYS):
    """
    tool_input 전체를 재귀 탐색해 경로 후보를 모은다

    특정 키 이름에 의존하지 않으므로 도구가 추가되거나
    인자 이름이 바뀌어도(file_path / filePath / target) 경계가 유지된다.

    Args:
        node: 탐색할 값 (딕셔너리 / 리스트 / 문자열)
        depth: 현재 재귀 깊이
        key: 상위 딕셔너리에서의 키 이름
        skip_keys: 탐색에서 제외할 키 이름 집합

    Returns:
        경로 후보 문자열 리스트
    """
    if depth > MAX_RECURSION_DEPTH:
        return []

    if isinstance(node, str):
        if key and str(key).lower() in skip_keys:
            return []
        return [node] if looks_like_path(node) else []

    if isinstance(node, dict):
        found = []
        for child_key, child in node.items():
            if str(child_key).lower() in skip_keys:
                continue
            found.extend(collect_path_candidates(child, depth + 1, child_key, skip_keys))
        return found

    if isinstance(node, (list, tuple)):
        found = []
        for child in node:
            found.extend(collect_path_candidates(child, depth + 1, key, skip_keys))
        return found

    return []


def is_search_tool(tool_name):
    """
    검색 계열 도구인지 판단한다

    Args:
        tool_name: 도구 이름

    Returns:
        검색 도구면 True
    """
    lowered = str(tool_name).lower()
    return any(marker in lowered for marker in SEARCH_TOOLS)


def normalize_tool_name(tool_name):
    """
    도구 이름을 비교용으로 정규화한다

    Args:
        tool_name: 원본 도구 이름

    Returns:
        소문자, 구분자 제거된 이름
    """
    lowered = str(tool_name).lower().strip()
    # MCP 도구는 mcp__server__tool 형태이므로 마지막 조각만 본다
    if '__' in lowered:
        lowered = lowered.rsplit('__', 1)[-1]
    return lowered.replace('-', '').replace('_', '').replace(' ', '')


def is_write_tool(tool_name):
    """
    쓰기 계열 도구인지 판단한다

    **모르는 도구는 쓰기로 본다 (fail-closed).**
    읽기 판정은 정확히 일치할 때만 한다. 부분 문자열 매칭을 쓰면
    ReadWrite / catalog_write / ViewEdit 처럼 이름에 읽기 단어가 섞인
    쓰기 도구가 읽기 모드로 빠져나간다.

    Args:
        tool_name: 도구 이름

    Returns:
        쓰기 도구면 True
    """
    normalized = normalize_tool_name(tool_name)

    # 쓰기 표지가 있으면 읽기 목록에 있어도 쓰기로 본다
    if any(marker in normalized for marker in WRITE_MARKERS):
        return True

    # 정규화한 읽기 목록과 정확히 일치할 때만 읽기
    read_only = {name.replace('_', '') for name in READ_ONLY_TOOLS}
    return normalized not in read_only


def check_search_scope(tool_input, study_root, config, base_dir):
    """
    검색 도구의 범위를 검사한다

    Args:
        tool_input: 도구 입력 딕셔너리
        study_root: study 루트 Path 또는 None
        config: config 딕셔너리
        base_dir: 상대경로 해석 기준 디렉터리

    Returns:
        (차단 사유, 대상) 튜플. 문제 없으면 (None, None)
    """
    from gxpllm.core import classify_search_scope

    scope = tool_input.get('path') or tool_input.get('directory') or tool_input.get('cwd')
    reason = classify_search_scope(scope, study_root, config, base_dir)
    return (reason, scope or '(범위 미지정)') if reason else (None, None)


def record_block(study_root, tool_name, target, reason):
    """
    차단 사실을 감사 로그에 기록한다

    감사 기록 실패가 차단 동작을 방해해서는 안 되므로 예외를 삼킨다

    Args:
        study_root: study 루트 경로 (없으면 기록 생략)
        tool_name: 차단된 도구 이름
        target: 차단된 경로
        reason: 차단 사유
    """
    if not study_root:
        return
    try:
        from gxpllm.core import append_audit
        append_audit(study_root, {
            'event': 'access_blocked',
            'tool': tool_name,
            'target': str(target)[:500],
            'reason': reason,
            'hook': 'guard_file_access',
        })
    except Exception:
        pass


def block(study_root, tool_name, target, reason):
    """
    차단하고 종료한다

    Args:
        study_root: study 루트 경로
        tool_name: 도구 이름
        target: 차단된 경로
        reason: 차단 사유
    """
    record_block(study_root, tool_name, target, reason)
    print(
        f"[gxpllm-guard] 접근 차단\n"
        f"  도구: {tool_name}\n"
        f"  대상: {target}\n"
        f"  사유: {reason}\n"
        f"{GUIDANCE}",
        file=sys.stderr,
    )
    sys.exit(EXIT_BLOCK)


def main():
    """메인 함수"""
    # --- 입력 파싱 ---------------------------------------------------------
    try:
        payload = read_hook_payload()
    except Exception as exc:
        print(f"[gxpllm-guard] hook 입력을 해석할 수 없어 차단합니다: {exc}", file=sys.stderr)
        sys.exit(EXIT_BLOCK)

    # --- 판정 --------------------------------------------------------------
    try:
        from gxpllm.core import find_study_root, classify_path

        tool_name = payload.get('tool_name', '(unknown)')
        tool_input = payload.get('tool_input') or {}
        cwd = payload.get('cwd') or os.getcwd()

        # Bash 는 guard_bash.py 가 담당한다
        if str(tool_name).lower() in ('bash', 'shell', 'powershell'):
            sys.exit(EXIT_ALLOW)

        study_root, config = find_study_root(cwd)
        skip_keys = NON_PATH_KEYS
        mode = 'write' if is_write_tool(tool_name) else 'read'

        # 1. 검색 도구는 범위를 허용 디렉터리로 명시해야 한다
        if is_search_tool(tool_name):
            reason, scope = check_search_scope(tool_input, study_root, config, cwd)
            if reason:
                block(study_root, tool_name, scope, reason)
            # 검색 패턴(**/*.sas 등)은 경로가 아니므로 판정에서 제외한다
            skip_keys = NON_PATH_KEYS | SEARCH_NON_PATH_KEYS

        # 2. tool_input 전체에서 경로 후보를 찾아 각각 판정한다
        for candidate in collect_path_candidates(tool_input, skip_keys=skip_keys):
            reason = classify_path(candidate, study_root, config,
                                   base_dir=cwd, mode=mode)
            if reason:
                block(study_root, tool_name, candidate, reason)

        sys.exit(EXIT_ALLOW)

    except SystemExit:
        raise
    except Exception as exc:
        # fail-closed: 판정 불가 시 차단
        print(
            f"[gxpllm-guard] 내부 오류로 차단합니다 (fail-closed): "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        sys.exit(EXIT_BLOCK)


if __name__ == "__main__":
    main()
