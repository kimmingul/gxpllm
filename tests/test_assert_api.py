"""
3개 언어 assertion API 일치 검증

SAS / Python / R 의 assertion 함수가 **같은 이름과 같은 파라미터**를 갖는지 확인한다.

왜 필요한가
  로컬 LLM 은 같은 SAP 명세로 세 언어의 코드를 만든다.
  파라미터 이름이 언어마다 다르면 (예: Python 만 expected_exact, SAS/R 은 expected_n)
  LLM 이 한 언어에서 익힌 패턴을 다른 언어에 그대로 쓰다가 깨진다.
  실제로 이 불일치가 verify_environment.py 에서 발견됐다.

  또한 skills/*.md 와 MCP 시스템 프롬프트가 광고하는 시그니처와
  실제 구현이 어긋나면, LLM 이 문서대로 코드를 짜고 실패한다.

실행:
    python tests/test_assert_api.py
"""

import inspect
import re
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))
if str(PLUGIN_ROOT / 'scripts') not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT / 'scripts'))

# ============================================================================
# 설정 영역 - 여기서 수정하세요
# ============================================================================

SAS_MACRO = PLUGIN_ROOT / 'macros' / 'gxpllm_assert.sas'
R_MODULE = PLUGIN_ROOT / 'scripts' / 'gxpllm_assert.R'
MCP_SERVER = PLUGIN_ROOT / 'mcp' / 'local_coder_server.py'

SKILL_FILES = (
    PLUGIN_ROOT / 'skills' / 'sas-programming' / 'SKILL.md',
    PLUGIN_ROOT / 'skills' / 'python-r-programming' / 'SKILL.md',
)

# 세 언어가 모두 제공해야 하는 assertion
# (Python 함수명, SAS 매크로명, R 함수명)
REQUIRED_ASSERTIONS = (
    ('assert_rowcount',        'gxpllm_assert_rowcount',        'gxpllm_assert_rowcount'),
    ('assert_rowcount_delta',  'gxpllm_assert_rowcount_delta',  'gxpllm_assert_rowcount_delta'),
    ('assert_unique',          'gxpllm_assert_unique',          'gxpllm_assert_unique'),
    ('assert_domain',          'gxpllm_assert_domain',          'gxpllm_assert_domain'),
    ('assert_missingness',     'gxpllm_assert_missingness',     'gxpllm_assert_missingness'),
    ('assert_date_order',      'gxpllm_assert_date_order',      'gxpllm_assert_date_order'),
    ('assert_analysis_set',    'gxpllm_assert_analysis_set',    'gxpllm_assert_analysis_set'),
    ('assert_denominator',     'gxpllm_assert_denominator',     'gxpllm_assert_denominator'),
    ('assert_le',              'gxpllm_assert_le',              'gxpllm_assert_le'),
)

# 세 언어에서 이름이 같아야 하는 파라미터
# {Python 파라미터: (SAS 파라미터, R 파라미터)}
SHARED_PARAMETERS = {
    'label':          ('label', 'label'),
    'expected_min':   ('expected_min', 'expected_min'),
    'expected_max':   ('expected_max', 'expected_max'),
    'expected_n':     ('expected_n', 'expected_n'),
    'max_loss_rate':  ('max_loss_rate', 'max_loss_rate'),
    'strict':         ('strict', 'strict'),
}


# ============================================================================
# 파서
# ============================================================================

def parse_sas_macros(path):
    """
    SAS 매크로 정의를 파싱한다

    Args:
        path: .sas 파일 경로

    Returns:
        {매크로명: 파라미터 이름 집합}
    """
    text = path.read_text(encoding='utf-8', errors='replace')
    macros = {}

    for match in re.finditer(r'%macro\s+(\w+)\s*\(([^)]*)\)', text, re.IGNORECASE):
        name = match.group(1).lower()
        params = set()
        for chunk in match.group(2).split(','):
            chunk = chunk.strip()
            if not chunk:
                continue
            params.add(chunk.split('=')[0].strip().lower())
        macros[name] = params

    return macros


