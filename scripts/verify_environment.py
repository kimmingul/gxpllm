"""
실행 환경 end-to-end 검증

개발 환경에서는 SAS 9.4 / R / DGX Spark vLLM 이 없어 검증할 수 없었던 부분을
**실제 PC 에서** 확인한다. 배포 전 반드시 실행하고 결과를 validation/ 에 보관한다.

검증 항목
  1. SAS 9.4      배치 실행, CP949 로그 인코딩, 로그 스캔 규칙, assertion 매크로
  2. R            Rscript --vanilla, renv, assertion 함수, sessionInfo
  3. Python       uv 환경 잠금, assertion 모듈
  4. vLLM         endpoint 연결, 모델 응답, 프롬프트 로깅 비활성 여부
  5. 통합         3개 언어가 동일한 assertion 형식을 내는가

각 항목은 **의도적으로 실패하는 케이스**를 포함한다.
성공만 확인하면 탐지 로직이 죽어 있어도 통과하기 때문이다.

사용:
    python scripts/verify_environment.py --study D:\\clinical\\DEMO-001
    python scripts/verify_environment.py --study . --skip-llm
    python scripts/verify_environment.py --study . --only sas
"""

import _common  # noqa: F401  (sys.path 설정)

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from gxpllm.core import load_config, now_iso, current_user, current_hostname

# ============================================================================
# 설정 영역 - 여기서 수정하세요
# ============================================================================

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
LLM_TIMEOUT_SEC = 120
RUNNER_TIMEOUT_SEC = 900

# --- SAS 검증 프로그램 ------------------------------------------------------
# 정상 경로와 로그 스캔 대상(다대다 MERGE, 타입 변환)을 함께 넣는다
SAS_PROGRAM = r"""/*----------------------------------------------------------------------------
  GXPLLM-META-BEGIN
  program      : verify_sas.sas
  purpose      : SAS 9.4 runner 환경 검증
  inputs       :
  outputs      : output/tables/verify_sas.txt
  analysis_set : (검증용)
  GXPLLM-META-END
----------------------------------------------------------------------------*/

%include "&GXPLLM_PLUGIN_ROOT./macros/gxpllm_assert.sas";

/* --- 1. 정상 데이터셋 생성 --------------------------------------------- */
data work.subjects;
    length USUBJID $20 SAFFL $1 SEX $1;
    do i = 1 to 50;
        USUBJID = cats('VERIFY-', put(i, z4.));
        SAFFL = ifc(mod(i, 10) = 0, 'N', 'Y');
        SEX   = ifc(mod(i, 2) = 0, 'F', 'M');
        AGE   = 40 + mod(i, 40);
        output;
    end;
    drop i;
run;

%gxpllm_assert_rowcount(work.subjects, label=VERIFY_ROWCOUNT, expected_n=50);
%gxpllm_assert_unique(work.subjects, keys=USUBJID, label=VERIFY_UNIQUE);
%gxpllm_assert_domain(work.subjects, column=SEX, allowed=%str(M F), label=VERIFY_DOMAIN);

/* --- 2. 분석군 필터 ----------------------------------------------------- */
data work.saf;
    set work.subjects;
    where SAFFL = 'Y';
run;

%gxpllm_assert_rowcount_delta(work.subjects, work.saf,
                             label=VERIFY_FILTER, max_loss_rate=0.2);
%gxpllm_assert_analysis_set(work.saf, flag_column=SAFFL, flag_value=Y, label=VERIFY_SAFETY_SET);
%gxpllm_assert_denominator(work.saf, subject_column=USUBJID, denominator=45,
                          label=VERIFY_DENOMINATOR);

/* --- 3. 의도적 실패: 잘못된 기대값 -------------------------------------- */
/*     이 assertion 은 반드시 FAIL 로 기록되어야 한다.                      */
/*     통과하면 assertion 매크로가 동작하지 않는 것이다.                    */
%gxpllm_assert_rowcount(work.saf, label=VERIFY_INTENTIONAL_FAIL, expected_n=99999);

/* --- 4. 의도적 로그 경고: 다대다 MERGE ---------------------------------- */
/*     MERGE_REPEAT_BY 규칙이 이것을 잡아야 한다.                          */
data work.dup_a;
    length USUBJID $20;
    do i = 1 to 3;
        USUBJID = 'VERIFY-0001'; VAL_A = i; output;
    end;
    drop i;
run;

data work.dup_b;
    length USUBJID $20;
    do i = 1 to 3;
        USUBJID = 'VERIFY-0001'; VAL_B = i; output;
    end;
    drop i;
run;

data work.merged;
    merge work.dup_a work.dup_b;
    by USUBJID;
run;

/* --- 5. 의도적 로그 경고: 문자→숫자 변환 -------------------------------- */
data work.convert;
    length TXT $10;
    TXT = '123';
    NUM = TXT + 0;
    output;
run;

/* --- 6. 출력 ------------------------------------------------------------ */
data _null_;
    file "&GXPLLM_STUDY_ROOT./output/tables/verify_sas.txt";
    put 'SAS verification output';
run;
"""

