"""
R 분석 실행 runner

R 프로그램을 Rscript --vanilla 로 실행하고 감사 증적을 남긴다.

R 특유의 주의점 (runner 가 강제하는 것)
- --vanilla : .Rprofile / .RData / 환경변수 로딩을 모두 끈다.
              이전 세션의 워크스페이스가 결과에 영향을 주지 못하게 한다. 재현성 필수 조건.
- warn 설정 : R 은 경고가 조용히 지나간다. runner 가 options(warn=) 을 주입한다.
              exploratory=1(즉시 출력), submission_candidate=2(경고를 오류로 승격)
- sessionInfo(): 프로그램 종료 시 자동 출력해 로그에 남긴다
- renv.lock : 환경 잠금. submission_candidate 에서는 필수

사용:
    python scripts/run_r.py --program programs/r/f_km_curve.R
    python scripts/run_r.py --program programs/r/f_km.R --purpose submission_candidate
"""

import _common  # noqa: F401  (sys.path 설정)

import argparse
import re
import shutil
import sys
import tempfile
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

LANGUAGE = 'r'
DEFAULT_TIMEOUT_SEC = 3600

# purpose 별 warn 옵션
#   1 = 경고를 즉시 출력
#   2 = 경고를 오류로 승격 (제출 경로)
WARN_LEVEL = {
    'exploratory': 1,
    'qc': 1,
    'submission_candidate': 2,
}

# R 실행 출력 스캔 규칙: (심각도, 규칙 ID, 정규식)
LOG_RULES = [
    ('ERROR',    'R_ERROR',            r'^\s*Error\b'),
    ('ERROR',    'R_EXECUTION_HALTED', r'Execution halted'),
    ('WARNING',  'R_WARNING',          r'^\s*Warning( message)?s?\b'),
    ('WARNING',  'R_IN_ADDITION',      r'^\s*In addition: Warning'),

    # --- 조용히 잘못된 결과를 만드는 R 특유의 경고 ---
    ('CRITICAL', 'NAS_INTRODUCED',     r'NAs introduced by coercion'),
    ('CRITICAL', 'LONGER_OBJECT',      r'longer object length is not a multiple'),
    ('CRITICAL', 'NUMBER_OF_ITEMS',    r'number of items to replace is not a multiple'),
    ('CRITICAL', 'INVALID_FACTOR',     r'invalid factor level, NA generated'),
    ('CRITICAL', 'ARGUMENT_LENGTH',    r'the condition has length > 1'),
    ('CRITICAL', 'MANY_TO_MANY',       r'Detected an unexpected many-to-many relationship'),
    ('CRITICAL', 'JOIN_ROWS',          r'Each row in `x` is expected to match at most 1 row'),
    ('CRITICAL', 'COERCE_DOUBLE',      r'Coercing (LHS|RHS) to a (double|character)'),
    ('CRITICAL', 'NAN_PRODUCED',       r'NaNs produced'),

    ('INFO',     'DEPRECATED',         r'(deprecated|Deprecated|superseded)'),
    ('INFO',     'MASKED_OBJECTS',     r'The following objects are masked'),
]

COMPILED_RULES = [(sev, rule, re.compile(pattern)) for sev, rule, pattern in LOG_RULES]

LOG_LINE_MAX_LENGTH = 300

# 프로그램 실행 전후에 주입할 R 코드
PREAMBLE_TEMPLATE = """
# --- gxpllm runner preamble (자동 주입) --------------------------------------
options(warn = {warn_level})
options(digits = 15)
options(stringsAsFactors = FALSE)
Sys.setenv(GXPLLM_RUN_DIR = "{run_dir}")
Sys.setenv(GXPLLM_RUN_ID = "{run_id}")
Sys.setenv(GXPLLM_STUDY_ROOT = "{study_root}")
{renv_restore}
# ----------------------------------------------------------------------------

"""