def parse_r_functions(path):
    """
    R 함수 정의를 파싱한다

    Args:
        path: .R 파일 경로

    Returns:
        {함수명: 파라미터 이름 집합}
    """
    text = path.read_text(encoding='utf-8', errors='replace')
    functions = {}

    for match in re.finditer(r'^(\w[\w.]*)\s*<-\s*function\s*\(', text, re.MULTILINE):
        name = match.group(1)
        start = match.end()

        # 괄호 균형을 맞춰 시그니처 끝을 찾는다
        depth = 1
        index = start
        while index < len(text) and depth > 0:
            if text[index] == '(':
                depth += 1
            elif text[index] == ')':
                depth -= 1
            index += 1

        signature = text[start:index - 1]
        params = set()
        depth = 0
        current = ''
        for char in signature:
            if char in '([{':
                depth += 1
            elif char in ')]}':
                depth -= 1
            if char == ',' and depth == 0:
                params.add(current.split('=')[0].strip())
                current = ''
            else:
                current += char
        if current.strip():
            params.add(current.split('=')[0].strip())

        functions[name] = {p for p in params if p and p != '...'}

    return functions


def parse_python_functions():
    """
    Python assertion 함수를 파싱한다

    Returns:
        {함수명: 파라미터 이름 집합}
    """
    import gxpllm_assert

    functions = {}
    for name, obj in vars(gxpllm_assert).items():
        if name.startswith('assert_') and callable(obj):
            functions[name] = set(inspect.signature(obj).parameters)
    return functions


def collect_documented_signatures():
    """
    skill 문서와 MCP 프롬프트가 광고하는 함수 호출을 수집한다

    Returns:
        (파일 경로, 함수명, 파라미터 집합) 튜플 리스트
    """
    entries = []
    pattern = re.compile(r'(%?gxpllm_assert_\w+|na\.assert_\w+)\s*\(([^)]*)\)')

    for path in list(SKILL_FILES) + [MCP_SERVER]:
        if not path.is_file():
            continue
        text = path.read_text(encoding='utf-8', errors='replace')
        for match in pattern.finditer(text):
            name = match.group(1).lstrip('%').replace('na.', '')
            params = set()
            for chunk in match.group(2).split(','):
                chunk = chunk.strip()
                if '=' in chunk:
                    params.add(chunk.split('=')[0].strip())
            if params:
                entries.append((path.name, name, params))

    return entries


# ============================================================================
# 메인 로직
# ============================================================================

def check_coverage(python_fns, sas_macros, r_fns):
    """
    세 언어가 필수 assertion 을 모두 제공하는지 확인한다

    Args:
        python_fns: Python 함수 딕셔너리
        sas_macros: SAS 매크로 딕셔너리
        r_fns: R 함수 딕셔너리

    Returns:
        문제 리스트
    """
    print("\n[1/3] 필수 assertion 제공 여부...")
    problems = []

    for py_name, sas_name, r_name in REQUIRED_ASSERTIONS:
        missing = []
        if py_name not in python_fns:
            missing.append(f'Python {py_name}')
        if sas_name.lower() not in sas_macros:
            missing.append(f'SAS %{sas_name}')
        if r_name not in r_fns:
            missing.append(f'R {r_name}')

        if missing:
            problems.append(f"{py_name}: {', '.join(missing)} 없음")
            print(f"  FAIL {py_name} — {', '.join(missing)} 없음")
        else:
            print(f"  OK   {py_name}")

    return problems


