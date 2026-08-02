"""
LLM 경로 end-to-end 검증 (모의 서버)

DGX Spark 의 실제 vLLM 없이 MCP 서버의 전체 경로를 검증한다.

    MCP 도구 호출 -> HTTP 요청 구성 -> 응답 파싱 -> 코드 펜스 제거 -> 반환

test_mcp.py 는 프로토콜 계층만 본다 (도구 목록, 오류 처리).
이 테스트는 **실제 HTTP 왕복**을 포함해 다음을 확인한다.

  1. 요청 본문이 OpenAI 호환 형식인가 (model, messages, max_tokens, temperature)
  2. 시스템 프롬프트에 임상 규약이 들어가는가
  3. 마크다운 코드 펜스가 제거되는가
  4. structure_text 가 JSON Schema 를 response_format 으로 전달하는가
  5. 서버 오류 / 형식 오류 / 타임아웃에서 크래시하지 않는가
  6. verify_environment.py 의 LLM 검증이 실제로 동작하는가
  7. **잘린 응답(finish_reason=length)과 빈 본문을 거부하는가**
     추론 모델은 추론과 본문이 같은 토큰 예산을 나눠 쓴다.
     잘린 소스를 정상 반환하면 assertion 이 빠진 프로그램이 저장된다.

**검증하는 것은 gxpllm 쪽 코드 경로이지 LLM 품질이 아니다.**
LLM 품질은 scripts/benchmark_codegen.py 로 실측한다.

실행:
    python tests/test_llm_path.py
"""

import json
import os
import subprocess
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))
if str(PLUGIN_ROOT / 'tests') not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT / 'tests'))

import mock_vllm_server  # noqa: E402

# ============================================================================
# 설정 영역 - 여기서 수정하세요
# ============================================================================

MCP_SERVER = PLUGIN_ROOT / 'mcp' / 'local_coder_server.py'
PORT = 18017
ENDPOINT = f'http://127.0.0.1:{PORT}/v1'
CALL_TIMEOUT_SEC = 90

# 요청 본문에 반드시 있어야 할 키
REQUIRED_REQUEST_KEYS = ('model', 'messages', 'max_tokens', 'temperature')

# 시스템 프롬프트에 반드시 들어가야 할 임상 규약
REQUIRED_PROMPT_MARKERS = (
    '계산은 코드가 한다',
    'GXPLLM-META',
    '다대다 병합',
    '하드코딩',
)


# ============================================================================
# 유틸
# ============================================================================

def call_tool(tool_name, arguments, scenario='normal'):
    """
    MCP 서버의 도구를 호출한다

    Args:
        tool_name: 호출할 도구 이름
        arguments: 도구 인자 딕셔너리
        scenario: 모의 서버 시나리오

    Returns:
        (성공 여부, 반환 텍스트, 모의 서버가 받은 요청 리스트)
    """
    mock_vllm_server.MockHandler.scenario = scenario
    mock_vllm_server.MockHandler.request_log = []

    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['GXPLLM_ENDPOINT'] = ENDPOINT
    env['GXPLLM_MODEL'] = mock_vllm_server.MODEL_ID

    requests = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
         'params': {'name': tool_name, 'arguments': arguments}},
    ]
    payload = '\n'.join(json.dumps(r, ensure_ascii=False) for r in requests) + '\n'

    try:
        completed = subprocess.run(
            [sys.executable, str(MCP_SERVER)],
            input=payload.encode('utf-8'),
            capture_output=True, timeout=CALL_TIMEOUT_SEC, env=env,
        )
    except subprocess.TimeoutExpired:
        return False, '(타임아웃)', list(mock_vllm_server.MockHandler.request_log)

    text, is_error = '', True
    for line in (completed.stdout or b'').decode('utf-8', errors='replace').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            response = json.loads(line)
        except ValueError:
            continue
        if response.get('id') != 2:
            continue
        result = response.get('result') or {}
        text = (result.get('content') or [{}])[0].get('text', '')
        is_error = bool(result.get('isError'))

    return (not is_error), text, list(mock_vllm_server.MockHandler.request_log)


# ============================================================================
# 검증
# ============================================================================

def test_request_format():
    """요청 본문이 OpenAI 호환 형식인지 검증한다"""
    print("\n[1/7] 요청 본문 형식...")
    problems = []

    ok, text, requests = call_tool('write_program', {
        'language': 'sas', 'program_name': 't_x.sas', 'purpose': '검증',
        'table_shell': 'Table 14.1.1', 'inputs': ['data/derived/adsl.sas7bdat'],
        'outputs': ['output/tables/t_x.rtf'],
    })

    if not requests:
        return ['LLM 요청이 전송되지 않았습니다']

    request = requests[0]
    for key in REQUIRED_REQUEST_KEYS:
        if key not in request:
            problems.append(f"요청에 '{key}' 없음")

    if request.get('model') != mock_vllm_server.MODEL_ID:
        problems.append(f"model 불일치: {request.get('model')}")

    messages = request.get('messages', [])
    if len(messages) < 2:
        problems.append(f"messages 가 {len(messages)}개 (system + user 필요)")
    elif messages[0].get('role') != 'system':
        problems.append(f"첫 메시지 role 이 system 이 아님: {messages[0].get('role')}")

    for item in problems:
        print(f"  FAIL {item}")
    if not problems:
        print(f"  OK   model / messages / max_tokens / temperature 정상")

    return problems