POSTAMBLE = """

# --- gxpllm runner postamble (자동 주입) -------------------------------------
cat("\\n--- SESSION INFO ---\\n")
print(sessionInfo())
cat("\\n--- LOCALE ---\\n")
cat(Sys.getlocale(), "\\n")
# ----------------------------------------------------------------------------
"""


# ============================================================================
# 메인 로직
# ============================================================================

def find_rscript():
    """
    Rscript 실행 파일을 찾는다

    Returns:
        Rscript 경로 문자열. 없으면 None
    """
    return shutil.which('Rscript') or shutil.which('Rscript.exe')


def build_wrapper_script(context, program_abs, use_renv, warn_level):
    """
    preamble / postamble 을 붙인 임시 실행 스크립트를 만든다

    원본 프로그램을 수정하지 않으면서 재현성 옵션과 sessionInfo 출력을 강제한다.
    source() 로 원본을 부르므로 오류 위치가 원본 파일 기준으로 보고된다.

    Args:
        context: 실행 컨텍스트
        program_abs: 원본 .R 절대경로
        use_renv: renv::restore() 를 넣을지 여부
        warn_level: options(warn=) 값

    Returns:
        생성된 래퍼 스크립트 Path
    """
    def r_path(path):
        """R 문자열 리터럴용으로 경로를 이스케이프한다"""
        return str(path).replace('\\', '/')

    renv_restore = ''
    if use_renv:
        renv_restore = (
            'if (requireNamespace("renv", quietly = TRUE)) '
            '{ renv::restore(prompt = FALSE) }'
        )

    preamble = PREAMBLE_TEMPLATE.format(
        warn_level=warn_level,
        run_dir=r_path(context['run_dir']),
        run_id=context['run_id'],
        study_root=r_path(context['study_root']),
        renv_restore=renv_restore,
    )

    body = f'source("{r_path(program_abs)}", echo = FALSE, keep.source = TRUE)\n'

    wrapper_path = Path(context['run_dir']) / '_gxpllm_wrapper.R'
    wrapper_path.write_text(preamble + body + POSTAMBLE, encoding='utf-8')
    return wrapper_path


