"""
SAS 9.4 배치 실행 runner

SAS 프로그램을 배치 모드로 실행하고 감사 증적을 남긴다.
- run_id 생성 및 run 디렉터리 준비
- GXPLLM-META 블록에서 입출력 파악, 입력 데이터셋 SHA-256 계산
- sas.exe 배치 실행 (-sysin / -log / -print / -noterminal)
- .log 스캔 (ERROR / WARNING / CRITICAL NOTE 14종)
- assertion 수집 및 판정
- manifest.json 생성, 감사 로그 append

사용:
    python scripts/run_sas.py --program programs/sas/t_dm_summary.sas
    python scripts/run_sas.py --program programs/sas/t_ae.sas --purpose submission_candidate

중요: SAS 는 논리적으로 치명적인 상황에서도 종료 코드 0 을 반환할 수 있다.
      반환 코드만 믿지 말고 반드시 로그를 스캔한다.
"""

import _common  # noqa: F401  (sys.path 설정)

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

from _common import (
    preflight, run_subprocess, finalize, announce_start,
    print_summary, read_text_auto,
)
from gxpllm.core import relative_to_root, sha256_file

# ============================================================================
# 설정 영역 - 여기서 수정하세요
# ============================================================================

LANGUAGE = 'sas'
DEFAULT_SAS_EXE = r'C:\Program Files\SASHome\SASFoundation\9.4\sas.exe'
DEFAULT_WORK_ROOT = r'C:\sastemp'
DEFAULT_TIMEOUT_SEC = 3600

# SAS 로그 스캔 규칙: (심각도, 규칙 ID, 정규식)
# 임상 프로그래밍에서 통용되는 로그 QC 체크리스트
LOG_RULES = [
    ('ERROR',    'SAS_ERROR',          r'^ERROR'),
    ('WARNING',  'SAS_WARNING',        r'^WARNING'),

    # --- 아래는 SAS 가 종료 코드 0 을 반환해도 발생할 수 있는 치명적 NOTE ---
    ('CRITICAL', 'MERGE_REPEAT_BY',    r'MERGE statement has more than one data set with repeats of BY values'),
    ('CRITICAL', 'NUM_TO_CHAR',        r'Numeric values have been converted to character'),
    ('CRITICAL', 'CHAR_TO_NUM',        r'Character values have been converted to numeric'),
    ('CRITICAL', 'INVALID_NUMERIC',    r'Invalid numeric data'),
    ('CRITICAL', 'INVALID_DATA',       r'Invalid data for'),
    ('CRITICAL', 'MISSING_GENERATED',  r'Missing values were generated as a result of'),
    ('CRITICAL', 'DIVIDE_BY_ZERO',     r'Division by zero detected'),
    ('CRITICAL', 'MATH_FAILED',        r'Mathematical operations could not be performed'),
    ('CRITICAL', 'UNINITIALIZED',      r'Variable .+ is uninitialized'),
    ('CRITICAL', 'FORMAT_TOO_SMALL',   r'W\.D format was too small'),
    ('CRITICAL', 'SQL_REMERGE',        r'query requires remerging summary statistics'),
    ('CRITICAL', 'CARTESIAN',          r'The execution of this query involves performing one or more Cartesian'),
    ('CRITICAL', 'MULTIPLE_LENGTHS',   r'Multiple lengths were specified for the variable'),
    ('CRITICAL', 'CHARACTER_TRUNCATED',r'.+ values? (have|has) been (converted|truncated)'),

    # --- 확인이 필요한 정보성 NOTE ---
    ('INFO',     'ZERO_OBS_READ',      r'There were 0 observations read'),
    ('INFO',     'ZERO_OBS_OUT',       r'has 0 observations'),
    ('INFO',     'DATA_STEP_STOPPED',  r'The SAS System stopped processing this step'),
]

COMPILED_RULES = [(sev, rule, re.compile(pattern)) for sev, rule, pattern in LOG_RULES]

LOG_LINE_MAX_LENGTH = 300

# SAS 종료 코드 해석
SAS_EXIT_MEANING = {
    0: '정상 종료',
    1: 'WARNING 발생',
    2: 'ERROR 발생',
    3: 'ABORT 문 실행',
    4: 'ABORT RETURN 실행',
    5: 'ABORT ABEND 실행',
    6: 'SAS 내부 오류',
}


# ============================================================================
# 메인 로직
# ============================================================================

