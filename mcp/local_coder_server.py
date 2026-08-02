"""
로컬 LLM 코드 생성 MCP 서버

DGX Spark 의 vLLM(Qwen3.6-35B-A3B)에 붙어 SAS / Python / R 코드를 생성한다.
Claude Code(Opus)는 이 서버의 도구만 호출할 수 있으며,
파일 읽기 / 셸 실행 / 임의 SQL 은 노출하지 않는다.

노출 도구
  write_program    : SAP table shell + Data Dictionary 로 분석 프로그램 생성
  revise_program   : assertion 실패 / 로그 스캔 결과를 근거로 프로그램 수정
  profile_data     : 데이터 프로파일링 프로그램 생성 (실행은 runner 담당)
  structure_text   : 비정형 임상 문구를 정형 데이터로 변환

노출하지 않는 것
  파일 읽기 / 쓰기, 셸 실행, 임의 SQL, URL fetch, 자격증명 조회

프로토콜: MCP stdio (JSON-RPC 2.0)
외부 의존성 없이 표준 라이브러리만 사용한다.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

# ============================================================================
# 설정 영역 - 여기서 수정하세요
# ============================================================================

SERVER_NAME = 'gxpllm-local-coder'
SERVER_VERSION = '0.1.0'
PROTOCOL_VERSION = '2024-11-05'

DEFAULT_ENDPOINT = 'http://dgx-spark.internal:8001/v1'
DEFAULT_MODEL = 'Qwen3.6-35B-A3B-NVFP4'
DEFAULT_TIMEOUT_SEC = 300
DEFAULT_TEMPERATURE = 0.2

ENDPOINT = os.environ.get('GXPLLM_ENDPOINT', DEFAULT_ENDPOINT)
MODEL = os.environ.get('GXPLLM_MODEL', DEFAULT_MODEL)

# 정상 완료로 인정하는 finish_reason — **allowlist 여야 한다**
#
# 'length' 만 거부하면 content_filter, 필드 누락, 프록시 고유 값이 통과한다.
# OpenAI 호환에서 정상 완료는 'stop' 뿐이다.
SUCCESS_FINISH_REASONS = frozenset({'stop'})

# 추론 모델은 추론과 본문이 **같은** 토큰 예산을 나눠 쓴다.
#
# 실측(Qwen3.6-35B-A3B, 2026-08-02): 인구통계 요약표 요청 하나에
# reasoning 26,023자 / content 859자, completion_tokens 8,192 로 max_tokens 를
# 전부 소진하고 finish_reason=length. 8192 로는 실제 업무 프로그램이 잘린다.
#
# 상향은 **완화책이지 안전장치가 아니다.** 잘림 자체는 아래 finish_reason
# 검사로 막는다.
FALLBACK_MAX_TOKENS = 32768

try:
    DEFAULT_MAX_TOKENS = int(os.environ.get('GXPLLM_MAX_TOKENS') or FALLBACK_MAX_TOKENS)
except ValueError:
    raise SystemExit(
        f"GXPLLM_MAX_TOKENS 를 숫자로 해석할 수 없습니다: "
        f"{os.environ.get('GXPLLM_MAX_TOKENS')!r}"
    )

if DEFAULT_MAX_TOKENS < 1:
    raise SystemExit(f"GXPLLM_MAX_TOKENS 는 1 이상이어야 합니다: {DEFAULT_MAX_TOKENS}")

LANGUAGE_LABELS = {
    'sas': 'SAS 9.4',
    'python': 'Python 3.12',
    'r': 'R',
}

META_TEMPLATES = {
    'sas': '''/*----------------------------------------------------------------------------
  GXPLLM-META-BEGIN
  program      : {program_name}
  purpose      : {purpose}
  sap_ref      : {sap_ref}
  inputs       : {inputs}
  outputs      : {outputs}
  analysis_set : {analysis_set}
  author       : local-llm/{model}
  GXPLLM-META-END
----------------------------------------------------------------------------*/''',
    'python': '''"""
{purpose}

