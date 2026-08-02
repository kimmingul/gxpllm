"""
Runner 및 assertion emitter 검증 테스트

세 언어(SAS / Python / R)의 assertion 이 동일한 형식을 내는지,
runner 가 manifest 와 감사 로그를 올바르게 남기는지 확인한다.

SAS 와 R 은 설치되어 있지 않으면 해당 테스트를 건너뛴다.
Python 경로는 항상 검증한다.

실행:
    python tests/test_runners.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

# ============================================================================
# 설정 영역 - 여기서 수정하세요
# ============================================================================

STUDY_ID = 'TEST-RUN'

REQUIRED_ASSERTION_KEYS = ('label', 'rule', 'result', 'language', 'message')

CONFIG_CONTENT = {
    "study_id": STUDY_ID,
    "sas_exe": "C:\\Program Files\\SASHome\\SASFoundation\\9.4\\sas.exe",
    "sas_log_encoding": "cp949",
    "llm_endpoint": "http://dgx-spark.internal:8001/v1",
    "blinded": False,
}

# 테스트용 Python 분석 프로그램
PYTHON_PROGRAM = '''"""
테스트용 분석 프로그램

GXPLLM-META-BEGIN
program      : t_test.py
purpose      : runner 동작 검증
sap_ref      : docs/sap.md#test
inputs       : data/derived/adsl.json
outputs      : output/tables/t_test.txt
analysis_set : Safety Set (SAFFL='Y')
GXPLLM-META-END
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.environ['GXPLLM_PLUGIN_ROOT'] + '/scripts')
import gxpllm_assert as na


def main():
    """메인 함수"""
    root = Path(os.environ['GXPLLM_STUDY_ROOT'])

    print("[1/3] 데이터 로드...")
    with open(root / 'data' / 'derived' / 'adsl.json', encoding='utf-8') as f:
        records = json.load(f)
    print(f"  ADSL: {len(records):,}행")

    print("[2/3] 검증...")
    na.assert_rowcount(records, label='ADSL_LOADED', expected_min=1)

    subjects = [r['USUBJID'] for r in records]
    unique_subjects = len(set(subjects))
    na.assert_le(unique_subjects, len(records),
                 label='SUBJ_LE_ROWS', expression='unique subjects <= rows')

    saf = [r for r in records if r.get('SAFFL') == 'Y']
    na.assert_rowcount_delta(len(records), len(saf), label='SAFETY_FILTER',
                             max_loss_rate=0.5)

    na.assert_sum_equals([len(saf), len(records) - len(saf)], len(records),
                         label='ARM_SUM_CHECK')

    print("[3/3] 출력 생성...")
    out = root / 'output' / 'tables' / 't_test.txt'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(f"Safety Set: {len(saf)}\\n", encoding='utf-8')

    na.summary()


if __name__ == "__main__":
    main()
'''

# 테스트용 R 분석 프로그램
R_PROGRAM = '''# GXPLLM-META-BEGIN
# program      : t_test.R
# purpose      : runner 동작 검증
# sap_ref      : docs/sap.md#test
# inputs       : data/derived/adsl.json
# outputs      : output/tables/t_test_r.txt
# analysis_set : Safety Set (SAFFL='Y')
# GXPLLM-META-END

source(file.path(Sys.getenv("GXPLLM_PLUGIN_ROOT"), "scripts", "gxpllm_assert.R"))

root <- Sys.getenv("GXPLLM_STUDY_ROOT")

cat("[1/3] 데이터 로드...\\n")
lines <- readLines(file.path(root, "data", "derived", "adsl.json"), warn = FALSE)
txt <- paste(lines, collapse = "")
n_records <- lengths(regmatches(txt, gregexpr("USUBJID", txt)))
df <- data.frame(
  USUBJID = paste0("S", seq_len(n_records)),
  SAFFL = rep("Y", n_records),
  stringsAsFactors = FALSE
)
cat(sprintf("  ADSL: %d행\\n", nrow(df)))

