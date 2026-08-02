"""
실제 로컬 LLM 서버 연동 검증 (live)

`.mcp.json` 에 설정된 **실제** 서버에 붙어 MCP 경로 전체를 왕복시킨다.
tests/test_llm_path.py 가 모의 서버로 보는 것과 같은 경로를, 진짜 모델로 본다.

  MCP 도구 호출 -> HTTP -> 실제 추론 -> 응답 파싱 -> 코드 펜스 제거 -> 반환

**run_all.py 에 넣지 않는다.**
AGENTS.md 규칙 6: 전체 테스트는 SAS / R / 라이브 LLM 없이 통과해야 한다.
이 테스트는 서버가 있는 PC 에서 사람이 직접 실행한다.

판정 구분
  FAIL  gxpllm 코드 경로 문제. 반드시 고쳐야 한다.
  WARN  모델이 규약을 덜 지킴. 품질 문제는 benchmark_codegen.py 로 실측한다.

실행:
    python tests/test_live_llm.py
    python tests/test_live_llm.py --endpoint http://192.168.0.124:80/v1 --model Qwen3.6-35B-A3B
    python tests/test_live_llm.py --skip-slow
    python tests/test_live_llm.py --output live_llm_report.json
"""

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

# ============================================================================
# 설정 영역 - 여기서 수정하세요
# ============================================================================

MCP_CONFIG = PLUGIN_ROOT / '.mcp.json'
MCP_SERVER = PLUGIN_ROOT / 'mcp' / 'local_coder_server.py'
MCP_SERVER_NAME = 'local-coder'

CONNECT_TIMEOUT_SEC = 20
GENERATE_TIMEOUT_SEC = 900

# 추론 모델은 max_tokens 를 추론과 본문이 나눠 쓴다.
# 이 비율을 넘으면 잘릴 위험이 있다고 본다.
TOKEN_HEADROOM_WARN_RATIO = 0.7

# truncation 을 강제로 일으킬 토큰 수 (추론만으로도 소진되는 크기)
TRUNCATION_MAX_TOKENS = 64

# 언어별 assertion 호출 표지
ASSERTION_MARKERS = {
    'sas':    ('%gxpllm_assert',),
    'python': ('na.assert', 'gxpllm_assert'),
    'r':      ('gxpllm_assert',),
}

# 추론 텍스트가 코드로 새어나온 흔적
REASONING_LEAK_MARKERS = (
    '<think>', '</think>',
    '**Final Output', 'Let me think', "Here's the",
    '1.  **', '**Step 1',
)

# 정형화 검증용 임상 문구 — 부정 표현과 가족력 귀속을 함께 넣는다
STRUCTURE_TEXT_SAMPLE = (
    '투여 12일차에 경한 두통을 호소하였다. 발열은 부인하였다. '
    '부친이 심근경색 병력 있음.'
)

STRUCTURE_TEXT_SCHEMA = {
    'type': 'object',
    'properties': {
        'symptoms': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'term': {'type': 'string'},
                    'present': {'type': 'boolean'},
                    'subject': {'type': 'string'},
                    'evidence': {'type': 'string'},
                },
                'required': ['term', 'present', 'evidence'],
            },
        },
    },
    'required': ['symptoms'],
}


# ============================================================================
# 결과 누적
# ============================================================================

class Report:
    """검증 결과 누적기"""

    def __init__(self):
        self.items = []

    def add(self, name, status, detail=''):
        """
        결과를 추가한다

        Args:
            name: 항목 이름
            status: 'PASS' / 'FAIL' / 'WARN' / 'SKIP'
            detail: 상세 설명
        """
        self.items.append({'name': name, 'status': status, 'detail': detail})
        print(f"  {status:<4} {name}" + (f"  — {detail}" if detail else ''))

    def check(self, name, ok, detail='', fail_detail=None, warn_only=False):
        """
        조건을 판정해 기록한다

        이 결과는 CSV 문서에 첨부되는 검증 증적이므로, 통과한 항목에
        실패 안내 문구가 붙으면 안 된다. 조치 안내가 필요한 설명은
        fail_detail 로 분리한다.

        Args:
            name: 항목 이름
            ok: 통과 여부
            detail: 통과 / 실패 공통 설명
            fail_detail: 실패일 때만 쓸 설명 (없으면 detail 을 쓴다)
            warn_only: 실패해도 WARN 으로 처리할지

        Returns:
            ok 를 그대로 돌려준다
        """
        if ok:
            self.add(name, 'PASS', detail)
        else:
            self.add(name, 'WARN' if warn_only else 'FAIL',
                     detail if fail_detail is None else fail_detail)
        return ok

    def count(self, status):
        """
        상태별 건수를 센다

        Args:
            status: 셀 상태 문자열

        Returns:
            건수
        """
        return sum(1 for i in self.items if i['status'] == status)

    @property
    def failures(self):
        """FAIL 항목 리스트"""
        return [i for i in self.items if i['status'] == 'FAIL']

    @property
    def warnings(self):
        """WARN 항목 리스트"""
        return [i for i in self.items if i['status'] == 'WARN']