def scan_r_log(text):
    """
    R 실행 출력에서 문제 항목을 찾는다

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


def sanitize_r_error(stderr_text):
    """
    R 오류 메시지에서 Opus 전달용 요약을 추출한다

    R 오류에는 데이터 값이 그대로 들어가는 경우가 많으므로 절단한다

    Args:
        stderr_text: 원본 stderr 텍스트

    Returns:
        정제된 요약 문자열
    """
    for line in stderr_text.splitlines():
        stripped = line.strip()
        if stripped.startswith('Error'):
            if len(stripped) > 80:
                return stripped[:80] + '...(절단됨)'
            return stripped
    return ''


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
    sub = '-' * 69

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
        f"R           : {(context.get('env_snapshot') or {}).get('r', {}).get('version', 'unknown')}",
        f"run_mode    : {run_mode}",
        f"purpose     : {context['purpose']}",
        f"sap_ref     : {context.get('sap_reference') or '(미지정)'}",
        sep,
        '',
        f'--- INPUTS {sub}',
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

    lines += ['', f'--- STDOUT {sub}', stdout_text.rstrip() or '(없음)']
    lines += ['', f'--- STDERR {sub}', stderr_text.rstrip() or '(없음)']

    lines += ['', f'--- LOG SCAN {sub}']
    if findings:
        for finding in findings:
            lines.append(
                f"{finding['severity']:<9} [{finding['rule']}] L{finding['line']}: {finding['text']}"
            )
    else:
        lines.append('(문제 없음)')

    lines += ['', f'--- OUTPUTS {sub}']
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
        f'--- SUMMARY {sub}',
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
    print("R 분석 실행 runner")
    print("=" * 80)

    parser = argparse.ArgumentParser(description='R 프로그램을 실행하고 감사 증적을 남깁니다')
    parser.add_argument('--program', required=True, help='실행할 .R 파일 경로')
    parser.add_argument('--purpose', default='exploratory',
                        choices=['exploratory', 'qc', 'submission_candidate'])
    parser.add_argument('--allow-no-assertions', action='store_true',
                        help='assertion 0건을 실패로 보지 않습니다 (exploratory 전용, '
                             '감사 로그에 기록됨)')
    parser.add_argument('--timeout', type=int, default=DEFAULT_TIMEOUT_SEC)
    parser.add_argument('--no-renv', action='store_true',
                        help='renv::restore() 를 건너뜁니다 (재현성 보장 안 됨)')
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

    rscript = find_rscript()
    if not rscript:
        print(f"\n  오류: Rscript 를 찾을 수 없습니다. PATH 를 확인하십시오.")
        sys.exit(2)

    renv_lock = study_root / '.gxpllm' / 'env' / 'renv.lock'
    use_renv = renv_lock.is_file() and not args.no_renv
    warn_level = WARN_LEVEL[args.purpose]

    print(f"  study_id : {context['study_id']}")
    print(f"  run_id   : {context['run_id']}")
    print(f"  purpose  : {args.purpose}")
    print(f"  program  : {context['program']['path']} ({context['program']['lines']:,}줄)")
    print(f"  Rscript  : {rscript}")
    print(f"  warn     : options(warn={warn_level})"
          f"{' — 경고를 오류로 승격' if warn_level == 2 else ''}")

    if use_renv:
        print(f"  환경     : renv.lock 적용")
    else:
        print(f"  환경     : renv 미적용")
        if args.purpose == 'submission_candidate':
            print(f"\n  오류: submission_candidate 는 환경 잠금이 필수입니다.")
            print(f"        .gxpllm/env/renv.lock 을 준비하십시오.")
            sys.exit(2)
        print(f"  경고     : 환경 잠금 없이 실행합니다. 재현성이 보장되지 않습니다.")

    announce_start(context)

    # --- 2/6 입력 확인 ------------------------------------------------------
    print(f"\n[2/6] 입력 데이터셋 확인...")
    declared_inputs = context['meta'].get('inputs', [])
    if declared_inputs:
        for relative in declared_inputs:
            path = study_root / relative
            status = 'OK' if path.is_file() else '없음'
            size = f"{path.stat().st_size:,} bytes" if path.is_file() else '-'
            print(f"  [{status}] {relative}  {size}")
    else:
        print(f"  선언된 입력 없음")

    # --- 3/6 실행 -----------------------------------------------------------
    print(f"\n[3/6] R 실행 중...")
    wrapper = build_wrapper_script(
        context, context['program']['abs_path'], use_renv, warn_level
    )
    cmd = [rscript, '--vanilla', str(wrapper)]
    run_mode = f"Rscript --vanilla (warn={warn_level}, renv={'적용' if use_renv else '미적용'})"

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
            'R_LIBS_USER': str(study_root / '.gxpllm' / 'env' / 'rlibs'),
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
    findings = scan_r_log(stdout_text + '\n' + stderr_text)

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
        summary = sanitize_r_error(stderr_text)
        if summary:
            print(f"\n  오류 요약 (Opus 전달용): {summary}")
            context['sanitized_error'] = summary

    # --- 5/6 execution.log 생성 --------------------------------------------
    print(f"\n[5/6] execution.log 생성 및 manifest 작성...")
    log_path = write_execution_log(context, stdout_text, stderr_text, findings, run_mode)

    context['logs'] = {
        'execution_log': relative_to_root(log_path, study_root),
        'execution_lst': None,
        'log_sha256': sha256_file(log_path),
        'wrapper_sha256': sha256_file(wrapper),
    }
    context['environment_extra'] = {
        'run_mode': run_mode,
        'renv_locked': use_renv,
        'renv_lock_sha256': sha256_file(renv_lock) if renv_lock.is_file() else None,
        'warn_level': warn_level,
        'rscript': rscript,
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
