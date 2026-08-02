"""
Python 분석 실행 runner

Python 프로그램을 실행하고 SAS .log 에 대응하는 감사 증적을 남긴다.

SAS 와의 차이
- SAS 는 .log 를 자동 생성하지만 Python 은 아무것도 남기지 않는다.
  따라서 runner 가 헤더/입출력/stdout/stderr/assertion 을 모아 execution.log 를 만든다.
- 환경 재현성이 SAS 보다 낮다. uv.lock 기반 --locked 실행으로 보완한다.
- traceback 에 데이터 값이 딸려 나올 수 있다. 원문은 로컬 로그에만 남기고
  Opus 전달용은 sanitize_traceback() 으로 정제한다.

사용:
    python scripts/run_python.py --program programs/python/t_ae_summary.py
    python scripts/run_python.py --program programs/python/t_ae.py --purpose submission_candidate
"""

import _common  # noqa: F401  (sys.path 설정)

import argparse
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

from _common import (
    preflight, run_subprocess, finalize, announce_start,
    print_summary, read_text_auto,
)
from gxpllm.core import relative_to_root, sha256_file, current_user, current_hostname

# ============================================================================
# 설정 영역 - 여기서 수정하세요
# ============================================================================

LANGUAGE = 'python'
DEFAULT_TIMEOUT_SEC = 3600
TRACEBACK_DETAIL_MAX_LENGTH = 60

# stderr 스캔 규칙: (심각도, 규칙 ID, 정규식)
LOG_RULES = [
    ('ERROR',    'PY_TRACEBACK',       r'^Traceback \(most recent call last\)'),
    ('ERROR',    'PY_EXCEPTION',       r'^\w*(Error|Exception)\b.*:'),
    ('ERROR',    'PY_SYSTEM_EXIT',     r'^SystemExit'),
    ('WARNING',  'PY_WARNING',         r'\bWarning\b.*:'),

    # --- pandas / numpy 에서 조용히 잘못된 결과를 만드는 경고 ---
    ('CRITICAL', 'SETTING_WITH_COPY',  r'SettingWithCopyWarning'),
    ('CRITICAL', 'DTYPE_WARNING',      r'DtypeWarning'),
    ('CRITICAL', 'FUTURE_WARNING',     r'FutureWarning'),
    ('CRITICAL', 'RUNTIME_WARNING',    r'RuntimeWarning: (invalid value|divide by zero|overflow)'),
    ('CRITICAL', 'MERGE_SUFFIX',       r'MergeError|Suffixes.*overlapping'),
    ('CRITICAL', 'NUMPY_ALL_NAN',      r'(All-NaN|Mean of empty slice|Degrees of freedom <= 0)'),
    ('CRITICAL', 'PERFORMANCE_FRAG',   r'PerformanceWarning: DataFrame is highly fragmented'),

    ('INFO',     'DEPRECATION',        r'DeprecationWarning'),
    ('INFO',     'USER_WARNING',       r'UserWarning'),
]

COMPILED_RULES = [(sev, rule, re.compile(pattern)) for sev, rule, pattern in LOG_RULES]

LOG_LINE_MAX_LENGTH = 300


# ============================================================================
# 메인 로직
# ============================================================================

def find_uv():
    """
    uv 실행 파일을 찾는다

    Returns:
        uv 경로 문자열. 없으면 None
    """
    return shutil.which('uv')


def build_python_command(config, program_abs, study_root, use_uv):
    """
    Python 실행 명령을 구성한다

    uv 와 uv.lock 이 있으면 --locked 로 실행해 환경 재현성을 강제한다.
    없으면 현재 인터프리터로 실행하되 manifest 에 경고를 남긴다.

    Args:
        config: study 설정
        program_abs: 실행할 .py 절대경로
        study_root: study 루트 Path
        use_uv: uv 사용 여부

    Returns:
        (명령 리스트, 실행 방식 설명 문자열)
    """
    if use_uv:
        uv_exe = find_uv()
        return (
            [uv_exe, 'run', '--locked', '--project', str(study_root),
             'python', '-W', 'always', str(program_abs)],
            'uv run --locked (환경 잠금 적용)',
        )

    return (
        [sys.executable, '-W', 'always', str(program_abs)],
        f'{sys.executable} (환경 잠금 없음 — 재현성 주의)',
    )