def scan_sas_log(log_text):
    """
    SAS 로그를 스캔해 문제 항목을 찾는다

    각 줄에 대해 규칙을 순서대로 검사하고, 첫 번째로 일치하는 규칙을 기록한다.
    규칙 순서가 곧 우선순위이므로 ERROR / WARNING 을 앞에 둔다.

    Args:
        log_text: 디코딩된 SAS 로그 전체 텍스트

    Returns:
        findings 리스트. 각 항목은 severity / rule / line / text 를 갖는다
    """
    findings = []

    for line_no, line in enumerate(log_text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        for severity, rule, pattern in COMPILED_RULES:
            if pattern.search(stripped):
                findings.append({
                    'severity': severity,
                    'rule': rule,
                    'line': line_no,
                    'text': stripped[:LOG_LINE_MAX_LENGTH],
                })
                break

    return findings


def build_sas_command(config, program_abs, run_dir, run_id, study_id):
    """
    sas.exe 배치 실행 명령을 구성한다

    Args:
        config: study 설정
        program_abs: 실행할 .sas 절대경로
        run_dir: run 디렉터리 Path
        run_id: run 식별자
        study_id: study 식별자

    Returns:
        (명령 리스트, log_path, lst_path, work_dir)
    """
    sas_exe = config.get('sas_exe') or DEFAULT_SAS_EXE
    log_path = Path(run_dir) / 'execution.log'
    lst_path = Path(run_dir) / 'execution.lst'

    work_root = config.get('sas_work_root') or DEFAULT_WORK_ROOT
    work_dir = Path(work_root) / run_id
    work_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sas_exe,
        '-sysin', str(program_abs),
        '-log', str(log_path),
        '-print', str(lst_path),
        '-nosplash',
        '-noterminal',
        '-sysparm', f"run_id={run_id};study_id={study_id};run_dir={run_dir}",
        '-work', str(work_dir),
    ]

    # config 에서 추가 옵션을 지정할 수 있게 한다 (예: -config, -autoexec)
    for extra in config.get('sas_extra_options', []):
        cmd.append(str(extra))

    return cmd, log_path, lst_path, work_dir


def cleanup_work_dir(work_dir):
    """
    SAS WORK 디렉터리를 정리한다

    실패해도 무시한다. 잔여물이 남아도 run 별로 분리되어 있어 충돌하지 않는다.

    Args:
        work_dir: WORK 디렉터리 Path
    """
    try:
        import shutil
        shutil.rmtree(work_dir, ignore_errors=True)
    except Exception:
        pass


