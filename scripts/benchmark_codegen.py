"""
언어별 코드 생성 품질 실측 벤치마크

docs/development.md 11.1 의 측정을 자동화한다.
**plugin 을 실제 업무에 쓰기 전에 반드시 수행해야 한다.**

측정 목적
  Qwen3.6-35B-A3B 는 Python 대비 SAS 에서 성능이 낮을 가능성이 높다.
  SAS 매크로, PROC 문법, ADaM 파생 관례는 학습 데이터가 훨씬 적기 때문이다.
  이 결과에 따라 설계가 달라진다.

    SAS 도 괜찮으면  -> 계획대로 3개 언어 모두 지원
    SAS 만 나쁘면    -> SAS 코드 작성은 Opus 에게 맡긴다
                        (코드에는 피험자 데이터가 없으므로 경계 위반이 아니다)
    전반적으로 나쁘면 -> 범위를 build-dictionary 와 탐색적 분석으로 축소

측정 지표
  무수정 실행 성공률   생성된 코드가 그대로 도는 비율
  결과 일치율          기존 결과와 숫자가 일치하는 비율
  평균 수정 라운드     통과까지 필요한 수정 횟수
  **검토 시간**        이게 직접 작성 시간보다 길면 프로젝트가 성립하지 않는다

사용:
    # 1. 케이스 파일 템플릿 생성
    python scripts/benchmark_codegen.py --init-cases benchmark/cases.yaml

    # 2. 케이스를 실제 업무 프로그램으로 채운 뒤 실행
    python scripts/benchmark_codegen.py --cases benchmark/cases.yaml --study D:\\clinical\\DEMO-001

    # 3. 특정 언어만
    python scripts/benchmark_codegen.py --cases benchmark/cases.yaml --languages sas,python
"""

import _common  # noqa: F401  (sys.path 설정)

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from gxpllm.core import load_config, now_iso, sha256_text

# ============================================================================
# 설정 영역 - 여기서 수정하세요
# ============================================================================

DEFAULT_LANGUAGES = ('sas', 'python', 'r')
DEFAULT_MAX_REVISIONS = 3
MCP_SERVER = 'mcp/local_coder_server.py'

RUNNER_BY_LANGUAGE = {
    'sas': 'run_sas.py',
    'python': 'run_python.py',
    'r': 'run_r.py',
}

EXTENSION_BY_LANGUAGE = {
    'sas': '.sas',
    'python': '.py',
    'r': '.R',
}

# 케이스 파일 템플릿
CASES_TEMPLATE = """# 언어별 코드 생성 품질 실측 케이스
#
# 실제 업무에서 쓰던 프로그램 10개를 선정해 채우십시오.
# 다양성이 중요합니다: 요약표, 이상반응, 생존분석, listing, 파생 데이터셋 등
#
# 각 케이스마다
#   - table_shell     : SAP 의 표 정의 (행/열 구조, 라벨)
#   - data_dictionary : 사용할 변수의 이름/타입/도메인
#   - assertions_spec : 검증 규칙
#   - expected        : 기존 프로그램이 낸 결과 (JSON 파일 경로). 결과 일치율 측정용
#   - human_minutes   : 사람이 직접 작성했을 때 걸린 시간 (분). 없으면 생략

study_id: DEMO-001

cases:
  - id: t_14_1_1
    title: 인구통계학적 특성 요약
    difficulty: easy
    analysis_set: Safety Set (SAFFL='Y')
    inputs:
      - data/derived/adsl.sas7bdat
    outputs:
      - output/tables/t_14_1_1.rtf
    table_shell: |
      Table 14.1.1  인구통계학적 특성 (Safety Set)

                                A군 (N=xxx)   B군 (N=xxx)   전체 (N=xxx)
      연령 (세)
        n                            xxx           xxx           xxx
        평균 (표준편차)         xx.x (x.xx)   xx.x (x.xx)   xx.x (x.xx)
        중앙값                      xx.x          xx.x          xx.x
        최소, 최대               xx, xx        xx, xx        xx, xx
      성별, n (%)
        남                      xxx (xx.x)    xxx (xx.x)    xxx (xx.x)
        여                      xxx (xx.x)    xxx (xx.x)    xxx (xx.x)
    data_dictionary: |
      USUBJID  char(20)  피험자 식별자
      SAFFL    char(1)   Safety Set flag, 도메인 {Y, N}
      TRT01A   char(20)  실제 치료군
      AGE      num       연령 (세)
      SEX      char(1)   성별, 도메인 {M, F}
    assertions_spec: |
      - analysis_set: SAFFL='Y'
      - denominator: unique USUBJID
      - reconciliation: sum(arm_counts) == overall_count
    expected: benchmark/expected/t_14_1_1.json
    human_minutes: 45

  # 케이스 2~10 을 같은 형식으로 추가하십시오.
  # 난이도를 섞으십시오: easy 3, medium 4, hard 3 정도가 적절합니다.
"""