def test_system_prompt():
    """시스템 프롬프트에 임상 규약이 들어가는지 검증한다"""
    print("\n[2/7] 시스템 프롬프트 임상 규약...")
    problems = []

    _, _, requests = call_tool('write_program', {
        'language': 'sas', 'program_name': 't_x.sas', 'purpose': '검증',
    })

    if not requests:
        return ['LLM 요청이 전송되지 않았습니다']

    system = requests[0]['messages'][0].get('content', '')
    for marker in REQUIRED_PROMPT_MARKERS:
        if marker not in system:
            problems.append(f"시스템 프롬프트에 '{marker}' 없음")

    # 언어별 규약이 붙는지
    if 'gxpllm_assert' not in system:
        problems.append('시스템 프롬프트에 assertion 사용법 없음')

    for item in problems:
        print(f"  FAIL {item}")
    if not problems:
        print(f"  OK   임상 규약 {len(REQUIRED_PROMPT_MARKERS):,}종 + assertion 사용법 포함")

    return problems


def test_code_fence_stripped():
    """마크다운 코드 펜스가 제거되는지 검증한다"""
    print("\n[3/7] 코드 펜스 제거...")
    problems = []

    ok, text, _ = call_tool('write_program', {
        'language': 'sas', 'program_name': 't_x.sas', 'purpose': '검증',
    })

    if not ok:
        return [f'도구 호출 실패: {text[:150]}']

    if text.startswith('```') or text.rstrip().endswith('```'):
        problems.append('코드 펜스가 남아 있습니다')

    if 'GXPLLM-META-BEGIN' not in text:
        problems.append('GXPLLM-META 블록이 없습니다')

    if '%gxpllm_assert' not in text:
        problems.append('assertion 호출이 없습니다')

    for item in problems:
        print(f"  FAIL {item}")
    if not problems:
        print(f"  OK   펜스 제거됨, GXPLLM-META 및 assertion 포함 ({len(text):,}자)")

    return problems


def test_language_routing():
    """언어별로 다른 규약이 전달되는지 검증한다"""
    print("\n[4/7] 언어별 규약 분기...")
    problems = []

    markers = {
        'sas':    'libname',
        'python': 'pandas merge',
        'r':      'dplyr join',
    }

    for language, marker in markers.items():
        _, _, requests = call_tool('write_program', {
            'language': language, 'program_name': f't_x.{language}', 'purpose': '검증',
        })
        if not requests:
            problems.append(f'{language}: 요청 없음')
            continue

        system = requests[0]['messages'][0].get('content', '')
        if marker not in system:
            problems.append(f"{language} 규약에 '{marker}' 없음")
        else:
            print(f"  OK   {language} — '{marker}' 포함")

    for item in problems:
        print(f"  FAIL {item}")

    return problems


def test_structure_text_schema():
    """structure_text 가 JSON Schema 를 response_format 으로 전달하는지 검증한다"""
    print("\n[5/7] structure_text 구조화 출력...")
    problems = []

    schema = {
        'type': 'object',
        'properties': {'symptoms': {'type': 'array'}},
        'required': ['symptoms'],
    }

    ok, text, requests = call_tool('structure_text', {
        'text': '투여 12일차에 경한 두통을 호소하였다.',
        'output_schema': schema,
        'code_candidates': ['두통', '어지러움'],
    })

    if not requests:
        return ['LLM 요청이 전송되지 않았습니다']

    request = requests[0]
    response_format = request.get('response_format')
    if not response_format:
        problems.append('response_format 이 전달되지 않았습니다')
    elif response_format.get('type') != 'json_schema':
        problems.append(f"response_format.type 이 json_schema 가 아님: {response_format.get('type')}")

    if request.get('temperature') != 0.0:
        problems.append(f"structure_text 의 temperature 가 0 이 아님: {request.get('temperature')}")

    system = request['messages'][0].get('content', '')
    for marker in ('부정 표현', '가족력', 'evidence span'):
        if marker not in system:
            problems.append(f"정형화 프롬프트에 '{marker}' 없음")

    if ok and text:
        try:
            parsed = json.loads(text)
            if 'symptoms' not in parsed:
                problems.append('응답에 symptoms 없음')
            else:
                evidence = (parsed['symptoms'][0] or {}).get('evidence')
                if not evidence:
                    problems.append('추출 결과에 evidence span 없음')
        except ValueError:
            problems.append('응답이 JSON 이 아닙니다')

    for item in problems:
        print(f"  FAIL {item}")
    if not problems:
        print(f"  OK   json_schema 전달, temperature=0, evidence span 포함")

    return problems