cat("[2/3] 검증...\\n")
gxpllm_assert_rowcount(df, label = "ADSL_LOADED", expected_min = 1)
gxpllm_assert_unique(df, keys = "USUBJID", label = "ADSL_UNIQUE_SUBJ")
gxpllm_assert_domain(df, "SAFFL", c("Y", "N"), label = "SAFFL_DOMAIN")
gxpllm_assert_denominator(df, "USUBJID", nrow(df), label = "DENOM_CHECK")

cat("[3/3] 출력 생성...\\n")
out_dir <- file.path(root, "output", "tables")
dir.create(out_dir, recursive = TRUE, showWarnings = FALSE)
writeLines(sprintf("Safety Set: %d", nrow(df)),
           file.path(out_dir, "t_test_r.txt"))

gxpllm_assert_summary()
'''


# ============================================================================
# 메인 로직
# ============================================================================

def make_study(tmpdir):
    """
    테스트용 study 디렉터리를 만든다

    Args:
        tmpdir: 임시 디렉터리 경로

    Returns:
        study 루트 Path
    """
    root = Path(tmpdir) / STUDY_ID
    for sub in ('.gxpllm/env', 'data/raw', 'data/derived', 'docs',
                'programs/sas', 'programs/python', 'programs/r',
                'output/tables', 'output/figures', 'output/listings',
                'logs/runs', 'audit'):
        (root / sub).mkdir(parents=True, exist_ok=True)

    with open(root / '.gxpllm' / 'config.json', 'w', encoding='utf-8') as f:
        json.dump(CONFIG_CONTENT, f, ensure_ascii=False, indent=2)

    # 테스트용 데이터 (실제 PHI 아님)
    records = [
        {'USUBJID': f'ABC-301-{i:04d}', 'SAFFL': 'Y' if i % 10 else 'N',
         'AGE': 40 + (i % 40), 'SEX': 'M' if i % 2 else 'F'}
        for i in range(1, 51)
    ]
    with open(root / 'data' / 'derived' / 'adsl.json', 'w', encoding='utf-8') as f:
        json.dump(records, f, ensure_ascii=False)

    (root / 'programs' / 'python' / 't_test.py').write_text(PYTHON_PROGRAM, encoding='utf-8')
    (root / 'programs' / 'r' / 't_test.R').write_text(R_PROGRAM, encoding='utf-8')

    return root


def run_runner(runner_name, program_path, study_root, extra_args=()):
    """
    runner 를 실행한다

    Args:
        runner_name: run_python.py 등
        program_path: 실행할 프로그램 경로
        study_root: study 루트 Path
        extra_args: 추가 인자 튜플

    Returns:
        (exit_code, stdout, stderr)
    """
    env = dict(os.environ)
    env['GXPLLM_PLUGIN_ROOT'] = str(PLUGIN_ROOT).replace('\\', '/')
    env['PYTHONIOENCODING'] = 'utf-8'

    cmd = [
        sys.executable,
        str(PLUGIN_ROOT / 'scripts' / runner_name),
        '--program', str(program_path),
    ] + list(extra_args)

    result = subprocess.run(
        cmd, capture_output=True, cwd=str(study_root), env=env, timeout=300,
    )
    return (
        result.returncode,
        (result.stdout or b'').decode('utf-8', errors='replace'),
        (result.stderr or b'').decode('utf-8', errors='replace'),
    )


def run_dir_from_output(study_root, stdout):
    """
    runner 출력에서 run_id 를 추출해 run 디렉터리를 돌려준다

    run_id 는 '{timestamp}-{6자리 hex}' 형식이라 알파벳 정렬로는
    최신 run 을 고를 수 없다 (같은 초에 생성되면 hex 접미사가 순서를 결정).
    따라서 runner 가 출력한 run_id 를 직접 읽는다.

    Args:
        study_root: study 루트 Path
        stdout: runner 표준 출력

    Returns:
        run 디렉터리 Path. 찾지 못하면 None
    """
    import re

    match = re.search(r'run_id\s*:\s*(\S+)', stdout)
    if match:
        candidate = study_root / 'logs' / 'runs' / match.group(1)
        if candidate.is_dir():
            return candidate

    # 폴백: mtime 기준
    runs = [p for p in (study_root / 'logs' / 'runs').glob('*') if p.is_dir()]
    if not runs:
        return None
    return max(runs, key=lambda p: p.stat().st_mtime)


def validate_assertions(run_dir):
    """
    assertions.json 형식을 검증한다

    Args:
        run_dir: run 디렉터리 Path

    Returns:
        (문제 리스트, assertion 수)
    """
    problems = []
    path = run_dir / 'assertions.json'

    if not path.is_file():
        return ['assertions.json 이 생성되지 않았습니다'], 0

    with open(path, encoding='utf-8') as f:
        data = json.load(f)

    assertions = data.get('assertions', [])
    if not assertions:
        problems.append('assertion 이 하나도 기록되지 않았습니다')

    for index, item in enumerate(assertions):
        for key in REQUIRED_ASSERTION_KEYS:
            if key not in item:
                problems.append(f"assertion[{index}] 에 '{key}' 없음")
        if item.get('result') not in ('PASS', 'FAIL'):
            problems.append(f"assertion[{index}] result 값이 이상함: {item.get('result')}")

    return problems, len(assertions)


def validate_manifest(run_dir, expected_language):
    """
    manifest.json 형식을 검증한다

    Args:
        run_dir: run 디렉터리 Path
        expected_language: 기대 언어

    Returns:
        문제 리스트
    """
    problems = []
    path = run_dir / 'manifest.json'

    if not path.is_file():
        return ['manifest.json 이 생성되지 않았습니다']

    with open(path, encoding='utf-8') as f:
        manifest = json.load(f)

    required = ('run_id', 'study_id', 'language', 'purpose', 'program',
                'user', 'hostname', 'started_at', 'finished_at', 'exit_code',
                'inputs', 'outputs', 'logs', 'assertions', 'environment', 'result')
    for key in required:
        if key not in manifest:
            problems.append(f"manifest 에 '{key}' 없음")

    if manifest.get('language') != expected_language:
        problems.append(f"language 불일치: {manifest.get('language')} != {expected_language}")

    program = manifest.get('program') or {}
    if not program.get('sha256'):
        problems.append('program.sha256 없음')

    for item in manifest.get('inputs', []):
        if item.get('exists') and not item.get('sha256'):
            problems.append(f"입력 {item.get('path')} 에 sha256 없음")

    return problems


def validate_boundary(run_dir):
    """
    실행 로그가 경계 정책상 차단 대상인지 확인한다

    Args:
        run_dir: run 디렉터리 Path

    Returns:
        문제 리스트
    """
    from gxpllm.core import classify_path, find_study_root

    problems = []
    study_root, config = find_study_root(run_dir)

    must_block = ['stdout.txt', 'stderr.txt', 'execution.log']
    must_allow = ['manifest.json', 'assertions.json']

    for name in must_block:
        target = run_dir / name
        if not target.is_file():
            continue
        if classify_path(target, study_root, config) is None:
            problems.append(f"{name} 이 차단되지 않습니다 (PHI 유출 경로)")

    for name in must_allow:
        target = run_dir / name
        if not target.is_file():
            continue
        if classify_path(target, study_root, config) is not None:
            problems.append(f"{name} 이 차단됩니다 (정상 작업 방해)")

    return problems


def validate_audit(study_root, expected_events):
    """
    감사 로그를 검증한다

    Args:
        study_root: study 루트 Path
        expected_events: 기대하는 이벤트 이름 집합

    Returns:
        (문제 리스트, 항목 수)
    """
    from gxpllm.core import audit_path_for, verify_audit_chain

    problems = []
    audit_path = audit_path_for(study_root)

    if not audit_path.is_file():
        return ['audit.jsonl 이 생성되지 않았습니다'], 0

    ok, chain_problems, count = verify_audit_chain(audit_path)
    if not ok:
        problems.extend(chain_problems)

    events = set()
    with open(audit_path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                events.add(json.loads(line).get('event'))

    for expected in expected_events:
        if expected not in events:
            problems.append(f"감사 이벤트 '{expected}' 가 기록되지 않았습니다")

    return problems, count


def test_python_runner(study_root):
    """Python runner 를 검증한다"""
    print(f"\n[2/5] Python runner 검증...")
    problems = []

    code, stdout, stderr = run_runner(
        'run_python.py', study_root / 'programs' / 'python' / 't_test.py', study_root
    )
    print(f"  종료 코드: {code}")

    run_dir = run_dir_from_output(study_root, stdout)
    if run_dir is None:
        return ['run 디렉터리가 생성되지 않았습니다']

    assertion_problems, assertion_count = validate_assertions(run_dir)
    problems.extend(assertion_problems)
    print(f"  assertion: {assertion_count:,}건 기록")

    problems.extend(validate_manifest(run_dir, 'python'))
    problems.extend(validate_boundary(run_dir))

    if code != 0:
        problems.append(f"실행 실패 (exit {code}). stderr: {stderr[:300]}")

    for item in problems:
        print(f"  FAIL {item}")
    if not problems:
        print(f"  OK   manifest / assertions / 경계 정책 모두 정상")

    return problems


def test_undeclared_output(study_root):
    """
    선언되지 않은 산출물 탐지를 검증한다

    2차 검토 C-NEW-5: programs -> runner -> output/tables 경로로
    피험자 단위 데이터를 Opus 가 읽을 수 있는 영역에 덤프할 수 있었다.
    runner 가 GXPLLM-META 에 없는 산출물을 탐지해 실패로 처리해야 한다.
    """
    print(f"\n[4/5] 선언되지 않은 산출물 탐지...")
    problems = []

    exfil_program = '''"""
선언되지 않은 산출물을 만드는 프로그램 (탐지 테스트용)

GXPLLM-META-BEGIN
program      : t_exfil.py
purpose      : 미선언 산출물 탐지 검증
inputs       : data/derived/adsl.json
outputs      : output/tables/t_declared.txt
GXPLLM-META-END
"""