# ============================================================================
# MCP 호출
# ============================================================================

def call_mcp(plugin_root, tool_name, arguments, endpoint=None, model=None,
             timeout=600):
    """
    MCP 서버의 도구를 한 번 호출한다

    Args:
        plugin_root: plugin 루트 Path
        tool_name: 호출할 도구 이름
        arguments: 도구 인자 딕셔너리
        endpoint: LLM endpoint (None 이면 환경 기본값)
        model: LLM 모델명
        timeout: 타임아웃 (초)

    Returns:
        (성공 여부, 출력 문자열, 소요 시간 초)
    """
    import os

    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    if endpoint:
        env['GXPLLM_ENDPOINT'] = endpoint
    if model:
        env['GXPLLM_MODEL'] = model

    requests = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
         'params': {'name': tool_name, 'arguments': arguments}},
    ]
    payload = '\n'.join(json.dumps(r, ensure_ascii=False) for r in requests) + '\n'

    started = time.time()
    try:
        result = subprocess.run(
            [sys.executable, str(Path(plugin_root) / MCP_SERVER)],
            input=payload.encode('utf-8'),
            capture_output=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return False, f'타임아웃 {timeout}초 초과', time.time() - started

    elapsed = time.time() - started

    for line in (result.stdout or b'').decode('utf-8', errors='replace').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            response = json.loads(line)
        except ValueError:
            continue
        if response.get('id') != 2:
            continue

        payload_result = response.get('result') or {}
        text = (payload_result.get('content') or [{}])[0].get('text', '')
        if payload_result.get('isError'):
            return False, text, elapsed
        return True, text, elapsed

    return False, 'MCP 응답을 받지 못했습니다', elapsed


# ============================================================================
# 케이스 실행
# ============================================================================

def load_cases(path):
    """
    케이스 파일을 읽는다

    PyYAML 이 없으면 간단한 파서로 처리한다

    Args:
        path: 케이스 파일 경로

    Returns:
        케이스 딕셔너리

    Raises:
        SystemExit: 읽기 실패 시
    """
    p = Path(path)
    if not p.is_file():
        print(f"오류: 케이스 파일을 찾을 수 없습니다: {path}")
        print(f"      먼저 --init-cases 로 템플릿을 만드십시오.")
        sys.exit(2)

    try:
        import yaml
    except ImportError:
        print(f"오류: PyYAML 이 필요합니다. 'pip install pyyaml' 또는 'uv add pyyaml'")
        sys.exit(2)

    with open(p, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def run_case(case, language, plugin_root, study_root, max_revisions,
             endpoint=None, model=None):
    """
    한 케이스를 한 언어로 실행한다

    Args:
        case: 케이스 딕셔너리
        language: sas / python / r
        plugin_root: plugin 루트 Path
        study_root: study 루트 Path
        max_revisions: 최대 수정 횟수
        endpoint: LLM endpoint
        model: LLM 모델명

    Returns:
        결과 딕셔너리
    """
    case_id = case.get('id', 'unnamed')
    extension = EXTENSION_BY_LANGUAGE[language]
    program_path = study_root / 'programs' / language / f'bench_{case_id}{extension}'

    result = {
        'case_id': case_id,
        'title': case.get('title'),
        'difficulty': case.get('difficulty'),
        'language': language,
        'generated': False,
        'ran_without_revision': False,
        'passed': False,
        'revisions': 0,
        'gen_seconds': 0.0,
        'run_seconds': 0.0,
        'assertion_passed': 0,
        'assertion_failed': 0,
        'log_findings': {},
        'human_minutes': case.get('human_minutes'),
        'errors': [],
    }

    # --- 1. 코드 생성 -------------------------------------------------------
    ok, source, gen_seconds = call_mcp(
        plugin_root, 'write_program',
        {
            'language': language,
            'program_name': f'bench_{case_id}{extension}',
            'purpose': case.get('title', ''),
            'sap_ref': f"benchmark#{case_id}",
            'table_shell': case.get('table_shell', ''),
            'data_dictionary': case.get('data_dictionary', ''),
            'assertions_spec': case.get('assertions_spec', ''),
            'inputs': case.get('inputs', []),
            'outputs': case.get('outputs', []),
            'analysis_set': case.get('analysis_set', ''),
        },
        endpoint, model,
    )
    result['gen_seconds'] = round(gen_seconds, 1)

    if not ok:
        result['errors'].append(f"코드 생성 실패: {source[:200]}")
        return result

    result['generated'] = True
    result['source_sha256'] = sha256_text(source)
    result['source_lines'] = len(source.splitlines())

    program_path.parent.mkdir(parents=True, exist_ok=True)
    program_path.write_text(source, encoding='utf-8')

    # --- 2. 실행 및 수정 반복 -----------------------------------------------
    import os

    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['GXPLLM_PLUGIN_ROOT'] = str(plugin_root).replace('\\', '/')

    for attempt in range(max_revisions + 1):
        started = time.time()
        proc = subprocess.run(
            [sys.executable,
             str(Path(plugin_root) / 'scripts' / RUNNER_BY_LANGUAGE[language]),
             '--program', str(program_path),
             '--purpose', 'exploratory'],
            capture_output=True, cwd=str(study_root), env=env, timeout=1800,
        )
        result['run_seconds'] += round(time.time() - started, 1)

        stdout = (proc.stdout or b'').decode('utf-8', errors='replace')

        # manifest 에서 상세 정보 추출
        import re
        match = re.search(r'run_id\s*:\s*(\S+)', stdout)
        manifest = {}
        if match:
            manifest_path = study_root / 'logs' / 'runs' / match.group(1) / 'manifest.json'
            if manifest_path.is_file():
                try:
                    with open(manifest_path, encoding='utf-8') as f:
                        manifest = json.load(f)
                except (OSError, ValueError):
                    pass

        assertions = manifest.get('assertions') or {}
        result['assertion_passed'] = assertions.get('passed', 0)
        result['assertion_failed'] = assertions.get('failed', 0)
        result['log_findings'] = (manifest.get('log_scan') or {}).get('counts', {})

        if proc.returncode == 0:
            result['passed'] = True
            if attempt == 0:
                result['ran_without_revision'] = True
            break

        if attempt >= max_revisions:
            result['errors'].append(
                f"{max_revisions}회 수정 후에도 실패: "
                f"{'; '.join(manifest.get('failure_reasons', [])[:2])}"
            )
            break

        # --- 3. 수정 요청 (정제된 정보만 전달) ------------------------------
        result['revisions'] += 1

        assertion_failures = []
        assertions_path = study_root / 'logs' / 'runs' / match.group(1) / 'assertions.json' \
            if match else None
        if assertions_path and assertions_path.is_file():
            try:
                with open(assertions_path, encoding='utf-8') as f:
                    data = json.load(f)
                assertion_failures = [
                    f"{a.get('label')}: {a.get('message')}"
                    for a in data.get('assertions', [])
                    if a.get('result') == 'FAIL'
                ]
            except (OSError, ValueError):
                pass

        log_findings = [
            f"[{f.get('severity')}] {f.get('rule')} L{f.get('line')}: {f.get('text', '')[:150]}"
            for f in (manifest.get('log_scan') or {}).get('findings', [])
            if f.get('severity') in ('ERROR', 'CRITICAL')
        ]

        ok, revised, revise_seconds = call_mcp(
            plugin_root, 'revise_program',
            {
                'language': language,
                'source': program_path.read_text(encoding='utf-8'),
                'assertion_failures': '\n'.join(assertion_failures[:20]),
                'log_findings': '\n'.join(log_findings[:20]),
                'error_summary': (manifest.get('environment') or {}).get('sanitized_error', ''),
            },
            endpoint, model,
        )
        result['gen_seconds'] += round(revise_seconds, 1)

        if not ok:
            result['errors'].append(f"수정 실패: {revised[:200]}")
            break

        program_path.write_text(revised, encoding='utf-8')

    return result


# ============================================================================
# 결과 집계
# ============================================================================

def summarize(results):
    """
    언어별 지표를 집계한다

    Args:
        results: 케이스 결과 리스트

    Returns:
        {언어: 지표} 딕셔너리
    """
    summary = {}

    for language in DEFAULT_LANGUAGES:
        subset = [r for r in results if r['language'] == language]
        if not subset:
            continue

        total = len(subset)
        generated = sum(1 for r in subset if r['generated'])
        no_revision = sum(1 for r in subset if r['ran_without_revision'])
        passed = sum(1 for r in subset if r['passed'])
        revisions = [r['revisions'] for r in subset if r['generated']]

        human = [r['human_minutes'] for r in subset if r.get('human_minutes')]
        llm_minutes = [
            (r['gen_seconds'] + r['run_seconds']) / 60 for r in subset if r['generated']
        ]

        summary[language] = {
            'total': total,
            'generated': generated,
            'no_revision_success_rate': round(no_revision / total, 3) if total else 0,
            'final_success_rate': round(passed / total, 3) if total else 0,
            'avg_revisions': round(sum(revisions) / len(revisions), 2) if revisions else 0,
            'avg_llm_minutes': round(sum(llm_minutes) / len(llm_minutes), 1) if llm_minutes else 0,
            'avg_human_minutes': round(sum(human) / len(human), 1) if human else None,
        }

    return summary


def print_report(summary, results):
    """
    결과 보고서를 출력한다

    Args:
        summary: 언어별 지표
        results: 케이스 결과 리스트
    """
    print(f"\n{'=' * 80}")
    print("언어별 코드 생성 품질")
    print("=" * 80)

    header = f"{'언어':<10} {'케이스':>6} {'무수정':>8} {'최종':>8} {'평균수정':>9} {'LLM분':>8} {'사람분':>8}"
    print(f"\n{header}")
    print("-" * 80)

    for language, metrics in summary.items():
        human = f"{metrics['avg_human_minutes']:>8.1f}" if metrics['avg_human_minutes'] else f"{'-':>8}"
        print(
            f"{language:<10} {metrics['total']:>6,} "
            f"{metrics['no_revision_success_rate']:>7.1%} "
            f"{metrics['final_success_rate']:>7.1%} "
            f"{metrics['avg_revisions']:>9.2f} "
            f"{metrics['avg_llm_minutes']:>8.1f} {human}"
        )

    # 실패 케이스
    failures = [r for r in results if not r['passed']]
    if failures:
        print(f"\n실패 케이스 {len(failures):,}건")
        for item in failures:
            reason = item['errors'][0] if item['errors'] else '(사유 미기록)'
            print(f"  [{item['language']}] {item['case_id']} ({item.get('difficulty')}): {reason[:120]}")

    # 판단 가이드
    print(f"\n{'=' * 80}")
    print("판단 가이드")
    print("=" * 80)

    sas = summary.get('sas')
    python = summary.get('python')

    if sas and python:
        gap = python['final_success_rate'] - sas['final_success_rate']
        print(f"\n  SAS 최종 성공률   : {sas['final_success_rate']:.1%}")
        print(f"  Python 최종 성공률: {python['final_success_rate']:.1%}")
        print(f"  격차              : {gap:+.1%}")

        if sas['final_success_rate'] >= 0.7:
            print(f"\n  -> SAS 도 실용 수준입니다. 계획대로 3개 언어를 모두 지원하십시오.")
        elif gap > 0.3:
            print(f"\n  -> SAS 만 눈에 띄게 나쁩니다.")
            print(f"     SAS 코드 작성은 Opus 에게 맡기는 것을 검토하십시오.")
            print(f"     코드에는 피험자 데이터가 없으므로 경계 위반이 아닙니다.")
            print(f"     로컬 LLM 은 Python/R 과 비정형 정형화를 담당합니다.")
        else:
            print(f"\n  -> 전반적으로 낮습니다.")
            print(f"     범위를 /build-dictionary 와 탐색적 분석으로 축소하십시오.")

    # 검토 시간 판정 — 가장 중요
    for language, metrics in summary.items():
        if metrics['avg_human_minutes'] and metrics['avg_llm_minutes']:
            ratio = metrics['avg_llm_minutes'] / metrics['avg_human_minutes']
            verdict = '이득' if ratio < 0.7 else ('비슷' if ratio < 1.0 else '손해')
            print(f"\n  {language}: LLM {metrics['avg_llm_minutes']:.1f}분 vs "
                  f"사람 {metrics['avg_human_minutes']:.1f}분 = {ratio:.2f}배 ({verdict})")
            if ratio >= 1.0:
                print(f"     주의: 검토 시간을 더하면 손해입니다. 이 언어는 재검토하십시오.")

    print(f"\n  ※ LLM 분에는 사람의 코드 검토 시간이 포함되지 않았습니다.")
    print(f"     실제 판단은 검토 시간을 별도 측정해 더한 뒤 하십시오.")


# ============================================================================
# 메인 로직
# ============================================================================

def main():
    """메인 함수"""
    parser = argparse.ArgumentParser(
        description='언어별 코드 생성 품질을 실측합니다 (docs/development.md 11.1)'
    )
    parser.add_argument('--cases', help='케이스 파일 (YAML)')
    parser.add_argument('--study', default='.', help='study 루트 경로')
    parser.add_argument('--languages', default=','.join(DEFAULT_LANGUAGES),
                        help='측정할 언어 (쉼표 구분)')
    parser.add_argument('--max-revisions', type=int, default=DEFAULT_MAX_REVISIONS)
    parser.add_argument('--endpoint', help='LLM endpoint 재정의')
    parser.add_argument('--model', help='LLM 모델명 재정의')
    parser.add_argument('--output', help='결과 JSON 저장 경로')
    parser.add_argument('--init-cases', help='케이스 파일 템플릿을 생성하고 종료')
    args = parser.parse_args()

    plugin_root = Path(__file__).resolve().parent.parent

    # --- 템플릿 생성 --------------------------------------------------------
    if args.init_cases:
        path = Path(args.init_cases)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            print(f"오류: 이미 존재합니다: {path}")
            sys.exit(2)
        path.write_text(CASES_TEMPLATE, encoding='utf-8')
        print("=" * 80)
        print("케이스 템플릿 생성")
        print("=" * 80)
        print(f"\n  {path}")
        print(f"""
  다음 단계

  1. 실제 업무에서 쓰던 프로그램 10개를 선정하십시오
       난이도를 섞으십시오 (easy 3, medium 4, hard 3)
       요약표, 이상반응, 생존분석, listing, 파생 데이터셋을 고루 포함하십시오

  2. 각 케이스의 table_shell, data_dictionary, assertions_spec 을 채우십시오

  3. human_minutes 에 사람이 직접 작성했을 때 걸린 시간을 기록하십시오
       이 값이 없으면 가장 중요한 판단 지표를 계산할 수 없습니다

  4. 데모 study 를 준비하십시오 (실데이터 아님, 합성 데이터 권장)
       python scripts/init_study.py --root <경로> --study-id DEMO-001

  5. 실행
       python scripts/benchmark_codegen.py --cases {path} --study <경로>
""")
        sys.exit(0)

    if not args.cases:
        parser.error('--cases 또는 --init-cases 가 필요합니다')

    # --- 준비 ---------------------------------------------------------------
    print("=" * 80)
    print("언어별 코드 생성 품질 실측")
    print("=" * 80)

    config = load_config(args.study, required=False)
    if not config:
        print(f"\n오류: .gxpllm/config.json 을 찾을 수 없습니다 ({args.study})")
        print(f"      python scripts/init_study.py 로 study 를 먼저 만드십시오.")
        sys.exit(2)

    study_root = Path(config['root'])
    data = load_cases(args.cases)
    cases = data.get('cases', [])
    languages = [lang.strip() for lang in args.languages.split(',') if lang.strip()]

    for language in languages:
        if language not in RUNNER_BY_LANGUAGE:
            print(f"오류: 지원하지 않는 언어: {language}")
            sys.exit(2)

    print(f"\n  study     : {config.get('study_id')}")
    print(f"  케이스    : {len(cases):,}건")
    print(f"  언어      : {', '.join(languages)}")
    print(f"  최대 수정 : {args.max_revisions}회")
    print(f"  총 실행   : {len(cases) * len(languages):,}회")

    if len(cases) < 5:
        print(f"\n  경고: 케이스가 {len(cases)}건뿐입니다. 10건 이상을 권장합니다.")

    # --- 실행 ---------------------------------------------------------------
    results = []
    total_runs = len(cases) * len(languages)
    current = 0

    for language in languages:
        for case in cases:
            current += 1
            print(f"\n[{current}/{total_runs}] {language} / {case.get('id')} "
                  f"({case.get('difficulty', '?')})...")

            result = run_case(case, language, plugin_root, study_root,
                              args.max_revisions, args.endpoint, args.model)
            results.append(result)

            status = 'PASS' if result['passed'] else 'FAIL'
            marker = ' (무수정)' if result['ran_without_revision'] else ''
            print(f"  {status}{marker}  수정 {result['revisions']}회, "
                  f"생성 {result['gen_seconds']:.0f}초, 실행 {result['run_seconds']:.0f}초, "
                  f"assertion {result['assertion_passed']}/{result['assertion_passed'] + result['assertion_failed']}")
            for error in result['errors']:
                print(f"       {error[:150]}")

    # --- 보고 ---------------------------------------------------------------
    summary = summarize(results)
    print_report(summary, results)

    payload = {
        'measured_at': now_iso(),
        'study_id': config.get('study_id'),
        'model': args.model or config.get('llm_model'),
        'endpoint': args.endpoint or config.get('llm_endpoint'),
        'max_revisions': args.max_revisions,
        'summary': summary,
        'results': results,
    }

    output_path = Path(args.output) if args.output else (
        study_root / 'validation' / f"codegen_benchmark_{datetime.now().strftime('%Y%m%d')}.json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"\n  결과 저장: {output_path}")
    print(f"\n  이 결과를 validation/ 에 보관하고 CSV 문서에 첨부하십시오.")


if __name__ == "__main__":
    main()