GXPLLM-META-BEGIN
program      : {program_name}
purpose      : {purpose}
sap_ref      : {sap_ref}
inputs       : {inputs}
outputs      : {outputs}
analysis_set : {analysis_set}
author       : local-llm/{model}
GXPLLM-META-END
"""''',
    'r': '''# GXPLLM-META-BEGIN
# program      : {program_name}
# purpose      : {purpose}
# sap_ref      : {sap_ref}
# inputs       : {inputs}
# outputs      : {outputs}
# analysis_set : {analysis_set}
# author       : local-llm/{model}
# GXPLLM-META-END''',
}

SYSTEM_PROMPT_BASE = """당신은 제약회사 임상시험 데이터를 다루는 통계 프로그래머입니다.
SAP(통계분석계획서)의 table shell 과 Data Dictionary 를 근거로 분석 프로그램을 작성합니다.

반드시 지킬 것
1. 계산은 코드가 한다. 숫자를 직접 써넣지 않는다.
2. 프로그램 첫머리에 GXPLLM-META 블록을 둔다. inputs / outputs 를 정확히 선언한다.
3. 모든 데이터 변환 단계마다 gxpllm assertion 을 호출한다.
   - 행 수 변화 (필터, 병합)
   - key 유일성 (USUBJID, USUBJID+PARAMCD+AVISITN)
   - 분석군 flag 정합 (SAFFL, FASFL, PPROTFL)
   - 분모 검증
   - 정합성 관계식 (AE subject count <= 분모, arm 합 = 전체 합)
4. 병합 시 행이 늘어나는지 반드시 검증한다. 다대다 병합은 임상 데이터에서
   행 수가 조용히 늘어나는 가장 흔한 원인이다.
5. 결측 처리, baseline 정의, TEAE window 는 SAP 에 명시된 대로만 구현한다.
   SAP 에 없으면 임의로 정하지 말고 주석으로 확인 요청을 남긴다.
6. 코드 안에 실제 피험자 ID, 실제 수치, 자유기술 텍스트를 하드코딩하지 않는다.

출력 형식: 코드만 출력한다. 설명 문장이나 마크다운 코드 펜스를 붙이지 않는다."""

LANGUAGE_GUIDANCE = {
    'sas': """SAS 9.4 규약
- assertion 매크로: %include "&GXPLLM_PLUGIN_ROOT./macros/gxpllm_assert.sas";
  %gxpllm_assert_rowcount(ds, label=, expected_min=)
  %gxpllm_assert_rowcount_delta(ds_before, ds_after, label=, max_loss_rate=)
  %gxpllm_assert_unique(ds, keys=, label=)
  %gxpllm_assert_domain(ds, column=, allowed=, label=)
  %gxpllm_assert_analysis_set(ds, flag_column=, flag_value=Y, label=, expected_n=)
  %gxpllm_assert_denominator(ds, subject_column=USUBJID, denominator=, label=)
  %gxpllm_assert_le(actual, limit, label=, expr=)
- libname 은 access=readonly 로 연다
- MERGE 대신 PROC SQL join 을 쓸 때도 행 수 변화를 검증한다
- ODS 출력은 output/tables/ 또는 output/figures/ 에만 쓴다""",

    'python': """Python 규약
- assertion: sys.path 에 scripts 를 추가하고 import gxpllm_assert as na
  na.assert_rowcount(df, label=, expected_min=)
  na.assert_rowcount_delta(before, after, label=, max_loss_rate=)
  na.assert_join_loss(left, merged, key=, label=)
  na.assert_unique(df, keys=, label=)
  na.assert_domain(df, column=, allowed=, label=)
  na.assert_analysis_set(df, flag_column=, flag_value='Y', label=, expected_n=)
  na.assert_denominator(df, subject_column='USUBJID', denominator=, label=)
  na.assert_le(actual, limit, label=, expression=)
- 원본 데이터는 읽기만 한다
- pandas merge 는 반드시 validate= 를 지정한다 ('one_to_one', 'many_to_one' 등)
- 프로그램 끝에서 na.summary() 를 호출한다
- 사용자 스타일 가이드를 따른다: 설정 상수는 파일 상단 대문자,
  구분선 주석, [1/n] 진행 표시, 함수 docstring 에 Args/Returns""",

    'r': """R 규약
- assertion: source(file.path(Sys.getenv("GXPLLM_PLUGIN_ROOT"), "scripts", "gxpllm_assert.R"))
  gxpllm_assert_rowcount(df, label=, expected_min=)
  gxpllm_assert_rowcount_delta(before, after, label=, max_loss_rate=)
  gxpllm_assert_unique(df, keys=, label=)
  gxpllm_assert_domain(df, column=, allowed=, label=)
  gxpllm_assert_analysis_set(df, flag_column=, flag_value="Y", label=, expected_n=)
  gxpllm_assert_denominator(df, subject_column="USUBJID", denominator=, label=)
  gxpllm_assert_le(actual, limit, label=)
