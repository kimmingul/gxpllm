"""
일상 명령 오탐 점검

통계 프로그래머가 실제로 칠 법한 명령이 hook 에 막히지 않는지 확인한다.

**오탐은 보안 이슈다.**
정당한 작업이 막히면 사용자는 plugin 을 끈다. 그것이 가장 현실적인 메타 우회이며,
어떤 기술적 차단보다 확실하게 경계를 무너뜨린다.
따라서 오탐 회귀를 보안 회귀와 동일한 우선순위로 다룬다.

새 명령이 막힌다는 보고를 받으면
  1. 여기에 케이스를 추가한다
  2. 차단이 의도된 것인지 판단한다 (예: python -c 는 의도된 차단)
  3. 의도되지 않았으면 즉시 수정한다

실행:
    python tests/test_false_positives.py
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

# ============================================================================
# 설정 영역 - 여기서 수정하세요
# ============================================================================

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
GUARD_BASH = PLUGIN_ROOT / 'hooks' / 'guard_bash.py'

EXIT_ALLOW = 0
EXIT_BLOCK = 2

# 통계 프로그래머의 일상 명령 (모두 허용되어야 한다)
EVERYDAY_COMMANDS = [
    'git status',
    'git add programs/sas/t_dm.sas',
    'git commit -m "add demographics table"',
    'git branch -a',
    'git checkout -b feature/ae-summary',
    'git log --oneline -20',
    'git remote -v',
    'git config user.name',
    'git stash list',
    'dir output\\tables',
    'type docs\\sap.md',
    'copy programs\\sas\\t1.sas programs\\sas\\t2.sas',
    'del programs\\sas\\old.sas',
    'mkdir output\\tables\\draft',
    'fc programs\\sas\\a.sas programs\\sas\\b.sas',
    'ruff check programs/python',
    'black --check programs/python',
    'mypy programs/python',
    'pytest tests',
    'code programs/sas/t_dm.sas',
    'notepad docs\\sap.md',
    'echo analysis complete',
    'echo update the data dictionary',
    'cls',
    'python scripts/run_sas.py --program programs/sas/t.sas',
    'python scripts/run_python.py --program programs/python/t.py --purpose qc',
    'python scripts/run_python.py --program programs/python/profile.py --allow-no-assertions',
    'python scripts/run_r.py --program programs/r/f_km.R --allow-no-assertions --no-renv',
    'python scripts/verify_audit.py --study .',
    'python scripts/compare_outputs.py --primary a.json --qc b.json',
    'python scripts/init_study.py --root D:\\clinical\\X --study-id X',
    'python scripts/benchmark_codegen.py --cases benchmark/cases.yaml --study .',
    # 4차 검토에서 확인된 오탐 (변수 확장 / 조회 명령)
    'echo Today is %DATE%',
    'echo %DATE% %TIME%',
    'echo check $nobs in log',
    'echo use ${nobs} from SAS',
    'echo $ErrorActionPreference',
    'set PURPOSE=exploratory',
    'git log --format=%H',
    'echo 50% complete',
    'where python',
    'which python',
    'command -v python',
    'Get-Command python',
]

# 반드시 차단되어야 하는 명령 (누수 확인)
MUST_BLOCK_COMMANDS = [
    'env python programs/python/payload',
    'call python payload',
    'timeout 5 python payload',
    'timeout /T 5 python payload',
    'env.exe python payload',
    'busybox python payload',
    'pipenv run python payload',
    'coverage run programs/python/payload',
    'set PY=python && %PY% -c "print(1)"',
    '!PY! -c print(1)',
    'git log -p',
    'git -C . show HEAD:data/raw/x.csv',
    'git --no-pager log -p',
    'git format-patch -1 HEAD',
    'powershell -File x.ps1',
    'cmd.exe /c "python -c print(1)"',
    'sas.exe -sysin x.sas',
    'Rscript x.R',
    'for %A in (python) do %A payload',
    'wmic process call create python',
]

STUDY_SUBDIRS = (
    '.gxpllm', 'data/raw', 'data/derived', 'docs',
    'programs/sas', 'programs/python', 'programs/r',
    'output/tables', 'output/figures', 'output/listings',
    'logs/runs', 'audit', 'macros', 'spec', 'validation',
)


# ============================================================================
# 메인 로직
# ============================================================================

def make_study(tmpdir):
    """
    임시 study 디렉터리를 만든다

    Args:
        tmpdir: 임시 디렉터리 경로

    Returns:
        study 루트 Path
    """
    root = Path(tmpdir, 'STUDY')
    for sub in STUDY_SUBDIRS:
        Path(root, sub).mkdir(parents=True, exist_ok=True)

    config = {'study_id': 'STUDY', 'blinded': False}
    Path(root, '.gxpllm', 'config.json').write_text(
        json.dumps(config, ensure_ascii=False), encoding='utf-8'
    )
    return root


def probe(command, study_root):
    """
    hook 에 명령을 넣고 결과를 받는다

    Args:
        command: 검사할 셸 명령
        study_root: study 루트 Path

    Returns:
        (exit_code, 차단 사유 문자열)
    """
    payload = {
        'session_id': 'probe',
        'cwd': str(Path(study_root, 'programs')),
        'hook_event_name': 'PreToolUse',
        'tool_name': 'Bash',
        'tool_input': {'command': command},
    }

    result = subprocess.run(
        [sys.executable, str(GUARD_BASH)],
        input=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        capture_output=True,
        timeout=30,
    )

    stderr = (result.stderr or b'').decode('utf-8', errors='replace')
    reason = ''
    for line in stderr.splitlines():
        if '사유' in line:
            reason = line.strip()
            break

    return result.returncode, reason


def main():
    """메인 함수"""
    print("=" * 80)
    print("일상 명령 오탐 점검")
    print("=" * 80)

    with tempfile.TemporaryDirectory() as tmpdir:
        study_root = make_study(tmpdir)

        print(f"\n[1/2] 일상 명령 {len(EVERYDAY_COMMANDS):,}건 (모두 허용 기대)...")
        false_positives = []
        for command in EVERYDAY_COMMANDS:
            code, reason = probe(command, study_root)
            if code != EXIT_ALLOW:
                false_positives.append((command, reason))
                print(f"  오탐  {command}")
                print(f"        {reason[:120]}")

        if not false_positives:
            print(f"  오탐 없음")

        print(f"\n[2/2] 차단 대상 {len(MUST_BLOCK_COMMANDS):,}건 (모두 차단 기대)...")
        leaks = []
        for command in MUST_BLOCK_COMMANDS:
            code, _ = probe(command, study_root)
            if code != EXIT_BLOCK:
                leaks.append(command)
                print(f"  누수  {command}")

        if not leaks:
            print(f"  누수 없음")

    print(f"\n{'=' * 80}")
    print(f"오탐 {len(false_positives):,}건 / 누수 {len(leaks):,}건")
    print("=" * 80)

    sys.exit(1 if (false_positives or leaks) else 0)


if __name__ == "__main__":
    main()