import json
import os
from pathlib import Path


def main():
    """메인 함수"""
    root = Path(os.environ['GXPLLM_STUDY_ROOT'])

    print("[1/2] 선언된 산출물 생성...")
    (root / 'output' / 'tables' / 't_declared.txt').write_text('ok', encoding='utf-8')

    print("[2/2] 선언되지 않은 산출물 생성 (덤프 시뮬레이션)...")
    with open(root / 'data' / 'derived' / 'adsl.json', encoding='utf-8') as f:
        records = json.load(f)
    subjects = '\\n'.join(r['USUBJID'] for r in records)

    # (a) output/tables 에 덤프
    (root / 'output' / 'tables' / '_leak.csv').write_text(subjects, encoding='utf-8')

    # (b) 감시 밖으로 알려졌던 디렉터리에 덤프 (3차 검토 HIGH-4)
    (root / 'macros' / '_leak2.csv').write_text(subjects, encoding='utf-8')

    # (c) 기존 파일 내용 변경 후 mtime 복원 (3차 검토 HIGH-5)
    victim = root / 'docs' / 'existing.md'
    original_stat = victim.stat()
    victim.write_text(subjects, encoding='utf-8')
    os.utime(victim, (original_stat.st_atime, original_stat.st_mtime))


if __name__ == "__main__":
    main()