def check_parameter_names(python_fns, sas_macros, r_fns):
    """
    공통 파라미터 이름이 세 언어에서 같은지 확인한다

    Args:
        python_fns: Python 함수 딕셔너리
        sas_macros: SAS 매크로 딕셔너리
        r_fns: R 함수 딕셔너리

    Returns:
        문제 리스트
    """
    print("\n[2/3] 공통 파라미터 이름 일치...")
    problems = []

    for py_name, sas_name, r_name in REQUIRED_ASSERTIONS:
        py_params = python_fns.get(py_name, set())
        sas_params = sas_macros.get(sas_name.lower(), set())
        r_params = r_fns.get(r_name, set())

        for shared, (sas_key, r_key) in SHARED_PARAMETERS.items():
            in_py = shared in py_params
            in_sas = sas_key.lower() in sas_params
            in_r = r_key in r_params

            # 어느 한 곳에라도 있으면 나머지도 같은 이름을 써야 한다
            present = [in_py, in_sas, in_r]
            if any(present) and not all(present):
                # date_order 처럼 일부 언어에만 있는 개념은 제외
                if shared in ('expected_min', 'expected_max', 'expected_n',
                              'max_loss_rate') and py_name not in (
                        'assert_rowcount', 'assert_rowcount_delta',
                        'assert_analysis_set'):
                    continue

                where = []
                if in_py:
                    where.append('Python')
                if in_sas:
                    where.append('SAS')
                if in_r:
                    where.append('R')
                missing = [n for n, p in zip(('Python', 'SAS', 'R'), present) if not p]

                problems.append(
                    f"{py_name}.{shared}: {', '.join(where)} 에만 있음 "
                    f"({', '.join(missing)} 누락)"
                )
                print(f"  FAIL {py_name}.{shared} — {', '.join(where)} 에만 있음")

    if not problems:
        print(f"  OK   공통 파라미터 이름이 세 언어에서 일치")

    return problems


def check_documentation(python_fns, sas_macros, r_fns):
    """
    문서가 광고하는 시그니처가 실제 구현과 맞는지 확인한다

    Args:
        python_fns: Python 함수 딕셔너리
        sas_macros: SAS 매크로 딕셔너리
        r_fns: R 함수 딕셔너리

    Returns:
        문제 리스트
    """
    print("\n[3/3] 문서·MCP 프롬프트 시그니처 정합성...")
    problems = []
    checked = 0

    for source, name, documented in collect_documented_signatures():
        # SAS 매크로와 R 함수는 이름이 같다 (gxpllm_assert_domain).
        # 따라서 후보를 모두 모아 그중 하나라도 만족하면 통과로 본다.
        candidates = []
        if name in python_fns:
            candidates.append(('Python', python_fns[name], documented))
        if name.lower() in sas_macros:
            candidates.append((
                'SAS',
                {p.lower() for p in sas_macros[name.lower()]},
                {p.lower() for p in documented},
            ))
        if name in r_fns:
            candidates.append(('R', r_fns[name], documented))

        if not candidates:
            continue

        checked += 1
        if any(not (doc - actual) for _, actual, doc in candidates):
            continue

        languages = ', '.join(lang for lang, _, _ in candidates)
        unknown = sorted(set.intersection(*[doc - actual for _, actual, doc in candidates]))
        problems.append(
            f"{source}: {name}({', '.join(unknown)}) "
            f"— {languages} 어느 구현에도 없는 파라미터"
        )
        print(f"  FAIL {source}: {name} — 없는 파라미터 {', '.join(unknown)}")

    if not problems:
        print(f"  OK   문서 호출 {checked:,}건 모두 구현과 일치")

    return problems


def main():
    """메인 함수"""
    print("=" * 80)
    print("3개 언어 assertion API 일치 검증")
    print("=" * 80)

    python_fns = parse_python_functions()
    sas_macros = parse_sas_macros(SAS_MACRO)
    r_fns = parse_r_functions(R_MODULE)

    print(f"\n  Python 함수 : {len(python_fns):,}개")
    print(f"  SAS 매크로  : {len(sas_macros):,}개")
    print(f"  R 함수      : {len(r_fns):,}개")

    problems = []
    problems += check_coverage(python_fns, sas_macros, r_fns)
    problems += check_parameter_names(python_fns, sas_macros, r_fns)
    problems += check_documentation(python_fns, sas_macros, r_fns)

    print(f"\n{'=' * 80}")
    if problems:
        print(f"실패: {len(problems):,}건")
        for item in problems:
            print(f"  - {item}")
    else:
        print("모든 검증 통과 — 세 언어의 assertion API 가 일치합니다")
    print("=" * 80)

    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