- runner 가 options(warn=), set.seed(), sessionInfo() 를 주입하므로 중복 설정하지 않는다
- dplyr join 은 relationship= 인자로 관계를 명시한다
- 프로그램 끝에서 gxpllm_assert_summary() 를 호출한다""",
}

STRUCTURE_TEXT_SYSTEM = """당신은 임상 데이터 관리자입니다.
자유기술 임상 기록을 정형 데이터로 변환합니다.

반드시 지킬 것
1. 원문에 없는 내용을 만들어내지 않는다. 확실하지 않으면 null 과 confidence 를 낮게 준다.
2. 추출한 모든 값에 근거 위치(evidence span)를 원문 문자 offset 으로 표시한다.
   근거를 댈 수 없으면 그 값을 추출하지 않는다.
3. 부정 표현을 정확히 처리한다.
   "두통 없음", "발열 부인함", "특이소견 없음" 은 증상 있음이 아니다.
4. 주체를 정확히 귀속한다.
   "부친이 심근경색 병력" 은 피험자의 이상반응이 아니라 가족력이다.
5. 불확실성과 시제를 구분한다.
   "의심됨", "R/O", "이전 방문 시", "호전 중" 을 확정 현재 소견으로 처리하지 않는다.
6. 코드(MedDRA PT 등)는 제공된 후보 목록에서만 고른다. 새로 만들지 않는다.

출력 형식: 지정된 JSON Schema 를 따르는 JSON 만 출력한다."""


# ============================================================================
# LLM 호출
# ============================================================================

def call_llm(messages, max_tokens=DEFAULT_MAX_TOKENS, temperature=DEFAULT_TEMPERATURE,
             response_format=None):
    """
    vLLM 의 OpenAI 호환 endpoint 를 호출한다

    Args:
        messages: chat completion 메시지 리스트
        max_tokens: 최대 생성 토큰 수
        temperature: 샘플링 온도
        response_format: 구조화 출력 스펙 (JSON Schema 등)

    Returns:
        생성된 텍스트 문자열

    Raises:
        RuntimeError: 호출 실패, 응답이 잘림, 본문이 비어 있는 경우
    """
    payload = {
        'model': MODEL,
        'messages': messages,
        'max_tokens': max_tokens,
        'temperature': temperature,
    }
    if response_format:
        payload['response_format'] = response_format

    request = urllib.request.Request(
        f"{ENDPOINT.rstrip('/')}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )

    try:
        with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT_SEC) as response:
            body = json.loads(response.read().decode('utf-8'))
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"로컬 LLM 호출 실패 ({ENDPOINT}): {exc}. "
            f"DGX Spark 의 vLLM 서비스가 기동 중인지 확인하십시오."
        ) from exc
    except (ValueError, KeyError) as exc:
        raise RuntimeError(f"로컬 LLM 응답을 해석할 수 없습니다: {exc}") from exc

    try:
        choice = body['choices'][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"로컬 LLM 응답 형식이 예상과 다릅니다: {body}") from exc

    finish_reason = choice.get('finish_reason')
    content = (choice.get('message') or {}).get('content')
    usage = body.get('usage') or {}

    budget = (
        f"finish_reason={finish_reason!r}, "
        f"completion_tokens={usage.get('completion_tokens')}, "
        f"max_tokens={max_tokens:,}"
    )

    # 잘린 응답을 정상으로 반환하면 assertion 이 통째로 빠진 프로그램이
    # 저장되고, runner 는 남은 assertion 만 보고 PASSED 를 낸다.
    if finish_reason not in SUCCESS_FINISH_REASONS:
        raise RuntimeError(
            f"로컬 LLM 응답이 정상 종료되지 않아 거부했습니다 ({budget}). "
            f"추론 모델은 추론과 본문이 같은 토큰 예산을 나눠 씁니다. "
            f"GXPLLM_MAX_TOKENS 를 올리거나 요청 범위를 줄이십시오. "
            f"**자동으로 토큰을 늘려 재시도하지 마십시오.**"
        )

    # content is None 은 strip_code_fence 에서 AttributeError 로 걸리지만
    # content == '' 는 조용히 통과한다. 두 경우를 같은 자리에서 막는다.
    if content is None or not str(content).strip():
        raise RuntimeError(
            f"로컬 LLM 이 본문 없이 응답했습니다 ({budget}). "
            f"추론만 반환되고 content 가 비었습니다."
        )

    return content


def strip_code_fence(text):
    """
    응답에서 마크다운 코드 펜스를 제거한다

    Args:
        text: 원본 응답 텍스트

    Returns:
        코드만 남은 문자열
    """
    stripped = text.strip()
    if not stripped.startswith('```'):
        return stripped

    lines = stripped.splitlines()
    if lines and lines[0].startswith('```'):
        lines = lines[1:]
    if lines and lines[-1].strip() == '```':
        lines = lines[:-1]
    return '\n'.join(lines).strip()


# ============================================================================
# 도구 구현
# ============================================================================

def tool_write_program(args):
    """
    SAP table shell 과 Data Dictionary 로 분석 프로그램을 생성한다

    Args:
        args: 도구 인자 딕셔너리

    Returns:
        생성된 프로그램 소스 문자열
    """
    language = str(args.get('language', 'sas')).lower()
    if language not in LANGUAGE_LABELS:
        raise ValueError(f"language 는 sas / python / r 중 하나여야 합니다 (입력: {language})")

    meta = META_TEMPLATES[language].format(
        program_name=args.get('program_name', 'unnamed'),
        purpose=args.get('purpose', ''),
        sap_ref=args.get('sap_ref', ''),
        inputs=', '.join(args.get('inputs', [])),
        outputs=', '.join(args.get('outputs', [])),
        analysis_set=args.get('analysis_set', ''),
        model=MODEL,
    )

    user_prompt = f"""다음 명세로 {LANGUAGE_LABELS[language]} 분석 프로그램을 작성하십시오.