# --- R 검증 프로그램 --------------------------------------------------------
R_PROGRAM = r"""# GXPLLM-META-BEGIN
# program      : verify_r.R
# purpose      : R runner 환경 검증
# inputs       :
# outputs      : output/tables/verify_r.txt
# GXPLLM-META-END

source(file.path(Sys.getenv("GXPLLM_PLUGIN_ROOT"), "scripts", "gxpllm_assert.R"))

cat("[1/4] 데이터 생성...\n")
subjects <- data.frame(
  USUBJID = sprintf("VERIFY-%04d", 1:50),
  SAFFL   = ifelse(1:50 %% 10 == 0, "N", "Y"),
  SEX     = ifelse(1:50 %% 2 == 0, "F", "M"),
  AGE     = 40 + (1:50) %% 40,
  stringsAsFactors = FALSE
)

cat("[2/4] 정상 assertion...\n")
gxpllm_assert_rowcount(subjects, label = "VERIFY_ROWCOUNT", expected_n = 50)
gxpllm_assert_unique(subjects, keys = "USUBJID", label = "VERIFY_UNIQUE")
gxpllm_assert_domain(subjects, "SEX", c("M", "F"), label = "VERIFY_DOMAIN")

saf <- subjects[subjects$SAFFL == "Y", ]
gxpllm_assert_rowcount_delta(subjects, saf, label = "VERIFY_FILTER",
                            max_loss_rate = 0.2)
gxpllm_assert_analysis_set(saf, "SAFFL", "Y", label = "VERIFY_SAFETY_SET")
gxpllm_assert_denominator(saf, "USUBJID", nrow(saf), label = "VERIFY_DENOMINATOR")

cat("[3/4] 의도적 실패 assertion...\n")
gxpllm_assert_rowcount(saf, label = "VERIFY_INTENTIONAL_FAIL", expected_n = 99999)

cat("[4/4] 출력 생성...\n")
out_dir <- file.path(Sys.getenv("GXPLLM_STUDY_ROOT"), "output", "tables")
dir.create(out_dir, recursive = TRUE, showWarnings = FALSE)
writeLines("R verification output", file.path(out_dir, "verify_r.txt"))

gxpllm_assert_summary()
"""

# --- Python 검증 프로그램 ---------------------------------------------------
PYTHON_PROGRAM = '''"""
Python runner 환경 검증

GXPLLM-META-BEGIN
program      : verify_python.py
purpose      : Python runner 환경 검증
inputs       :
outputs      : output/tables/verify_python.txt
GXPLLM-META-END
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, os.environ['GXPLLM_PLUGIN_ROOT'] + '/scripts')
import gxpllm_assert as na


def main():
    """메인 함수"""
    root = Path(os.environ['GXPLLM_STUDY_ROOT'])

    print("[1/4] 데이터 생성...")
    records = [
        {'USUBJID': f'VERIFY-{i:04d}',
         'SAFFL': 'N' if i % 10 == 0 else 'Y',
         'SEX': 'F' if i % 2 == 0 else 'M',
         'AGE': 40 + (i % 40)}
        for i in range(1, 51)
    ]

    print("[2/4] 정상 assertion...")
    na.assert_rowcount(records, label='VERIFY_ROWCOUNT', expected_n=50)

    saf = [r for r in records if r['SAFFL'] == 'Y']
    na.assert_rowcount_delta(len(records), len(saf),
                             label='VERIFY_FILTER', max_loss_rate=0.2)
    na.assert_le(len(saf), len(records), label='VERIFY_LE',
                 expression='safety <= total')
    na.assert_sum_equals([len(saf), len(records) - len(saf)], len(records),
                         label='VERIFY_SUM')

    print("[3/4] 의도적 실패 assertion...")
    na.assert_rowcount(saf, label='VERIFY_INTENTIONAL_FAIL', expected_n=99999)

    print("[4/4] 출력 생성...")
    out = root / 'output' / 'tables' / 'verify_python.txt'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text('Python verification output\\n', encoding='utf-8')

    na.summary()


if __name__ == "__main__":
    main()
'''