def test_failure_handling():
    """서버 오류와 형식 오류에서 크래시하지 않는지 검증한다"""
    print("\n[6/7] 오류 처리...")
    problems = []

    for scenario, description in (('error', '서버 500'), ('malformed', '형식 오류')):
        ok, text, _ = call_tool('write_program', {
            'language': 'sas', 'program_name': 't_x.sas', 'purpose': '검증',
        }, scenario=scenario)

        if ok:
            problems.append(f'{description}: 오류인데 성공으로 반환')
        elif not text or '오류' not in text:
            problems.append(f'{description}: 오류 메시지가 불충분 ({text[:80]})')
        else:
            print(f"  OK   {description} — isError 반환: {text[:70]}")

    for item in problems:
        print(f"  FAIL {item}")

    return problems


def test_truncation_rejected():
    """잘린 응답과 빈 본문을 거부하는지 검증한다

    추론 모델(Qwen3.6)은 추론과 본문이 같은 max_tokens 예산을 나눠 쓴다.
    실측(2026-08-02): reasoning 26,023자 / content 859자로 8,192 토큰을
    전부 소진하고 finish_reason=length.

    이때 잘린 소스를 정상 반환하면 assertion 호출이 통째로 빠진 프로그램이
    저장되고, runner 는 남은 assertion 만 보고 PASSED 를 낸다.
    **검증 장치가 사라진 것을 아무도 모르게 된다.**
    """
    print("\n[7/7] 잘린 응답 / 빈 본문 거부...")
    problems = []

    cases = (
        ('truncated',     'finish_reason=length',  ('write_program', {
            'language': 'sas', 'program_name': 't_x.sas', 'purpose': '검증'})),
        ('empty_content', 'content 빈 문자열',      ('write_program', {
            'language': 'sas', 'program_name': 't_x.sas', 'purpose': '검증'})),
        ('null_content',  'content null',          ('write_program', {
            'language': 'sas', 'program_name': 't_x.sas', 'purpose': '검증'})),
        ('truncated',     'revise 잘림',            ('revise_program', {
            'language': 'sas', 'source': 'proc print; run;'})),
    )

    for scenario, description, (tool, arguments) in cases:
        ok, text, _ = call_tool(tool, arguments, scenario=scenario)

        if ok:
            problems.append(
                f'{description}: 거부하지 않고 {len(text):,}자를 정상 반환했습니다 — '
                f'잘린 프로그램이 저장됩니다'
            )
        elif '오류' not in text:
            problems.append(f'{description}: 오류 메시지가 불충분 ({text[:80]})')
        else:
            print(f"  OK   {description} — isError 반환")

    # 정형화 응답의 JSON 완결성
    ok, text, _ = call_tool('structure_text', {
        'text': '경한 두통을 호소하였다.',
        'output_schema': {'type': 'object', 'properties': {'symptoms': {'type': 'array'}}},
    }, scenario='broken_json')

    if ok:
        problems.append(f'깨진 JSON: 거부하지 않고 반환했습니다 ({text[:60]})')
    else:
        print(f"  OK   깨진 JSON — isError 반환")

    for item in problems:
        print(f"  FAIL {item}")

    return problems


def main():
    """메인 함수"""
    print("=" * 80)
    print("LLM 경로 end-to-end 검증 (모의 서버)")
    print("=" * 80)

    server, _ = mock_vllm_server.start_server(PORT)
    print(f"\n  모의 서버: {ENDPOINT}")

    all_problems = []
    try:
        for test_fn in (test_request_format, test_system_prompt, test_code_fence_stripped,
                        test_language_routing, test_structure_text_schema,
                        test_failure_handling, test_truncation_rejected):
            try:
                all_problems.extend(test_fn())
            except Exception as exc:
                all_problems.append(f"{test_fn.__name__} 예외: {type(exc).__name__}: {exc}")
                print(f"  FAIL 예외: {exc}")
    finally:
        server.shutdown()

    print(f"\n{'=' * 80}")
    if all_problems:
        print(f"실패: {len(all_problems):,}건")
        for item in all_problems:
            print(f"  - {item}")
    else:
        print("모든 검증 통과 — MCP -> HTTP -> 파싱 경로가 정상 동작합니다")
        print("\n  주의: 이것은 gxpllm 코드 경로 검증입니다.")
        print("        실제 LLM 품질은 scripts/benchmark_codegen.py 로 실측하십시오.")
    print("=" * 80)

    sys.exit(1 if all_problems else 0)


if __name__ == "__main__":
    main()