def scan_python_log(text):
    """
    Python 실행 출력에서 문제 항목을 찾는다

    Args:
        text: stdout + stderr 를 합친 텍스트

    Returns:
        findings 리스트
    """
    findings = []

    for line_no, line in enumerate(text.splitlines(), start=1):
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


def sanitize_traceback(stderr_text):
    """
    traceback 에서 Opus 전달용 요약을 추출한다

    traceback 본문에는 데이터 값(피험자 ID, 실제 수치, 자유기술 텍스트)이
    딸려 나올 수 있으므로 예외 타입과 발생 위치만 남기고 상세는 절단한다.

    Args:
        stderr_text: 원본 stderr 텍스트

    Returns:
        정제된 요약 문자열. traceback 이 없으면 빈 문자열
    """
    lines = [line for line in stderr_text.strip().splitlines() if line.strip()]
    if not lines:
        return ''

    # 마지막 줄이 보통 "ExceptionType: message"
    exception_line = lines[-1].strip()
    if ':' in exception_line:
        exc_type, _, detail = exception_line.partition(':')
        detail = detail.strip()
        if len(detail) > TRACEBACK_DETAIL_MAX_LENGTH:
            detail = detail[:TRACEBACK_DETAIL_MAX_LENGTH] + '...(절단됨)'
        exception_line = f"{exc_type.strip()}: {detail}"

    location = ''
    for line in reversed(lines):
        match = re.search(r'File "([^"]+)", line (\d+)', line)
        if match:
            location = f"{Path(match.group(1)).name}:{match.group(2)}"
            break

    return f"{exception_line} ({location})" if location else exception_line


def write_execution_log(context, stdout_text, stderr_text, findings, run_mode):
    """
    SAS .log 에 대응하는 execution.log 를 생성한다

    Args:
        context: 실행 컨텍스트
        stdout_text: 표준 출력 텍스트
        stderr_text: 표준 에러 텍스트
        findings: 로그 스캔 결과
        run_mode: 실행 방식 설명

    Returns:
        생성된 로그 파일 Path
    """
    study_root = context['study_root']
    log_path = Path(context['run_dir']) / 'execution.log'
    sep = '=' * 80
    sub = '-' * 80

    lines = [
        sep,
        'GXPLLM EXECUTION LOG',
        f"run_id      : {context['run_id']}",
        f"study_id    : {context['study_id']}",
        f"language    : {LANGUAGE}",
        f"program     : {context['program']['path']}",
        f"program_sha : {context['program']['sha256']}",
        f"user        : {current_user()}",
        f"hostname    : {current_hostname()}",
        f"started_at  : {context['started_at']}",
        f"python      : {(context.get('env_snapshot') or {}).get('python', {}).get('version', 'unknown')}",
        f"run_mode    : {run_mode}",
        f"purpose     : {context['purpose']}",
        f"sap_ref     : {context.get('sap_reference') or '(미지정)'}",
        sep,
        '',
        f'--- INPUTS {sub[11:]}',
    ]

    declared_inputs = context['meta'].get('inputs', [])
    if declared_inputs:
        for relative in declared_inputs:
            path = study_root / relative
            if path.is_file():
                lines.append(
                    f"{relative}  sha256={sha256_file(path)}  bytes={path.stat().st_size:,}"
                )
            else:
                lines.append(f"{relative}  <<< 파일 없음 >>>")
    else:
        lines.append('(GXPLLM-META 에 inputs 선언 없음)')

    lines += ['', f'--- STDOUT {sub[11:]}', stdout_text.rstrip() or '(없음)']
    lines += ['', f'--- STDERR {sub[11:]}', stderr_text.rstrip() or '(없음)']

    lines += ['', f'--- LOG SCAN {sub[13:]}']
    if findings:
        for finding in findings:
            lines.append(
                f"{finding['severity']:<9} [{finding['rule']}] L{finding['line']}: {finding['text']}"
            )
    else:
        lines.append('(문제 없음)')

    lines += ['', f'--- OUTPUTS {sub[12:]}']
    declared_outputs = context['meta'].get('outputs', [])
    if declared_outputs:
        for relative in declared_outputs:
            path = study_root / relative
            if path.is_file():
                lines.append(
                    f"{relative}  sha256={sha256_file(path)}  bytes={path.stat().st_size:,}"
                )
            else:
                lines.append(f"{relative}  <<< 생성되지 않음 >>>")
    else:
        lines.append('(GXPLLM-META 에 outputs 선언 없음)')

    lines += [
        '',
        f'--- SUMMARY {sub[12:]}',
        f"exit_code    : {context['exit_code']}",
        f"duration_sec : {context['duration_sec']:,.1f}",
        f"finished_at  : {context['finished_at']}",
        sep,
        '',
    ]

    log_path.write_text('\n'.join(lines), encoding='utf-8')
    return log_path