## 프로그램 헤더 (그대로 사용)
{meta}

## SAP table shell
{args.get('table_shell', '(미제공)')}

## Data Dictionary
{args.get('data_dictionary', '(미제공)')}

## assertion 명세
{args.get('assertions_spec', '(미제공)')}

## 추가 지시
{args.get('instructions', '(없음)')}
"""

    messages = [
        {'role': 'system', 'content': SYSTEM_PROMPT_BASE + '\n\n' + LANGUAGE_GUIDANCE[language]},
        {'role': 'user', 'content': user_prompt},
    ]

    return strip_code_fence(call_llm(messages))


def tool_revise_program(args):
    """
    assertion 실패나 로그 스캔 결과를 근거로 프로그램을 수정한다

    Args:
        args: 도구 인자 딕셔너리

    Returns:
        수정된 프로그램 소스 문자열
    """
    language = str(args.get('language', 'sas')).lower()
    if language not in LANGUAGE_LABELS:
        raise ValueError(f"language 는 sas / python / r 중 하나여야 합니다 (입력: {language})")

    user_prompt = f"""아래 {LANGUAGE_LABELS[language]} 프로그램이 실패했습니다. 수정하십시오.

## 현재 프로그램
{args.get('source', '')}

## assertion 실패
{args.get('assertion_failures', '(없음)')}

## 로그 스캔 결과
{args.get('log_findings', '(없음)')}

## 오류 요약
{args.get('error_summary', '(없음)')}

## 추가 지시
{args.get('instructions', '(없음)')}

GXPLLM-META 블록은 유지하되 inputs / outputs 가 실제와 다르면 수정하십시오.
수정된 전체 프로그램을 출력하십시오."""

    messages = [
        {'role': 'system', 'content': SYSTEM_PROMPT_BASE + '\n\n' + LANGUAGE_GUIDANCE[language]},
        {'role': 'user', 'content': user_prompt},
    ]

    return strip_code_fence(call_llm(messages))


def tool_profile_data(args):
    """
    데이터 프로파일링 프로그램을 생성한다

    Data Dictionary 가 없는 의뢰 건에서 가장 먼저 실행한다.
    실행은 runner 가 담당하며, 이 도구는 코드만 만든다.

    Args:
        args: 도구 인자 딕셔너리

    Returns:
        생성된 프로파일링 프로그램 소스 문자열
    """
    language = str(args.get('language', 'sas')).lower()
    if language not in LANGUAGE_LABELS:
        raise ValueError(f"language 는 sas / python / r 중 하나여야 합니다 (입력: {language})")

    datasets = args.get('datasets', [])
    user_prompt = f"""다음 데이터셋을 프로파일링하는 {LANGUAGE_LABELS[language]} 프로그램을 작성하십시오.