'''

    # mtime 복원 우회 검증용 기존 파일 (실행 전부터 존재해야 한다)
    (study_root / 'docs' / 'existing.md').write_text('원본 내용\n', encoding='utf-8')
    (study_root / 'macros').mkdir(parents=True, exist_ok=True)

    program_path = study_root / 'programs' / 'python' / 't_exfil.py'
    program_path.write_text(exfil_program, encoding='utf-8')

    code, stdout, stderr = run_runner('run_python.py', program_path, study_root)

    run_dir = run_dir_from_output(study_root, stdout)
    if run_dir is None:
        return ['run 디렉터리가 생성되지 않았습니다']

    with open(run_dir / 'manifest.json', encoding='utf-8') as f:
        manifest = json.load(f)

    undeclared = manifest.get('undeclared_outputs', [])
    paths = [item.get('path', '') for item in undeclared]

    expectations = [
        ('_leak.csv',  'output/tables 덤프'),
        ('_leak2.csv', 'macros 덤프 (감시 범위 확대)'),
        ('existing.md', 'mtime 복원 우회 (해시 비교)'),
    ]
    for marker, description in expectations:
        if not any(marker in p for p in paths):
            problems.append(f"{description} 를 탐지하지 못했습니다 ({marker}) — 탐지: {paths}")

    if paths:
        print(f"  탐지: {', '.join(paths)}")

    if manifest.get('result') != 'FAILED':
        problems.append(f"미선언 산출물이 있는데 result 가 {manifest.get('result')} 입니다")

    if code == 0:
        problems.append(f"미선언 산출물이 있는데 종료 코드가 0 입니다")

    for item in problems:
        print(f"  FAIL {item}")
    if not problems:
        print(f"  OK   미선언 산출물 탐지 및 실패 처리 정상")

    return problems


def test_r_runner(study_root):
    """R runner 를 검증한다 (R 미설치 시 건너뜀)"""
    print(f"\n[3/5] R runner 검증...")

    if not (shutil.which('Rscript') or shutil.which('Rscript.exe')):
        print(f"  SKIP  Rscript 를 찾을 수 없습니다")
        return []

    problems = []
    code, stdout, stderr = run_runner(
        'run_r.py', study_root / 'programs' / 'r' / 't_test.R', study_root
    )
    print(f"  종료 코드: {code}")

    run_dir = run_dir_from_output(study_root, stdout)
    assertion_problems, assertion_count = validate_assertions(run_dir)
    problems.extend(assertion_problems)
    print(f"  assertion: {assertion_count:,}건 기록")

    problems.extend(validate_manifest(run_dir, 'r'))
    problems.extend(validate_boundary(run_dir))

    for item in problems:
        print(f"  FAIL {item}")
    if not problems:
        print(f"  OK   manifest / assertions / 경계 정책 모두 정상")

    return problems


ZERO_ASSERTION_PROGRAM = '''"""
assertion 이 없는 프로그램 (판정 정책 검증용)

GXPLLM-META-BEGIN
program      : profile_only.py
purpose      : assertion 을 요구하지 않는 정당한 작업
inputs       :
outputs      : output/tables/profile_only.txt
GXPLLM-META-END
"""