# ============================================================================
# 설정 해석
# ============================================================================

def expand_placeholder(value, name):
    """
    .mcp.json 의 ${VAR} / ${VAR:-default} 를 해석한다

    Claude Code 가 실제 실행 시 하는 확장을 테스트에서도 재현한다.
    변수가 없고 기본값도 없으면 None 을 돌려준다.

    Args:
        value: .mcp.json 에 적힌 원본 문자열
        name: 참고용 키 이름 (진단 메시지용)

    Returns:
        해석된 문자열. 값을 정할 수 없으면 None
    """
    if not isinstance(value, str) or not value.startswith('${'):
        return value or None

    body = value[2:-1] if value.endswith('}') else value[2:]
    if ':-' in body:
        var_name, default = body.split(':-', 1)
    else:
        var_name, default = body, ''

    return (os.environ.get(var_name.strip()) or default).strip() or None


def resolve_target(endpoint_arg, model_arg):
    """
    검증 대상 endpoint 와 모델을 정한다

    우선순위: 명령행 인자 > 환경변수 > .mcp.json (${VAR} 확장 포함)

    Args:
        endpoint_arg: --endpoint 값 또는 None
        model_arg: --model 값 또는 None

    Returns:
        (endpoint, model, 출처 설명) 튜플
    """
    source = '명령행 인자'
    endpoint = endpoint_arg
    model = model_arg

    if not endpoint or not model:
        env_endpoint = os.environ.get('GXPLLM_ENDPOINT')
        env_model = os.environ.get('GXPLLM_MODEL')
        if env_endpoint or env_model:
            source = '환경변수'
            endpoint = endpoint or env_endpoint
            model = model or env_model

    if not endpoint or not model:
        try:
            with open(MCP_CONFIG, encoding='utf-8') as f:
                servers = json.load(f).get('mcpServers', {})
            env = (servers.get(MCP_SERVER_NAME) or {}).get('env', {})
        except (OSError, ValueError):
            env = {}
        if env:
            source = '.mcp.json (${VAR} 확장)'
            endpoint = endpoint or expand_placeholder(
                env.get('GXPLLM_ENDPOINT'), 'GXPLLM_ENDPOINT')
            model = model or expand_placeholder(
                env.get('GXPLLM_MODEL'), 'GXPLLM_MODEL')

    return endpoint, model, source


def resolve_api_key():
    """
    인증 키를 정한다

    Returns:
        API key 문자열. 없으면 빈 문자열
    """
    value = (os.environ.get('GXPLLM_API_KEY') or '').strip()
    if value.startswith('${'):
        return ''
    return value


API_KEY = resolve_api_key()