## 대상 데이터셋
{chr(10).join('- ' + d for d in datasets) if datasets else '(data/raw 아래 전체)'}

## 산출할 정보 (output/tables/profile.json 에 JSON 으로 기록)
각 데이터셋마다:
- 행 수, 컬럼 수
- 컬럼별: 이름, 타입, 길이, 레이블, 결측 수와 비율, 고유값 수
- 문자형 컬럼: 값 도메인 (고유값 20개 이하면 전체, 초과면 상위 20개와 빈도)
- 숫자형 컬럼: 최소/최대/평균/중앙값/사분위수
- 날짜형 컬럼: 최소/최대 날짜
- 데이터셋 간 공통 컬럼 (key 관계 추정용)
- 각 후보 key 의 유일성 여부

## 중요
개별 피험자를 식별할 수 있는 값(피험자 ID 원본값, 자유기술 텍스트 원문)은
프로파일에 포함하지 마십시오. 고유값 수와 형식만 기록합니다.
자유기술 컬럼은 값 대신 평균 길이와 문장 수만 기록합니다.

## 추가 지시
{args.get('instructions', '(없음)')}
"""

    messages = [
        {'role': 'system', 'content': SYSTEM_PROMPT_BASE + '\n\n' + LANGUAGE_GUIDANCE[language]},
        {'role': 'user', 'content': user_prompt},
    ]

    return strip_code_fence(call_llm(messages))


def tool_structure_text(args):
    """
    비정형 임상 문구를 정형 데이터로 변환한다

    Args:
        args: 도구 인자 딕셔너리

    Returns:
        정형화 결과 JSON 문자열

    Raises:
        ValueError: text 가 비어 있는 경우
        RuntimeError: 응답이 유효한 JSON 이 아닌 경우
    """
    text = args.get('text', '')
    if not text:
        raise ValueError('text 가 비어 있습니다')

    schema = args.get('output_schema')
    candidates = args.get('code_candidates', [])

    user_prompt = f"""다음 임상 기록을 정형화하십시오.

## 원문
{text}

## 출력 스키마
{json.dumps(schema, ensure_ascii=False, indent=2) if schema else '(미지정)'}

## 코드 후보 (이 목록에서만 선택)
{chr(10).join('- ' + c for c in candidates) if candidates else '(미제공)'}

## 추가 지시
{args.get('instructions', '(없음)')}