import os
from pathlib import Path


def main():
    """메인 함수"""
    root = Path(os.environ['GXPLLM_STUDY_ROOT'])
    out = root / 'output' / 'tables' / 'profile_only.txt'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text('profile output\\n', encoding='utf-8')


if __name__ == "__main__":
    main()
'''


def test_zero_assertion_policy(study_root):
    """
    assertion 0건 판정 정책을 검증한다

    assertion 이 0건이면 "검증이 통과했다" 가 아니라 "검증이 없었다" 다.
    실패 수만 보면 assertion 이 통째로 빠진 프로그램이 PASSED 로 나온다.

    잘린 LLM 응답(finish_reason=length)으로 저장된 프로그램이 정확히
    이 경로를 탄다. MCP 쪽 방어가 뚫려도 runner 가 막아야 한다.

    판정 함수와 CLI 플래그 배선을 함께 본다.

    Args:
        study_root: 테스트 study 루트 Path

    Returns:
        문제 리스트
    """
    print(f"\n[5/6] assertion 0건 판정 정책...")
    problems = []

    sys.path.insert(0, str(PLUGIN_ROOT / 'scripts'))
    from _common import decide_result

    def judge(purpose, total, allow=False, failed=0):
        """판정을 수행한다"""
        return decide_result(purpose, 0, [], failed, [], [],
                             assertion_total=total, allow_no_assertions=allow)

    cases = (
        # (설명, purpose, total, allow, 기대 result)
        ('exploratory + 0건 + 플래그 없음',
         'exploratory', 0, False, 'FAILED'),
        ('exploratory + 0건 + --allow-no-assertions',
         'exploratory', 0, True, 'PASSED'),
        ('qc + 0건 + 플래그 (플래그가 통하면 안 됨)',
         'qc', 0, True, 'FAILED'),
        ('submission_candidate + 0건 + 플래그 (플래그가 통하면 안 됨)',
         'submission_candidate', 0, True, 'FAILED'),
        # --- 오탐 점검: 정상 실행은 그대로 통과해야 한다 --------------------
        ('exploratory + 3건 통과',
         'exploratory', 3, False, 'PASSED'),
        ('submission_candidate + 12건 통과',
         'submission_candidate', 12, False, 'PASSED'),
        ('assertion_total 미전달 (하위 호환)',
         'exploratory', None, False, 'PASSED'),
    )

    for description, purpose, total, allow, expected in cases:
        result, reasons = judge(purpose, total, allow)
        if result != expected:
            problems.append(
                f"{description}: {result} (기대 {expected}) — 사유: {reasons}"
            )
        else:
            print(f"  OK   {description} → {result}")

    # 실패 assertion 이 있으면 0건 사유가 중복되지 않아야 한다
    result, reasons = judge('exploratory', 5, False, failed=2)
    if result != 'FAILED':
        problems.append(f"assertion 실패 2건인데 {result}")
    elif any('0건' in r for r in reasons):
        problems.append(f"실패 assertion 이 있는데 0건 사유가 붙었습니다: {reasons}")
    else:
        print(f"  OK   assertion 실패 2건 → FAILED (사유 중복 없음)")

    # --- CLI 플래그 배선 (end-to-end) ---------------------------------------
    program = study_root / 'programs' / 'python' / 'profile_only.py'
    program.write_text(ZERO_ASSERTION_PROGRAM, encoding='utf-8')

    for extra_args, expected, label in (
        ((), 'FAILED', '플래그 없이 실행'),
        (('--allow-no-assertions',), 'PASSED', '--allow-no-assertions 로 실행'),
    ):
        code, stdout, stderr = run_runner('run_python.py', program, study_root,
                                          extra_args=extra_args)
        run_dir = run_dir_from_output(study_root, stdout)
        if run_dir is None:
            problems.append(f'{label}: run 디렉터리 없음 (exit {code}) {stderr[:200]}')
            continue

        with open(run_dir / 'manifest.json', encoding='utf-8') as f:
            manifest = json.load(f)

        summary = manifest.get('assertions') or {}
        if summary.get('total') != 0:
            problems.append(f"{label}: assertion 이 {summary.get('total')}건 기록됨 "
                            f"(0건이어야 검증이 성립)")
            continue

        if manifest.get('result') != expected:
            problems.append(
                f"{label}: {manifest.get('result')} (기대 {expected}) — "
                f"사유: {manifest.get('failure_reasons')}"
            )
            continue

        # 허용했다는 사실이 증적에 남아야 한다
        allowed = summary.get('no_assertions_allowed')
        if allowed != bool(extra_args):
            problems.append(
                f"{label}: manifest 의 no_assertions_allowed 가 {allowed!r} "
                f"(기대 {bool(extra_args)!r}) — 감사 증적이 사실과 다릅니다"
            )
            continue

        print(f"  OK   {label} → {manifest['result']} "
              f"(no_assertions_allowed={allowed})")

    for item in problems:
        print(f"  FAIL {item}")

    return problems


def test_sas_available(study_root):
    """SAS 설치 여부를 확인한다"""
    print(f"\n[6/6] SAS runner 확인...")
    sas_exe = Path(CONFIG_CONTENT['sas_exe'])
    if not sas_exe.is_file():
        print(f"  SKIP  SAS 9.4 를 찾을 수 없습니다: {sas_exe}")
        print(f"        실제 PC 에서 별도 검증이 필요합니다.")
        return []
    print(f"  SAS 발견: {sas_exe}")
    return []


def main():
    """메인 함수"""
    print("=" * 80)
    print("Runner 및 assertion emitter 검증")
    print("=" * 80)

    all_problems = []

    with tempfile.TemporaryDirectory() as tmpdir:
        print(f"\n[1/6] 테스트 study 준비...")
        study_root = make_study(tmpdir)
        print(f"  {study_root}")

        all_problems.extend(test_python_runner(study_root))
        all_problems.extend(test_r_runner(study_root))
        all_problems.extend(test_undeclared_output(study_root))
        all_problems.extend(test_zero_assertion_policy(study_root))
        all_problems.extend(test_sas_available(study_root))

        audit_problems, audit_count = validate_audit(
            study_root, {'run_started', 'run_finished'}
        )
        all_problems.extend(audit_problems)
        print(f"\n  감사 로그: {audit_count:,}건, 체인 {'정상' if not audit_problems else '문제'}")

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