def main():
    """메인 함수"""
    print("=" * 80)
    print("Python 분석 실행 runner")
    print("=" * 80)

    parser = argparse.ArgumentParser(description='Python 프로그램을 실행하고 감사 증적을 남깁니다')
    parser.add_argument('--program', required=True, help='실행할 .py 파일 경로')
    parser.add_argument('--purpose', default='exploratory',
                        choices=['exploratory', 'qc', 'submission_candidate'])
    parser.add_argument('--allow-no-assertions', action='store_true',
                        help='assertion 0건을 실패로 보지 않습니다 (exploratory 전용, '
                             '감사 로그에 기록됨)')
    parser.add_argument('--timeout', type=int, default=DEFAULT_TIMEOUT_SEC)
    parser.add_argument('--no-uv', action='store_true',
                        help='uv 없이 현재 인터프리터로 실행합니다 (재현성 보장 안 됨)')
    args = parser.parse_args()

    # --- 1/6 준비 -----------------------------------------------------------
    print(f"\n[1/6] 실행 환경 준비...")
    try:
        context = preflight(args.program, args.purpose, args.allow_no_assertions)
    except (FileNotFoundError, ValueError) as exc:
        print(f"\n  오류: {exc}")
        sys.exit(2)

    context['language'] = LANGUAGE
    study_root = context['study_root']

    uv_exe = find_uv()
    uv_lock = study_root / '.gxpllm' / 'env' / 'uv.lock'
    use_uv = bool(uv_exe) and uv_lock.is_file() and not args.no_uv

    print(f"  study_id : {context['study_id']}")
    print(f"  run_id   : {context['run_id']}")
    print(f"  purpose  : {args.purpose}")
    print(f"  program  : {context['program']['path']} ({context['program']['lines']:,}줄)")

    if use_uv:
        print(f"  환경     : uv run --locked (uv.lock 적용)")
    else:
        print(f"  환경     : {sys.executable}")
        if args.purpose == 'submission_candidate':
            print(f"\n  오류: submission_candidate 는 환경 잠금이 필수입니다.")
            print(f"        uv 를 설치하고 .gxpllm/env/uv.lock 을 준비하십시오.")
            sys.exit(2)
        print(f"  경고     : 환경 잠금 없이 실행합니다. 재현성이 보장되지 않습니다.")

    announce_start(context)

    # --- 2/6 입력 확인 ------------------------------------------------------
    print(f"\n[2/6] 입력 데이터셋 확인...")
    for relative in context['meta'].get('inputs', []) or ['(선언 없음)']:
        if relative == '(선언 없음)':
            print(f"  선언된 입력 없음")
            break
        path = study_root / relative
        status = 'OK' if path.is_file() else '없음'
        size = f"{path.stat().st_size:,} bytes" if path.is_file() else '-'
        print(f"  [{status}] {relative}  {size}")

    # --- 3/6 실행 -----------------------------------------------------------
    print(f"\n[3/6] Python 실행 중...")
    cmd, run_mode = build_python_command(
        context['config'], context['program']['abs_path'], study_root, use_uv
    )

    started = datetime.now()
    context['started_at'] = started.astimezone().isoformat(timespec='seconds')

    exit_code, stdout, stderr = run_subprocess(
        cmd,
        cwd=study_root,
        run_dir=context['run_dir'],
        env={
            'GXPLLM_RUN_DIR': str(context['run_dir']),
            'GXPLLM_RUN_ID': context['run_id'],
            'GXPLLM_STUDY_ROOT': str(study_root),
            'PYTHONIOENCODING': 'utf-8',
            'PYTHONUTF8': '1',
        },
        timeout=args.timeout,
    )

    finished = datetime.now()
    context['finished_at'] = finished.astimezone().isoformat(timespec='seconds')
    context['duration_sec'] = round((finished - started).total_seconds(), 1)
    context['exit_code'] = exit_code
    context['command'] = ' '.join(f'"{c}"' if ' ' in str(c) else str(c) for c in cmd)

    stdout_text = stdout.decode('utf-8', errors='replace')
    stderr_text = stderr.decode('utf-8', errors='replace')

    print(f"  종료 코드: {exit_code}")
    print(f"  소요 시간: {context['duration_sec']:,.1f}초")

    # --- 4/6 로그 스캔 ------------------------------------------------------
    print(f"\n[4/6] 실행 출력 스캔...")
    findings = scan_python_log(stdout_text + '\n' + stderr_text)

    counts = {}
    for finding in findings:
        counts[finding['severity']] = counts.get(finding['severity'], 0) + 1

    if findings:
        for severity in ('ERROR', 'WARNING', 'CRITICAL', 'INFO'):
            if counts.get(severity):
                print(f"  {severity}: {counts[severity]:,}건")
        for finding in findings:
            if finding['severity'] in ('ERROR', 'CRITICAL'):
                print(f"    L{finding['line']:,} [{finding['rule']}] {finding['text'][:120]}")
    else:
        print(f"  문제 없음")

    if exit_code != 0 and stderr_text.strip():
        summary = sanitize_traceback(stderr_text)
        print(f"\n  예외 요약 (Opus 전달용): {summary}")
        context['sanitized_error'] = summary

    # --- 5/6 execution.log 생성 --------------------------------------------
    print(f"\n[5/6] execution.log 생성 및 manifest 작성...")
    log_path = write_execution_log(context, stdout_text, stderr_text, findings, run_mode)

    context['logs'] = {
        'execution_log': relative_to_root(log_path, study_root),
        'execution_lst': None,
        'log_sha256': sha256_file(log_path),
    }
    context['environment_extra'] = {
        'run_mode': run_mode,
        'uv_locked': use_uv,
        'uv_lock_sha256': sha256_file(uv_lock) if uv_lock.is_file() else None,
        'sanitized_error': context.get('sanitized_error'),
    }

    result, manifest = finalize(context, findings)
    print(f"  assertion: {manifest['assertions']['passed']:,}건 통과 / "
          f"{manifest['assertions']['failed']:,}건 실패")

    # --- 6/6 결과 -----------------------------------------------------------
    print(f"\n[6/6] 감사 로그 기록 완료")

    print_summary(context, result)
    sys.exit(1 if result == 'FAILED' else 0)


if __name__ == "__main__":
    main()