각 추출 값마다 evidence 필드에 원문 내 시작/끝 문자 offset 과 해당 문구를 포함하십시오.
근거를 댈 수 없는 값은 추출하지 마십시오."""

    messages = [
        {'role': 'system', 'content': STRUCTURE_TEXT_SYSTEM},
        {'role': 'user', 'content': user_prompt},
    ]

    response_format = None
    if schema:
        response_format = {
            'type': 'json_schema',
            'json_schema': {'name': 'clinical_extraction', 'schema': schema},
        }

    raw = call_llm(messages, temperature=0.0, response_format=response_format)

    # 코드와 달리 JSON 은 완결성 검사가 저렴하다.
    # 깨진 JSON 을 그대로 넘기면 downstream 이 "추출된 항목 없음" 으로
    # 조용히 흡수한다.
    if schema:
        try:
            json.loads(raw)
        except ValueError as exc:
            raise RuntimeError(
                f"정형화 결과가 유효한 JSON 이 아닙니다: {exc}. "
                f"응답 앞부분: {raw[:200]}"
            ) from exc

    return raw


# ============================================================================
# MCP 프로토콜
# ============================================================================

TOOLS = [
    {
        'name': 'write_program',
        'description': (
            'SAP table shell 과 Data Dictionary 로 SAS/Python/R 분석 프로그램을 생성합니다. '
            '로컬 LLM(Qwen3.6)이 작성하며, 실행은 runner 가 담당합니다. '
            '생성된 코드는 사람 검토를 거쳐야 합니다.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'language': {'type': 'string', 'enum': ['sas', 'python', 'r']},
                'program_name': {'type': 'string'},
                'purpose': {'type': 'string'},
                'sap_ref': {'type': 'string'},
                'table_shell': {'type': 'string'},
                'data_dictionary': {'type': 'string'},
                'assertions_spec': {'type': 'string'},
                'inputs': {'type': 'array', 'items': {'type': 'string'}},
                'outputs': {'type': 'array', 'items': {'type': 'string'}},
                'analysis_set': {'type': 'string'},
                'instructions': {'type': 'string'},
            },
            'required': ['language', 'program_name', 'purpose'],
        },
    },
    {
        'name': 'revise_program',
        'description': (
            'assertion 실패나 로그 스캔 결과를 근거로 프로그램을 수정합니다. '
            '원본 데이터 값이 아니라 정제된 실패 메시지만 입력하십시오.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'language': {'type': 'string', 'enum': ['sas', 'python', 'r']},
                'source': {'type': 'string'},
                'assertion_failures': {'type': 'string'},
                'log_findings': {'type': 'string'},
                'error_summary': {'type': 'string'},
                'instructions': {'type': 'string'},
            },
            'required': ['language', 'source'],
        },
    },
    {
        'name': 'profile_data',
        'description': (
            '데이터 프로파일링 프로그램을 생성합니다. Data Dictionary 가 없는 '
            '의뢰 건에서 가장 먼저 실행합니다. 실행은 runner 가 담당합니다.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'language': {'type': 'string', 'enum': ['sas', 'python', 'r']},
                'datasets': {'type': 'array', 'items': {'type': 'string'}},
                'instructions': {'type': 'string'},
            },
            'required': ['language'],
        },
    },
    {
        'name': 'structure_text',
        'description': (
            '비정형 임상 문구를 정형 데이터로 변환합니다. 로컬 LLM 만 원문을 봅니다. '
            '추출 값마다 원문 내 근거 위치(evidence span)를 함께 반환합니다.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'text': {'type': 'string'},
                'output_schema': {'type': 'object'},
                'code_candidates': {'type': 'array', 'items': {'type': 'string'}},
                'instructions': {'type': 'string'},
            },
            'required': ['text'],
        },
    },
]

TOOL_HANDLERS = {
    'write_program': tool_write_program,
    'revise_program': tool_revise_program,
    'profile_data': tool_profile_data,
    'structure_text': tool_structure_text,
}


def handle_request(request):
    """
    MCP 요청을 처리한다

    Args:
        request: JSON-RPC 요청 딕셔너리

    Returns:
        응답 딕셔너리. 알림(notification)이면 None
    """
    method = request.get('method')
    request_id = request.get('id')

    if method == 'initialize':
        return {
            'jsonrpc': '2.0',
            'id': request_id,
            'result': {
                'protocolVersion': PROTOCOL_VERSION,
                'capabilities': {'tools': {}},
                'serverInfo': {'name': SERVER_NAME, 'version': SERVER_VERSION},
            },
        }

    if method == 'notifications/initialized':
        return None

    if method == 'tools/list':
        return {'jsonrpc': '2.0', 'id': request_id, 'result': {'tools': TOOLS}}

    if method == 'tools/call':
        params = request.get('params') or {}
        name = params.get('name')
        args = params.get('arguments') or {}

        handler = TOOL_HANDLERS.get(name)
        if handler is None:
            return {
                'jsonrpc': '2.0', 'id': request_id,
                'error': {'code': -32601, 'message': f'알 수 없는 도구: {name}'},
            }

        try:
            output = handler(args)
            return {
                'jsonrpc': '2.0', 'id': request_id,
                'result': {'content': [{'type': 'text', 'text': output}]},
            }
        except Exception as exc:
            return {
                'jsonrpc': '2.0', 'id': request_id,
                'result': {
                    'content': [{'type': 'text',
                                 'text': f'[오류] {type(exc).__name__}: {exc}'}],
                    'isError': True,
                },
            }

    if request_id is None:
        return None

    return {
        'jsonrpc': '2.0', 'id': request_id,
        'error': {'code': -32601, 'message': f'지원하지 않는 메서드: {method}'},
    }


def main():
    """메인 함수 (stdio 루프)"""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except ValueError:
            continue

        response = handle_request(request)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + '\n')
            sys.stdout.flush()


if __name__ == "__main__":
    main()