LANGUAGE_SPECS = {
    'sas':    ('verify_sas.sas',    SAS_PROGRAM,    'run_sas.py',    'sas'),
    'r':      ('verify_r.R',        R_PROGRAM,      'run_r.py',      'r'),
    'python': ('verify_python.py',  PYTHON_PROGRAM, 'run_python.py', 'python'),
}

# 각 언어 실행 후 반드시 확인할 것
EXPECTED_ASSERTIONS = ('VERIFY_ROWCOUNT', 'VERIFY_INTENTIONAL_FAIL')

# SAS 로그에서 반드시 탐지되어야 할 규칙 (의도적으로 발생시킴)
EXPECTED_SAS_LOG_RULES = ('MERGE_REPEAT_BY',)


# ============================================================================
# 검증 유틸
# ============================================================================

class Result:
    """검증 결과 누적기"""

    def __init__(self):
        self.items = []

    def add(self, category, name, ok, detail='', skipped=False):
        """
        검증 결과를 추가한다

        Args:
            category: 분류 (SAS / R / Python / vLLM / 통합)
            name: 항목 이름
            ok: 통과 여부
            detail: 상세 설명
            skipped: 건너뛴 항목인지
        """
        self.items.append({
            'category': category, 'name': name,
            'ok': bool(ok), 'skipped': bool(skipped), 'detail': detail,
        })

        if skipped:
            status = 'SKIP'
        elif ok:
            status = 'OK  '
        else:
            status = 'FAIL'
        print(f"  {status} {name}" + (f"  — {detail}" if detail else ''))

    @property
    def failures(self):
        """실패 항목 리스트"""
        return [i for i in self.items if not i['ok'] and not i['skipped']]

    @property
    def skipped(self):
        """건너뛴 항목 리스트"""
        return [i for i in self.items if i['skipped']]


