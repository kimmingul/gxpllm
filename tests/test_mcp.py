"""
MCP 서버 프로토콜 검증 테스트

local_coder_server.py 가 MCP stdio 프로토콜에 맞게 응답하는지 확인한다.
LLM 실제 호출은 하지 않고, 프로토콜 계층과 도구 스펙만 검증한다.

검증 항목
- initialize / tools/list / tools/call 응답 형식
- 노출 도구 목록 (파일/셸 도구가 없어야 한다)
- 알 수 없는 도구 처리
- LLM 연결 실패 시 오류를 isError 로 반환하는가 (크래시 금지)

실행:
    python tests/test_mcp.py
"""

import json
import subprocess
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SERVER = PLUGIN_ROOT / 'mcp' / 'local_coder_server.py'

# ============================================================================
# 설정 영역 - 여기서 수정하세요
# ============================================================================

EXPECTED_TOOLS = {'write_program', 'revise_program', 'profile_data', 'structure_text'}

# 절대 노출되면 안 되는 도구 이름 패턴
FORBIDDEN_TOOL_MARKERS = (
    'read', 'write_file', 'shell', 'bash', 'exec', 'sql',
    'fetch', 'url', 'credential', 'file',
)

# 존재하지 않는 endpoint (연결 실패 경로 검증용)
UNREACHABLE_ENDPOINT = 'http://127.0.0.1:59999/v1'


# ============================================================================
# 메인 로직
# ============================================================================

def run_session(requests, env_extra=None):
    """
    MCP 서버에 요청을 보내고 응답을 수집한다

    Args:
        requests: 보낼 JSON-RPC 요청 리스트
        env_extra: 추가 환경변수 딕셔너리

    Returns:
        응답 딕셔너리 리스트
    """
    import os

    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    if env_extra:
        env.update(env_extra)

    payload = '\n'.join(json.dumps(r, ensure_ascii=False) for r in requests) + '\n'

    result = subprocess.run(
        [sys.executable, str(SERVER)],
        input=payload.encode('utf-8'),
        capture_output=True,
        timeout=120,
        env=env,
    )

    responses = []
    for line in (result.stdout or b'').decode('utf-8', errors='replace').splitlines():
        line = line.strip()
        if line:
            try:
                responses.append(json.loads(line))
            except ValueError:
                pass

    return responses


def test_initialize():
    """initialize 응답 형식을 검증한다"""
    print("\n[1/5] initialize 응답 검증...")
    problems = []

    responses = run_session([
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
    ])

    if not responses:
        return ['initialize 응답이 없습니다']

    result = responses[0].get('result') or {}
    if not result.get('protocolVersion'):
        problems.append('protocolVersion 없음')
    if 'tools' not in (result.get('capabilities') or {}):
        problems.append('capabilities.tools 없음')
    if not (result.get('serverInfo') or {}).get('name'):
        problems.append('serverInfo.name 없음')

    for item in problems:
        print(f"  FAIL {item}")
    if not problems:
        print(f"  OK   protocolVersion={result['protocolVersion']}, "
              f"server={result['serverInfo']['name']}")

    return problems


def test_tools_list():
    """도구 목록과 스펙을 검증한다"""
    print("\n[2/5] tools/list 검증...")
    problems = []

    responses = run_session([
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}},
    ])

    tools_response = next((r for r in responses if r.get('id') == 2), None)
    if tools_response is None:
        return ['tools/list 응답이 없습니다']

    tools = (tools_response.get('result') or {}).get('tools') or []
    names = {t.get('name') for t in tools}

    missing = EXPECTED_TOOLS - names
    if missing:
        problems.append(f"누락된 도구: {', '.join(sorted(missing))}")

    extra = names - EXPECTED_TOOLS
    if extra:
        problems.append(f"예상 밖 도구: {', '.join(sorted(extra))}")

    for tool in tools:
        if not tool.get('description'):
            problems.append(f"{tool.get('name')} 에 description 없음")
        schema = tool.get('inputSchema') or {}
        if schema.get('type') != 'object':
            problems.append(f"{tool.get('name')} 의 inputSchema 가 object 가 아님")
        if 'properties' not in schema:
            problems.append(f"{tool.get('name')} 의 inputSchema 에 properties 없음")

    print(f"  도구 {len(tools):,}개: {', '.join(sorted(names))}")
    for item in problems:
        print(f"  FAIL {item}")
    if not problems:
        print(f"  OK   모든 도구 스펙 정상")

    return problems