def main():
    """메인 함수"""
    print("=" * 80)
    print("SAS 9.4 배치 실행 runner")
    print("=" * 80)

    parser = argparse.ArgumentParser(description='SAS 프로그램을 배치 실행하고 감사 증적을 남깁니다')
    parser.add_argument('--program', required=True, help='실행할 .sas 파일 경로')
    parser.add_argument('--purpose', default='exploratory',
                        choices=['exploratory', 'qc', 'submission_candidate'],
                        help='실행 목적 (제출 후보는 WARNING/CRITICAL 도 실패 처리)')
    parser.add_argument('--allow-no-assertions', action='store_true',
                        help='assertion 0건을 실패로 보지 않습니다 (exploratory 전용, '
                             '감사 로그에 기록됨)')
    parser.add_argument('--timeout', type=int, default=DEFAULT_TIMEOUT_SEC,
                        help=f'타임아웃 (초, 기본 {DEFAULT_TIMEOUT_SEC})')
    parser.add_argument('--keep-work', action='store_true',
                        help='SAS WORK 디렉터리를 삭제하지 않습니다')
    args = parser.parse_args()

    # --- 1/6 준비 -----------------------------------------------------------
    print(f"\n[1/6] 실행 환경 준비...")
    try:
        context = preflight(args.program, args.purpose, args.allow_no_assertions)
    except (FileNotFoundError, ValueError) as exc:
        print(f"\n  오류: {exc}")
        sys.exit(2)

    context['language'] = LANGUAGE
    sas_exe = context['config'].get('sas_exe') or DEFAULT_SAS_EXE

    print(f"  study_id : {context['study_id']}")
    print(f"  run_id   : {context['run_id']}")
    print(f"  purpose  : {args.purpose}")
    print(f"  program  : {context['program']['path']} ({context['program']['lines']:,}줄)")
    print(f"  SAS      : {sas_exe}")

    if not Path(sas_exe).is_file():
        print(f"\n  오류: SAS 실행 파일을 찾을 수 없습니다: {sas_exe}")
        print(f"        .gxpllm/config.json 의 sas_exe 를 확인하십시오.")
        sys.exit(2)

    announce_start(context)

    # --- 2/6 입력 확인 ------------------------------------------------------
    print(f"\n[2/6] 입력 데이터셋 확인...")
    declared_inputs = context['meta'].get('inputs', [])
    if declared_inputs:
        for relative in declared_inputs:
            path = context['study_root'] / relative
            status = 'OK' if path.is_file() else '없음'
            size = f"{path.stat().st_size:,} bytes" if path.is_file() else '-'
            print(f"  [{status}] {relative}  {size}")
    else:
        print(f"  선언된 입력 없음 (GXPLLM-META 의 inputs 를 확인하십시오)")

    # --- 3/6 SAS 실행 -------------------------------------------------------
    print(f"\n[3/6] SAS 실행 중...")
    cmd, log_path, lst_path, work_dir = build_sas_command(
        context['config'], context['program']['abs_path'],
        context['run_dir'], context['run_id'], context['study_id'],
    )

    started = datetime.now()
    context['started_at'] = started.astimezone().isoformat(timespec='seconds')

    exit_code, _, _ = run_subprocess(
        cmd,
        cwd=context['study_root'],
        run_dir=context['run_dir'],
        env={'GXPLLM_RUN_DIR': str(context['run_dir']),
             'GXPLLM_RUN_ID': context['run_id']},
        timeout=args.timeout,
    )

    finished = datetime.now()
    context['finished_at'] = finished.astimezone().isoformat(timespec='seconds')
    context['duration_sec'] = round((finished - started).total_seconds(), 1)
    context['exit_code'] = exit_code
    context['command'] = ' '.join(f'"{c}"' if ' ' in str(c) else str(c) for c in cmd)

    meaning = SAS_EXIT_MEANING.get(exit_code, '알 수 없는 코드')
    print(f"  종료 코드: {exit_code} ({meaning})")
    print(f"  소요 시간: {context['duration_sec']:,.1f}초")

    if not args.keep_work:
        cleanup_work_dir(work_dir)

    # --- 4/6 로그 스캔 ------------------------------------------------------
    print(f"\n[4/6] SAS 로그 스캔...")
    log_text, encoding = read_text_auto(
        log_path, context['config'].get('sas_log_encoding')
    )

    if not log_text:
        print(f"  경고: 로그 파일이 비어 있거나 생성되지 않았습니다")
        findings = [{
            'severity': 'ERROR',
            'rule': 'NO_LOG',
            'line': 0,
            'text': 'SAS 로그가 생성되지 않았습니다. SAS 실행 자체가 실패했을 수 있습니다.',
        }]
    else:
        print(f"  인코딩: {encoding}")
        findings = scan_sas_log(log_text)

    counts = {}
    for finding in findings:
        counts[finding['severity']] = counts.get(finding['severity'], 0) + 1

    if findings:
        for severity in ('ERROR', 'WARNING', 'CRITICAL', 'INFO'):
            if counts.get(severity):
                print(f"  {severity}: {counts[severity]:,}건")
        # 치명적 항목은 내용까지 보여준다 (로컬 화면에만 표시)
        for finding in findings:
            if finding['severity'] in ('ERROR', 'CRITICAL'):
                print(f"    L{finding['line']:,} [{finding['rule']}] {finding['text'][:120]}")
    else:
        print(f"  문제 없음")

    context['logs'] = {
        'execution_log': relative_to_root(log_path, context['study_root']),
        'execution_lst': relative_to_root(lst_path, context['study_root']),
        'log_sha256': sha256_file(log_path),
        'lst_sha256': sha256_file(lst_path),
        'log_encoding': encoding,
    }
    context['environment_extra'] = {'sas_exe': sas_exe}

    # --- 5/6 assertion 수집 및 manifest ------------------------------------
    print(f"\n[5/6] assertion 수집 및 manifest 생성...")
    result, manifest = finalize(context, findings)
    print(f"  assertion: {manifest['assertions']['passed']:,}건 통과 / "
          f"{manifest['assertions']['failed']:,}건 실패")
    print(f"  manifest : logs/runs/{context['run_id']}/manifest.json")

    # --- 6/6 결과 -----------------------------------------------------------
    print(f"\n[6/6] 감사 로그 기록 완료")

    print_summary(context, result)
    sys.exit(1 if result == 'FAILED' else 0)


if __name__ == "__main__":
    main()