def load_server_module(endpoint, model):
    """
    local_coder_server 를 in-process 로 불러온다

    ENDPOINT / MODEL 을 모듈 최상위에서 읽으므로 import 전에 환경변수를 세팅한다.
    MCP 왕복 없이 call_llm 을 직접 호출해 finish_reason 처리를 볼 때 쓴다.

    Args:
        endpoint: LLM endpoint
        model: 모델 이름

    Returns:
        불러온 모듈 객체
    """
    os.environ['GXPLLM_ENDPOINT'] = endpoint
    os.environ['GXPLLM_MODEL'] = model

    spec = importlib.util.spec_from_file_location('gxpllm_local_coder', MCP_SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ============================================================================
# 호출 유틸
# ============================================================================

def call_tool(tool_name, arguments, endpoint, model, timeout=GENERATE_TIMEOUT_SEC):
    """
    MCP 서버를 subprocess 로 띄워 도구를 호출한다

    Args:
        tool_name: 도구 이름
        arguments: 도구 인자 딕셔너리
        endpoint: LLM endpoint
        model: 모델 이름
        timeout: 제한 시간(초)

    Returns:
        (성공 여부, 반환 텍스트, 소요 초) 튜플
    """
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['GXPLLM_ENDPOINT'] = endpoint
    env['GXPLLM_MODEL'] = model
    env['GXPLLM_API_KEY'] = API_KEY

    requests = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
         'params': {'name': tool_name, 'arguments': arguments}},
    ]
    payload = '\n'.join(json.dumps(r, ensure_ascii=False) for r in requests) + '\n'

    started = time.time()
    try:
        completed = subprocess.run(
            [sys.executable, str(MCP_SERVER)],
            input=payload.encode('utf-8'),
            capture_output=True, timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        return False, f'(타임아웃 {timeout}초)', time.time() - started

    elapsed = time.time() - started
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

    return (not is_error), text, elapsed


def raw_chat(endpoint, model, messages, max_tokens, temperature=0.2):
    """
    endpoint 를 직접 호출해 응답 전문을 받는다

    usage 와 finish_reason 을 봐야 하는 검증에 쓴다.
    MCP 계층은 이 정보를 노출하지 않는다.

    Args:
        endpoint: LLM endpoint
        model: 모델 이름
        messages: chat 메시지 리스트
        max_tokens: 최대 생성 토큰
        temperature: 샘플링 온도

    Returns:
        응답 딕셔너리

    Raises:
        RuntimeError: 호출 실패 시
    """
    headers = {'Content-Type': 'application/json'}
    if API_KEY:
        headers['Authorization'] = f'Bearer {API_KEY}'

    request = urllib.request.Request(
        f"{endpoint.rstrip('/')}/chat/completions",
        data=json.dumps({
            'model': model, 'messages': messages,
            'max_tokens': max_tokens, 'temperature': temperature,
        }, ensure_ascii=False).encode('utf-8'),
        headers=headers,
        method='POST',
    )
    try:
        with urllib.request.urlopen(request, timeout=GENERATE_TIMEOUT_SEC) as response:
            return json.loads(response.read().decode('utf-8'))
    except (urllib.error.URLError, ValueError, OSError) as exc:
        raise RuntimeError(f'{type(exc).__name__}: {exc}') from exc


def find_leak(text):
    """
    코드에 추론 텍스트가 섞였는지 찾는다

    Args:
        text: 검사할 코드 문자열

    Returns:
        발견된 표지 리스트
    """
    return [marker for marker in REASONING_LEAK_MARKERS if marker in text]


# ============================================================================
# 검증
# ============================================================================

def check_connection(endpoint, model, report):
    """
    서버 연결과 모델 서빙 여부를 확인한다

    Args:
        endpoint: LLM endpoint
        model: 모델 이름
        report: Report 누적기

    Returns:
        계속 진행 가능하면 True
    """
    print(f"\n[1/6] 서버 연결...")

    models_request = urllib.request.Request(f"{endpoint.rstrip('/')}/models")
    if API_KEY:
        models_request.add_header('Authorization', f'Bearer {API_KEY}')

    try:
        started = time.time()
        with urllib.request.urlopen(models_request,
                                    timeout=CONNECT_TIMEOUT_SEC) as response:
            body = json.loads(response.read().decode('utf-8'))
        elapsed = time.time() - started
    except (urllib.error.URLError, ValueError, OSError) as exc:
        report.add('endpoint 연결', 'FAIL', f'{type(exc).__name__}: {exc}')
        print(f"       로컬 LLM 서버가 기동 중인지 확인하십시오: {endpoint}")
        return False

    served = [m.get('id') for m in body.get('data', [])]
    report.add('endpoint 연결', 'PASS', f'{elapsed:.2f}초, 모델 {len(served):,}개')
    return report.check(
        f'모델 {model} 서빙 목록에 존재', model in served,
        f"목록에 없음: {', '.join(str(s) for s in served)[:150]}" if model not in served else '',
    )


def check_write_program(endpoint, model, report, languages):
    """
    write_program 이 언어별로 규약을 지킨 코드를 내는지 확인한다

    Args:
        endpoint: LLM endpoint
        model: 모델 이름
        report: Report 누적기
        languages: 검증할 언어 리스트

    Returns:
        {언어: 생성 코드} 딕셔너리
    """
    print(f"\n[2/6] write_program 실제 생성...")
    generated = {}

    for language in languages:
        ok, text, elapsed = call_tool('write_program', {
            'language': language,
            'program_name': f'verify_live.{language}',
            'purpose': '안전성 집합 인구통계 요약',
            'sap_ref': 'SAP 14.1.1',
            'table_shell': 'Table 14.1.1 Demographics — Age(n, mean, sd), Sex(n, %) by ARM',
            'inputs': ['data/derived/adsl'],
            'outputs': ['output/tables/t_dm'],
            'analysis_set': 'SAF',
            'instructions': '짧게 작성하십시오.',
        }, endpoint, model)

        if not report.check(f'{language}: 호출 성공', ok, f'{elapsed:.1f}초  {text[:80]}'):
            continue

        generated[language] = text

        report.check(f'{language}: 빈 응답 아님', bool(text.strip()),
                     f'{len(text):,}자')
        report.check(f'{language}: 코드 펜스 제거됨',
                     not text.startswith('```') and not text.rstrip().endswith('```'))
        report.check(f'{language}: GXPLLM-META 헤더', 'GXPLLM-META-BEGIN' in text,
                     warn_only=True)

        markers = ASSERTION_MARKERS[language]
        report.check(f'{language}: assertion 호출',
                     any(m in text for m in markers),
                     f"기대 표지: {' 또는 '.join(markers)}", warn_only=True)

        leaks = find_leak(text)
        report.check(f'{language}: 추론 텍스트 누출 없음', not leaks,
                     f"발견: {', '.join(leaks)}" if leaks else '')

    return generated


def check_token_headroom(endpoint, model, report):
    """
    추론 토큰이 max_tokens 를 얼마나 잠식하는지 측정한다

    추론 모델은 max_tokens 를 추론과 본문이 나눠 쓴다.
    본문이 잘리면 잘린 프로그램이 정상처럼 반환된다.

    Args:
        endpoint: LLM endpoint
        model: 모델 이름
        report: Report 누적기
    """
    print(f"\n[3/6] 토큰 여유 측정...")

    module = load_server_module(endpoint, model)
    limit = module.DEFAULT_MAX_TOKENS

    messages = [
        {'role': 'system',
         'content': module.SYSTEM_PROMPT_BASE + '\n\n' + module.LANGUAGE_GUIDANCE['sas']},
        {'role': 'user',
         'content': 'ADSL 로 Table 14.1.1 인구통계 요약표를 만드는 SAS 프로그램을 작성하십시오. '
                    'ARM 별 Age(n, mean, sd, median, min, max)와 Sex(n, %)를 산출합니다.'},
    ]

    try:
        body = raw_chat(endpoint, model, messages, max_tokens=limit)
    except RuntimeError as exc:
        report.add('토큰 사용량 측정', 'FAIL', str(exc))
        return

    choice = (body.get('choices') or [{}])[0]
    usage = body.get('usage') or {}
    completion = usage.get('completion_tokens', 0)
    finish = choice.get('finish_reason')
    content = (choice.get('message') or {}).get('content') or ''
    reasoning = (choice.get('message') or {}).get('reasoning_content') or ''

    ratio = completion / limit if limit else 0
    report.add('토큰 사용량 측정', 'PASS',
               f'completion={completion:,} / max_tokens={limit:,} ({ratio:.0%}), '
               f'finish_reason={finish}')
    report.add('추론 / 본문 분리', 'PASS',
               f'reasoning={len(reasoning):,}자, content={len(content):,}자')

    report.check('실제 작업에서 잘리지 않음', finish != 'length',
                 f'finish_reason={finish}',
                 fail_detail=f'finish_reason={finish} — '
                             f'GXPLLM_MAX_TOKENS 를 올리거나 요청 범위를 줄이십시오')
    report.check(f'토큰 여유 {(1 - TOKEN_HEADROOM_WARN_RATIO):.0%} 이상 남음',
                 ratio < TOKEN_HEADROOM_WARN_RATIO,
                 f'{ratio:.0%} 사용',
                 fail_detail=f'{ratio:.0%} 사용 — 더 긴 프로그램에서 잘릴 수 있습니다',
                 warn_only=True)


def check_truncation_detected(endpoint, model, report):
    """
    본문이 잘렸을 때 call_llm 이 이를 알리는지 확인한다

    추론만으로 max_tokens 를 소진시켜 content 를 빈 문자열로 만든다.
    이때 조용히 반환하면 잘린 임상 분석 프로그램이 정상처럼 저장된다.

    Args:
        endpoint: LLM endpoint
        model: 모델 이름
        report: Report 누적기
    """
    print(f"\n[4/6] 잘린 응답 탐지...")

    module = load_server_module(endpoint, model)
    messages = [
        {'role': 'system', 'content': module.SYSTEM_PROMPT_BASE},
        {'role': 'user',
         'content': 'ADSL 로 인구통계 요약표를 만드는 SAS 프로그램을 작성하십시오.'},
    ]

    # 먼저 실제로 잘리는지 확인한다 (모델이 바뀌면 전제가 깨질 수 있다)
    try:
        body = raw_chat(endpoint, model, messages, max_tokens=TRUNCATION_MAX_TOKENS)
    except RuntimeError as exc:
        report.add('truncation 재현', 'SKIP', str(exc))
        return

    choice = (body.get('choices') or [{}])[0]
    if choice.get('finish_reason') != 'length':
        report.add('truncation 재현', 'SKIP',
                   f"max_tokens={TRUNCATION_MAX_TOKENS} 로도 잘리지 않음 "
                   f"(finish_reason={choice.get('finish_reason')})")
        return

    report.add('truncation 재현', 'PASS',
               f"max_tokens={TRUNCATION_MAX_TOKENS}, finish_reason=length, "
               f"content={len((choice.get('message') or {}).get('content') or ''):,}자")

    # call_llm 이 이 상황을 알리는가
    try:
        returned = module.call_llm(messages, max_tokens=TRUNCATION_MAX_TOKENS)
    except RuntimeError as exc:
        report.add('call_llm 이 잘림을 오류로 알림', 'PASS', str(exc)[:120])
        return

    report.check('call_llm 이 잘림을 오류로 알림', False,
                 f'예외 없이 {len(returned):,}자 반환 — '
                 f'잘린 프로그램이 정상처럼 저장됩니다')


def check_structure_text(endpoint, model, report):
    """
    structure_text 가 JSON 과 근거를 내는지 확인한다

    Args:
        endpoint: LLM endpoint
        model: 모델 이름
        report: Report 누적기
    """
    print(f"\n[5/6] structure_text 정형화...")

    ok, text, elapsed = call_tool('structure_text', {
        'text': STRUCTURE_TEXT_SAMPLE,
        'output_schema': STRUCTURE_TEXT_SCHEMA,
        'code_candidates': ['두통', '발열', '어지러움'],
    }, endpoint, model)

    if not report.check('호출 성공', ok, f'{elapsed:.1f}초  {text[:80]}'):
        return

    if not report.check('빈 응답 아님', bool(text.strip()), f'{len(text):,}자',
                        fail_detail='response_format 사용 시 추론이 토큰을 '
                                    '소진했을 수 있습니다'):
        return

    try:
        parsed = json.loads(text)
    except ValueError as exc:
        report.check('JSON 파싱', False, f'{exc}  본문: {text[:120]}')
        return

    report.check('JSON 파싱', True, '')

    symptoms = parsed.get('symptoms')
    if not report.check('symptoms 필드 존재', isinstance(symptoms, list) and symptoms,
                        f'{type(symptoms).__name__}'):
        return

    report.check('evidence span 포함',
                 all(s.get('evidence') for s in symptoms if isinstance(s, dict)),
                 warn_only=True)

    # 부정 표현: "발열은 부인하였다" 를 present=True 로 뽑으면 안 된다
    fever = [s for s in symptoms
             if isinstance(s, dict) and '발열' in str(s.get('term', ''))]
    report.check('부정 표현 처리 (발열 부인)',
                 all(s.get('present') is False for s in fever) if fever else True,
                 f'추출됨: {json.dumps(fever, ensure_ascii=False)[:120]}' if fever else '미추출',
                 warn_only=True)

    # 가족력 귀속: 부친의 심근경색은 피험자 증상이 아니다
    mi = [s for s in symptoms
          if isinstance(s, dict) and '심근경색' in str(s.get('term', ''))]
    report.check('가족력 귀속 (부친 심근경색)',
                 all(str(s.get('subject', '')).lower() not in ('subject', '피험자', '')
                     for s in mi) if mi else True,
                 f'추출됨: {json.dumps(mi, ensure_ascii=False)[:120]}' if mi else '미추출',
                 warn_only=True)


def check_error_handling(endpoint, model, report):
    """
    잘못된 모델명에서 isError 로 돌아오는지 확인한다

    Args:
        endpoint: LLM endpoint
        model: 모델 이름 (사용하지 않음, 시그니처 일관성 유지)
        report: Report 누적기
    """
    print(f"\n[6/6] 오류 처리...")

    ok, text, elapsed = call_tool('write_program', {
        'language': 'sas', 'program_name': 't_x.sas', 'purpose': '검증',
    }, endpoint, 'no-such-model-xyz', timeout=120)

    report.check('없는 모델명 → isError 반환', not ok,
                 f'{elapsed:.1f}초  {text[:120]}')
    report.check('오류 메시지에 endpoint 포함', endpoint in text or '오류' in text,
                 text[:120], warn_only=True)


# ============================================================================
# 메인 로직
# ============================================================================

def main():
    """메인 함수"""
    print("=" * 80)
    print("실제 로컬 LLM 서버 연동 검증 (live)")
    print("=" * 80)

    parser = argparse.ArgumentParser(description='실제 LLM 서버로 MCP 경로를 검증합니다')
    parser.add_argument('--endpoint', help='LLM endpoint 재정의')
    parser.add_argument('--model', help='모델명 재정의')
    parser.add_argument('--skip-slow', action='store_true',
                        help='write_program 을 sas 하나만 검증')
    parser.add_argument('--output', help='결과 JSON 저장 경로')
    args = parser.parse_args()

    endpoint, model, source = resolve_target(args.endpoint, args.model)
    if not endpoint or not model:
        print(f"\n오류: endpoint 또는 모델을 정할 수 없습니다")
        print(f"      {MCP_CONFIG} 의 mcpServers.{MCP_SERVER_NAME}.env 를 확인하거나")
        print(f"      --endpoint / --model 로 지정하십시오")
        sys.exit(2)

    print(f"\n  설정 출처 : {source}")
    print(f"  endpoint  : {endpoint}")
    print(f"  모델      : {model}")
    print(f"  인증      : {'GXPLLM_API_KEY 설정됨' if API_KEY else '없음'}")

    report = Report()
    started = time.time()

    if not check_connection(endpoint, model, report):
        print(f"\n{'=' * 80}")
        print("서버에 연결할 수 없어 중단합니다")
        print("=" * 80)
        sys.exit(1)

    languages = ['sas'] if args.skip_slow else ['sas', 'python', 'r']

    for check in (
        lambda: check_write_program(endpoint, model, report, languages),
        lambda: check_token_headroom(endpoint, model, report),
        lambda: check_truncation_detected(endpoint, model, report),
        lambda: check_structure_text(endpoint, model, report),
        lambda: check_error_handling(endpoint, model, report),
    ):
        try:
            check()
        except Exception as exc:
            report.add(f'{type(exc).__name__}', 'FAIL', str(exc)[:200])

    elapsed = time.time() - started

    # --- 요약 ----------------------------------------------------------------
    print(f"\n{'=' * 80}")
    print(f"결과: {report.count('PASS'):,}건 통과 / {len(report.failures):,}건 실패 / "
          f"{len(report.warnings):,}건 경고 / {report.count('SKIP'):,}건 건너뜀  "
          f"({elapsed:.0f}초)")
    print("=" * 80)

    if report.failures:
        print(f"\n실패 (gxpllm 코드 경로 문제 — 고쳐야 합니다):")
        for item in report.failures:
            print(f"  - {item['name']}")
            if item['detail']:
                print(f"      {item['detail']}")

    if report.warnings:
        print(f"\n경고 (모델이 규약을 덜 지킴 — benchmark_codegen.py 로 실측하십시오):")
        for item in report.warnings:
            print(f"  - {item['name']}")
            if item['detail']:
                print(f"      {item['detail']}")

    if not report.failures:
        print(f"\n  MCP -> 실제 LLM -> 파싱 경로가 정상 동작합니다.")
        print(f"  코드 생성 품질은 scripts/benchmark_codegen.py 로 별도 실측하십시오.")

    if args.output:
        payload = {
            'endpoint': endpoint, 'model': model, 'source': source,
            'elapsed_sec': round(elapsed, 1), 'items': report.items,
        }
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\n  결과 저장: {output_path}")

    sys.exit(1 if report.failures else 0)


if __name__ == "__main__":
    main()
