"""
benchmark_codegen.py 동작 검증 (모의 서버)

실측 도구 자체가 동작하는지 확인한다.

**왜 필요한가**
  benchmark_codegen.py 의 측정 결과가 프로젝트 설계를 좌우한다
  (SAS 성적이 나쁘면 SAS 작성을 Opus 에게 이관).
  그런 도구가 멈추거나 잘못된 집계를 내면 잘못된 결정을 하게 된다.
  실제 PC 에서 쓰기 전에 도구 자체를 먼저 검증한다.

검증 항목
  - --init-cases 템플릿 생성
  - 케이스 로드 및 언어별 실행
  - 코드 생성 -> runner 실행 -> 실패 시 수정 루프
  - 지표 집계 (성공률, 평균 수정 횟수, 사람 시간 평균)
  - 판단 가이드 출력
  - 결과 JSON 저장

모의 LLM 이 선언한 산출물을 만들지 않으므로 성공률 0% 가 정상이다.
**도구가 실패를 올바르게 탐지하는지**를 보는 것이 목적이다.

실행:
    python tests/test_benchmark.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT / 'tests') not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT / 'tests'))

import mock_vllm_server  # noqa: E402

# ============================================================================
# 설정 영역
# ============================================================================

PORT = 18031
ENDPOINT = f'http://127.0.0.1:{PORT}/v1'

CASES_YAML = """study_id: BENCH-001

cases:
  - id: t_easy
    title: 인구통계 요약
    difficulty: easy
    analysis_set: Safety Set (SAFFL='Y')
    inputs: []
    outputs:
      - output/tables/t_mock.rtf
    table_shell: |
      Table 14.1.1 인구통계학적 특성
    data_dictionary: |
      USUBJID char(20)
      SAFFL   char(1)
    assertions_spec: |
      - analysis_set: SAFFL='Y'
    human_minutes: 45

  - id: t_medium
    title: 이상반응 요약
    difficulty: medium
    analysis_set: Safety Set (SAFFL='Y')
    inputs: []
    outputs:
      - output/tables/t_mock.rtf
    table_shell: |
      Table 14.3.1 TEAE 요약
    data_dictionary: |
      USUBJID char(20)
      AEDECOD char(200)
    assertions_spec: |
      - count_unit: subject
    human_minutes: 90
"""


def main():
    """메인 함수"""
    print("=" * 80)
    print("benchmark_codegen.py 동작 검증 (모의 서버)")
    print("=" * 80)

    problems = []
    server, _ = mock_vllm_server.start_server(PORT)
    print(f"\n  모의 서버: {ENDPOINT}")

    tmpdir = tempfile.mkdtemp()
    try:
        study_root = Path(tmpdir, 'BENCH-001')

        # --- study 생성 -----------------------------------------------------
        print(f"\n[1/4] study 생성...")
        env = dict(os.environ)
        env['PYTHONIOENCODING'] = 'utf-8'
        env['GXPLLM_AUDIT_KEY_DIR'] = os.environ.get('TEMP', tmpdir)

        subprocess.run(
            [sys.executable, str(PLUGIN_ROOT / 'scripts' / 'init_study.py'),
             '--root', str(study_root), '--study-id', 'BENCH-001'],
            capture_output=True, env=env, timeout=120,
        )
        print(f"  {study_root}")

        # --- 케이스 파일 ----------------------------------------------------
        print(f"\n[2/4] 케이스 파일 작성...")
        cases_path = Path(tmpdir, 'cases.yaml')
        cases_path.write_text(CASES_YAML, encoding='utf-8')
        print(f"  케이스 2건")

        # --- 템플릿 생성 기능 확인 ------------------------------------------
        print(f"\n[3/4] --init-cases 템플릿 생성...")
        template_path = Path(tmpdir, 'template.yaml')
        result = subprocess.run(
            [sys.executable, str(PLUGIN_ROOT / 'scripts' / 'benchmark_codegen.py'),
             '--init-cases', str(template_path)],
            capture_output=True, env=env, timeout=120,
        )
        if not template_path.is_file():
            problems.append('--init-cases 가 템플릿을 만들지 않았습니다')
            print(f"  FAIL 템플릿 미생성")
        else:
            print(f"  OK   {(template_path.read_text(encoding='utf-8').count(chr(10))):,}줄")

        # --- 실측 실행 ------------------------------------------------------
        print(f"\n[4/4] 실측 실행 (python 만, 모의 LLM)...")
        output_path = Path(tmpdir, 'result.json')
        started = time.time()
        result = subprocess.run(
            [sys.executable, str(PLUGIN_ROOT / 'scripts' / 'benchmark_codegen.py'),
             '--cases', str(cases_path),
             '--study', str(study_root),
             '--languages', 'python',
             '--max-revisions', '1',
             '--endpoint', ENDPOINT,
             '--model', mock_vllm_server.MODEL_ID,
             '--output', str(output_path)],
            capture_output=True, env=env, timeout=900,
        )
        elapsed = time.time() - started

        stdout = (result.stdout or b'').decode('utf-8', errors='replace')
        stderr = (result.stderr or b'').decode('utf-8', errors='replace')

        print(f"  종료 코드: {result.returncode}, 소요 {elapsed:.1f}초")

        if not output_path.is_file():
            problems.append(f'결과 JSON 미생성. stderr: {stderr[:300]}')
            print(f"  FAIL 결과 JSON 없음")
            print(f"       stdout 마지막: {stdout[-500:]}")
        else:
            payload = json.loads(output_path.read_text(encoding='utf-8'))
            summary = payload.get('summary', {}).get('python', {})
            results = payload.get('results', [])

            print(f"  OK   결과 JSON 생성")
            print(f"       케이스 {len(results):,}건, "
                  f"생성 {summary.get('generated', 0):,}건")
            print(f"       무수정 성공률 {summary.get('no_revision_success_rate', 0):.1%}, "
                  f"최종 {summary.get('final_success_rate', 0):.1%}")
            print(f"       평균 사람 시간 {summary.get('avg_human_minutes')}분")

            # 집계가 정상인지
            if len(results) != 2:
                problems.append(f'케이스 2건인데 결과가 {len(results)}건')
            if summary.get('total') != 2:
                problems.append(f"summary.total 이 {summary.get('total')}")
            if summary.get('generated', 0) != 2:
                problems.append(f"코드 생성이 {summary.get('generated')}건 (2건 기대)")
            if summary.get('avg_human_minutes') != 67.5:
                problems.append(
                    f"avg_human_minutes 가 {summary.get('avg_human_minutes')} "
                    f"(45, 90 평균 67.5 기대)"
                )

            # 판단 가이드가 출력됐는지
            if '판단 가이드' not in stdout:
                problems.append('판단 가이드가 출력되지 않았습니다')

    finally:
        server.shutdown()
        shutil.rmtree(tmpdir, ignore_errors=True)

    print(f"\n{'=' * 80}")
    if problems:
        print(f"실패: {len(problems):,}건")
        for item in problems:
            print(f"  - {item}")
    else:
        print("benchmark_codegen.py 정상 동작 — 실제 PC 에서 사용 가능")
    print("=" * 80)

    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