def test_no_dangerous_tools():
    """파일/셸 계열 도구가 노출되지 않는지 검증한다"""
    print("\n[3/5] 위험 도구 미노출 검증...")
    problems = []

    responses = run_session([
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}},
    ])

    tools_response = next((r for r in responses if r.get('id') == 2), None)
    tools = (tools_response.get('result') or {}).get('tools') or []

    for tool in tools:
        name = str(tool.get('name', '')).lower()
        for marker in FORBIDDEN_TOOL_MARKERS:
            # write_program 은 허용 (파일을 쓰는 것이 아니라 코드를 생성)
            if marker in name and name in EXPECTED_TOOLS:
                continue
            if marker in name and name not in EXPECTED_TOOLS:
                problems.append(f"위험 도구 노출 의심: {name} (패턴: {marker})")

    for item in problems:
        print(f"  FAIL {item}")
    if not problems:
        print(f"  OK   파일/셸/SQL/URL 도구 없음")

    return problems


def test_unknown_tool():
    """알 수 없는 도구 호출 처리를 검증한다"""
    print("\n[4/5] 알 수 없는 도구 처리...")
    problems = []

    responses = run_session([
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
         'params': {'name': 'read_file', 'arguments': {'path': 'data/adsl.sas7bdat'}}},
    ])

    call_response = next((r for r in responses if r.get('id') == 2), None)
    if call_response is None:
        return ['tools/call 응답이 없습니다']

    if 'error' not in call_response:
        problems.append('알 수 없는 도구인데 오류를 반환하지 않았습니다')

    for item in problems:
        print(f"  FAIL {item}")
    if not problems:
        print(f"  OK   error 반환: {call_response['error']['message']}")

    return problems


def test_llm_failure_handling():
    """LLM 연결 실패 시 크래시하지 않고 isError 로 반환하는지 검증한다"""
    print("\n[5/5] LLM 연결 실패 처리...")
    problems = []

    responses = run_session(
        [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
             'params': {'name': 'write_program',
                        'arguments': {'language': 'sas', 'program_name': 't.sas',
                                      'purpose': 'test'}}},
        ],
        env_extra={'GXPLLM_ENDPOINT': UNREACHABLE_ENDPOINT},
    )

    call_response = next((r for r in responses if r.get('id') == 2), None)
    if call_response is None:
        return ['LLM 연결 실패 시 응답이 없습니다 (서버가 죽었을 가능성)']

    result = call_response.get('result') or {}
    if not result.get('isError'):
        problems.append('연결 실패인데 isError 가 아닙니다')

    content = (result.get('content') or [{}])[0].get('text', '')
    if 'vLLM' not in content and '호출 실패' not in content:
        problems.append(f'오류 메시지가 불충분합니다: {content[:100]}')

    for item in problems:
        print(f"  FAIL {item}")
    if not problems:
        print(f"  OK   isError 반환: {content[:80]}")

    return problems


def main():
    """메인 함수"""
    print("=" * 80)
    print("MCP 서버 프로토콜 검증")
    print("=" * 80)

    all_problems = []
    for test_fn in (test_initialize, test_tools_list, test_no_dangerous_tools,
                    test_unknown_tool, test_llm_failure_handling):
        try:
            all_problems.extend(test_fn())
        except Exception as exc:
            all_problems.append(f"{test_fn.__name__} 예외: {type(exc).__name__}: {exc}")
            print(f"  FAIL 예외: {exc}")

    print(f"\n{'=' * 80}")
    if all_problems:
        print(f"실패: {len(all_problems):,}건")
        for item in all_problems:
            print(f"  - {item}")
    else:
        print("모든 검증 통과")
    print("=" * 80)

    sys.exit(1 if all_problems else 0)


if __name__ == "__main__":
    main()