def run_runner(runner_name, program_path, study_root):
    """
    runner 를 실행한다

    Args:
        runner_name: run_sas.py 등
        program_path: 실행할 프로그램 경로
        study_root: study 루트 Path

    Returns:
        (exit_code, stdout, stderr)
    """
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['GXPLLM_PLUGIN_ROOT'] = str(PLUGIN_ROOT).replace('\\', '/')

    try:
        completed = subprocess.run(
            [sys.executable, str(PLUGIN_ROOT / 'scripts' / runner_name),
             '--program', str(program_path), '--purpose', 'exploratory'],
            capture_output=True, cwd=str(study_root), env=env,
            timeout=RUNNER_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return 124, '', f'타임아웃 {RUNNER_TIMEOUT_SEC}초 초과'

    return (
        completed.returncode,
        (completed.stdout or b'').decode('utf-8', errors='replace'),
        (completed.stderr or b'').decode('utf-8', errors='replace'),
    )


def find_run_dir(study_root, stdout):
    """
    runner 출력에서 run 디렉터리를 찾는다

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
    return None


def load_json_safe(path):
    """
    JSON 파일을 읽는다

    Args:
        path: 파일 경로

    Returns:
        파싱된 딕셔너리. 실패하면 빈 딕셔너리
    """
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


# ============================================================================
# 언어별 검증
# ============================================================================

def check_language(language, study_root, result):
    """
    한 언어의 runner 와 assertion emitter 를 검증한다

    Args:
        language: sas / r / python
        study_root: study 루트 Path
        result: Result 누적기

    Returns:
        assertion 결과 리스트 (통합 검증에 사용)
    """
    filename, source, runner_name, subdir = LANGUAGE_SPECS[language]
    label = language.upper()

    print(f"\n[{label}] runner 및 assertion 검증...")

    # --- 실행 가능 여부 확인 -----------------------------------------------
    if language == 'sas':
        config = load_config(study_root, required=False)
        sas_exe = config.get('sas_exe', '')
        if not sas_exe or not Path(sas_exe).is_file():
            result.add(label, 'SAS 9.4 설치 확인', False,
                       f'실행 파일 없음: {sas_exe}', skipped=True)
            print(f"       .gxpllm/config.json 의 sas_exe 를 실제 경로로 수정하십시오")
            return []
        result.add(label, 'SAS 9.4 설치 확인', True, sas_exe)

    elif language == 'r':
        rscript = shutil.which('Rscript') or shutil.which('Rscript.exe')
        if not rscript:
            result.add(label, 'Rscript 설치 확인', False,
                       'PATH 에서 찾을 수 없음', skipped=True)
            return []
        result.add(label, 'Rscript 설치 확인', True, rscript)

    # --- 프로그램 배치 및 실행 ----------------------------------------------
    program_dir = study_root / 'programs' / subdir
    program_dir.mkdir(parents=True, exist_ok=True)
    program_path = program_dir / filename
    program_path.write_text(source, encoding='utf-8')

    exit_code, stdout, stderr = run_runner(runner_name, program_path, study_root)

    run_dir = find_run_dir(study_root, stdout)
    if run_dir is None:
        result.add(label, 'run 디렉터리 생성', False,
                   f'exit={exit_code}, stderr={stderr[:200]}')
        return []
    result.add(label, 'run 디렉터리 생성', True, run_dir.name)

    # --- manifest ------------------------------------------------------------
    manifest = load_json_safe(run_dir / 'manifest.json')
    result.add(label, 'manifest.json 생성', bool(manifest))

    if manifest:
        result.add(label, 'manifest language 필드', manifest.get('language') == language,
                   str(manifest.get('language')))
        result.add(label, 'program sha256 기록',
                   bool((manifest.get('program') or {}).get('sha256')))

    # --- assertion -----------------------------------------------------------
    assertions_data = load_json_safe(run_dir / 'assertions.json')
    assertions = assertions_data.get('assertions', [])
    labels = {a.get('label') for a in assertions}

    result.add(label, 'assertion 기록됨', bool(assertions), f'{len(assertions):,}건')

    for expected in EXPECTED_ASSERTIONS:
        result.add(label, f'assertion {expected}', expected in labels)

    # 의도적 실패가 FAIL 로 기록되었는가 — 이것이 통과하면 emitter 가 죽은 것
    intentional = next(
        (a for a in assertions if a.get('label') == 'VERIFY_INTENTIONAL_FAIL'), None
    )
    if intentional:
        result.add(label, '의도적 실패가 FAIL 로 기록',
                   intentional.get('result') == 'FAIL',
                   f"result={intentional.get('result')}")
    else:
        result.add(label, '의도적 실패가 FAIL 로 기록', False, '항목 없음')

    # 실패가 있으므로 runner 는 FAILED 여야 한다
    result.add(label, '실패 assertion 이 run 을 FAILED 로',
               manifest.get('result') == 'FAILED',
               f"result={manifest.get('result')}")

    # --- 로그 ----------------------------------------------------------------
    logs = manifest.get('logs') or {}
    log_relative = logs.get('execution_log')
    if log_relative:
        log_path = study_root / log_relative
        result.add(label, '실행 로그 생성', log_path.is_file(), log_relative)
        result.add(label, '로그 해시 기록', bool(logs.get('log_sha256')))

    if language == 'sas':
        encoding = logs.get('log_encoding')
        result.add(label, 'SAS 로그 인코딩 감지', bool(encoding), f'encoding={encoding}')

        lst_relative = logs.get('execution_lst')
        if lst_relative:
            result.add(label, 'SAS .lst 생성',
                       (study_root / lst_relative).is_file(), lst_relative)

        # 로그 스캔이 의도적 경고를 잡았는가
        findings = (manifest.get('log_scan') or {}).get('findings', [])
        rules = {f.get('rule') for f in findings}
        for expected_rule in EXPECTED_SAS_LOG_RULES:
            result.add(label, f'로그 스캔 {expected_rule} 탐지',
                       expected_rule in rules,
                       f'탐지된 규칙: {", ".join(sorted(r for r in rules if r))[:100]}')

    return assertions


# ============================================================================
# vLLM 검증
# ============================================================================

def check_llm(study_root, result, endpoint_override=None, model_override=None):
    """
    DGX Spark 의 vLLM endpoint 를 검증한다

    Args:
        study_root: study 루트 Path
        result: Result 누적기
        endpoint_override: endpoint 재정의
        model_override: 모델명 재정의
    """
    print(f"\n[vLLM] DGX Spark endpoint 검증...")

    config = load_config(study_root, required=False)
    endpoint = endpoint_override or config.get('llm_endpoint')
    model = model_override or config.get('llm_model')

    if not endpoint:
        result.add('vLLM', 'endpoint 설정', False,
                   '.gxpllm/config.json 에 llm_endpoint 없음', skipped=True)
        return

    result.add('vLLM', 'endpoint 설정', True, endpoint)

    # --- 모델 목록 조회 ------------------------------------------------------
    models_url = f"{endpoint.rstrip('/')}/models"
    try:
        with urllib.request.urlopen(models_url, timeout=LLM_TIMEOUT_SEC) as response:
            body = json.loads(response.read().decode('utf-8'))
        served = [m.get('id') for m in body.get('data', [])]
        result.add('vLLM', 'endpoint 연결', True, f"모델 {len(served):,}개")
        result.add('vLLM', f'모델 {model} 서빙 중',
                   model in served if model else bool(served),
                   f"서빙 목록: {', '.join(str(s) for s in served)[:120]}")
    except (urllib.error.URLError, ValueError, OSError) as exc:
        result.add('vLLM', 'endpoint 연결', False, f'{type(exc).__name__}: {exc}')
        print(f"       DGX Spark 의 vLLM 서비스가 기동 중인지 확인하십시오")
        return

    # --- MCP 경유 코드 생성 --------------------------------------------------
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['GXPLLM_ENDPOINT'] = endpoint
    if model:
        env['GXPLLM_MODEL'] = model

    requests = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
         'params': {'name': 'write_program', 'arguments': {
             'language': 'sas',
             'program_name': 'verify_gen.sas',
             'purpose': '환경 검증용 최소 프로그램',
             'instructions': 'PROC PRINT 하나만 있는 최소 프로그램을 작성하십시오.',
         }}},
    ]
    payload = '\n'.join(json.dumps(r, ensure_ascii=False) for r in requests) + '\n'

    try:
        completed = subprocess.run(
            [sys.executable, str(PLUGIN_ROOT / 'mcp' / 'local_coder_server.py')],
            input=payload.encode('utf-8'), capture_output=True,
            timeout=LLM_TIMEOUT_SEC * 3, env=env,
        )
    except subprocess.TimeoutExpired:
        result.add('vLLM', 'MCP 경유 코드 생성', False, '타임아웃')
        return

    generated = ''
    is_error = True
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
        payload_result = response.get('result') or {}
        generated = (payload_result.get('content') or [{}])[0].get('text', '')
        is_error = bool(payload_result.get('isError'))

    result.add('vLLM', 'MCP 경유 코드 생성', not is_error and bool(generated),
               generated[:100].replace('\n', ' ') if generated else '응답 없음')

    if generated and not is_error:
        result.add('vLLM', 'GXPLLM-META 헤더 포함',
                   'GXPLLM-META-BEGIN' in generated,
                   '로컬 LLM 이 헤더 규약을 지키는지')

    # --- 프롬프트 로깅 비활성 안내 ------------------------------------------
    print(f"\n       [수동 확인 필요] DGX Spark 에서 다음을 확인하십시오")
    print(f"         vllm serve ... --disable-log-requests")
    print(f"       이 옵션이 없으면 코드 작성 프롬프트에 담긴 데이터 구조가")
    print(f"       서버 로그에 평문으로 쌓입니다.")


# ============================================================================
# 통합 검증
# ============================================================================

def check_cross_language(collected, result):
    """
    세 언어의 assertion 형식이 동일한지 검증한다

    Args:
        collected: {언어: assertion 리스트}
        result: Result 누적기
    """
    print(f"\n[통합] 언어별 assertion 형식 일치...")

    available = {lang: items for lang, items in collected.items() if items}
    if len(available) < 2:
        result.add('통합', '언어 간 형식 비교', False,
                   f'비교 가능한 언어가 {len(available)}개뿐', skipped=True)
        return

    required_keys = {'label', 'rule', 'result', 'language', 'message'}

    for language, items in available.items():
        missing = set()
        for item in items:
            missing |= (required_keys - set(item.keys()))
        result.add('통합', f'{language} assertion 필수 키',
                   not missing,
                   f"누락: {', '.join(sorted(missing))}" if missing else '')

        wrong_lang = [i for i in items if i.get('language') != language]
        result.add('통합', f'{language} language 필드 일치', not wrong_lang,
                   f'{len(wrong_lang):,}건 불일치' if wrong_lang else '')

    # 같은 rule 이름을 쓰는가
    rules_by_language = {
        lang: {i.get('rule') for i in items} for lang, items in available.items()
    }
    common_rules = set.intersection(*rules_by_language.values())
    result.add('통합', '공통 rule 이름 존재', bool(common_rules),
               f"공통: {', '.join(sorted(r for r in common_rules if r))[:100]}")


# ============================================================================
# 메인 로직
# ============================================================================

def main():
    """메인 함수"""
    print("=" * 80)
    print("실행 환경 end-to-end 검증")
    print("=" * 80)

    parser = argparse.ArgumentParser(
        description='SAS / R / Python / vLLM 환경을 실제로 검증합니다'
    )
    parser.add_argument('--study', default='.', help='study 루트 경로')
    parser.add_argument('--only', help='특정 항목만 (sas, r, python, llm)')
    parser.add_argument('--skip-llm', action='store_true', help='vLLM 검증 생략')
    parser.add_argument('--endpoint', help='vLLM endpoint 재정의')
    parser.add_argument('--model', help='vLLM 모델명 재정의')
    parser.add_argument('--output', help='결과 JSON 저장 경로')
    args = parser.parse_args()

    config = load_config(args.study, required=False)
    if not config:
        print(f"\n오류: .gxpllm/config.json 을 찾을 수 없습니다 ({args.study})")
        print(f"      python scripts/init_study.py 로 study 를 먼저 만드십시오.")
        sys.exit(2)

    study_root = Path(config['root'])
    result = Result()

    print(f"\n  study    : {config.get('study_id')}")
    print(f"  경로     : {study_root}")
    print(f"  사용자   : {current_user()}@{current_hostname()}")

    targets = [args.only] if args.only else ['sas', 'r', 'python']
    collected = {}

    for language in ('sas', 'r', 'python'):
        if language in targets:
            collected[language] = check_language(language, study_root, result)

    if not args.skip_llm and (not args.only or args.only == 'llm'):
        check_llm(study_root, result, args.endpoint, args.model)

    if not args.only:
        check_cross_language(collected, result)

    # --- 요약 ----------------------------------------------------------------
    total = len(result.items)
    failed = len(result.failures)
    skipped = len(result.skipped)
    passed = total - failed - skipped

    print(f"\n{'=' * 80}")
    print(f"결과: {passed:,}건 통과 / {failed:,}건 실패 / {skipped:,}건 건너뜀")
    print("=" * 80)

    if result.failures:
        print(f"\n실패 항목:")
        for item in result.failures:
            print(f"  [{item['category']}] {item['name']}")
            if item['detail']:
                print(f"      {item['detail']}")

    if result.skipped:
        print(f"\n건너뛴 항목 (해당 소프트웨어 미설치):")
        for item in result.skipped:
            print(f"  [{item['category']}] {item['name']} — {item['detail']}")

    payload = {
        'verified_at': now_iso(),
        'study_id': config.get('study_id'),
        'user': current_user(),
        'hostname': current_hostname(),
        'passed': passed,
        'failed': failed,
        'skipped': skipped,
        'items': result.items,
    }

    output_path = Path(args.output) if args.output else (
        study_root / 'validation' /
        f"environment_verification_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"\n  결과 저장: {output_path}")
    print(f"  이 파일을 validation/ 에 보관하고 CSV 문서(IQ/OQ)에 첨부하십시오.")

    if failed:
        print(f"\n  실패 항목이 있습니다. 해결 전까지 실제 임상 데이터에 사용하지 마십시오.")

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
