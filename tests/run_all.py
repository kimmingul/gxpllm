"""
전체 테스트 실행

plugin 배포 전 반드시 통과해야 하는 검증을 모두 실행한다.

포함
- test_hooks.py    : 경계 차단 (우회 시도 포함)
- test_audit.py    : 감사 체인 변조 탐지
- test_runners.py  : runner 및 assertion emitter
- test_mcp.py      : MCP 서버 프로토콜
- 구조 검증        : plugin 파일 구조 완결성

실행:
    python tests/run_all.py
"""

import json
import subprocess
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = PLUGIN_ROOT / 'tests'

# ============================================================================
# 설정 영역 - 여기서 수정하세요
# ============================================================================

TEST_SCRIPTS = (
    ('경계 차단',        'test_hooks.py'),
    ('오탐 점검',        'test_false_positives.py'),
    ('감사 체인',        'test_audit.py'),
    ('assertion API',    'test_assert_api.py'),
    ('runner / assert',  'test_runners.py'),
    ('MCP 프로토콜',     'test_mcp.py'),
    ('LLM 경로 (모의)',  'test_llm_path.py'),
    ('실측 도구',        'test_benchmark.py'),
)

# plugin 이 갖춰야 할 파일
REQUIRED_FILES = (
    '.claude-plugin/plugin.json',
    '.mcp.json',
    'hooks/hooks.json',
    'hooks/_bootstrap.py',
    'hooks/guard_file_access.py',
    'hooks/guard_bash.py',
    'hooks/audit_append.py',
    'hooks/snapshot_env.py',
    'gxpllm/__init__.py',
    'gxpllm/core.py',
    'scripts/_common.py',
    'scripts/run_sas.py',
    'scripts/run_python.py',
    'scripts/run_r.py',
    'scripts/gxpllm_assert.py',
    'scripts/gxpllm_assert.R',
    'scripts/verify_audit.py',
    'scripts/compare_outputs.py',
    'scripts/init_study.py',
    'scripts/benchmark_codegen.py',
    'scripts/verify_environment.py',
    'skills/sas-programming/SKILL.md',
    'skills/python-r-programming/SKILL.md',
    'README.md',
    'LICENSE',
    'CHANGELOG.md',
    'CONTRIBUTING.md',
    'AGENTS.md',
    'CLAUDE.md',
    'package.json',
    'plugin.json',
    '.gitignore',
    '.gitattributes',
    '.claude-plugin/marketplace.json',
    '.github/workflows/ci.yml',
    'docs/getting-started.md',
    'docs/architecture.md',
    'examples/minimal-study.md',
    'macros/gxpllm_assert.sas',
    'mcp/local_coder_server.py',
    'skills/clinical-conventions/SKILL.md',
    'tests/test_hooks.py',
    'tests/test_false_positives.py',
    'tests/test_audit.py',
    'tests/test_assert_api.py',
    'tests/test_runners.py',
    'tests/test_mcp.py',
    'tests/test_llm_path.py',
    'tests/test_live_llm.py',
    'tests/mock_vllm_server.py',
    'tests/test_benchmark.py',
    'docs/development.md',
)

# plugin.json / marketplace.json 이 서로 맞아야 한다
PLUGIN_NAME = 'gxpllm'

# 매니페스트 4개가 같은 author 를 가리켜야 한다.
#
# package.json 은 문자열, 나머지는 {"name": ...} 객체다.
# marketplace.json 은 최상위 owner 와 plugins[].author 를 둘 다 갖는다.
# 한 곳만 고치면 배포처마다 다른 저자가 표시된다.
PLUGIN_AUTHOR = 'Min-Gul Kim'

REQUIRED_COMMANDS = (
    'build-dictionary',
    'draft-protocol',
    'draft-sap',
    'draft-dmp',
    'derive-assertions',
    'write-program',
    'run-program',
    'qc-program',
    'review-output',
    'verify-audit',
)

# hooks.json 이 걸어야 할 이벤트
REQUIRED_HOOK_EVENTS = ('PreToolUse', 'PostToolUse', 'SessionStart')


# ============================================================================
# 메인 로직
# ============================================================================

def author_name(value):
    """
    author / owner 값에서 이름을 꺼낸다

    package.json 은 문자열, plugin 계열은 {"name": ...} 객체를 쓴다.

    Args:
        value: author 또는 owner 필드 값

    Returns:
        이름 문자열. 해석할 수 없으면 None
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get('name')
    return None


def check_author_consistency():
    """
    매니페스트 4개의 author 가 일치하는지 검증한다

    한 곳만 고치면 marketplace 와 npm 에 다른 저자가 표시된다.

    Returns:
        문제 리스트
    """
    problems = []

    for relative, key in (
        ('.claude-plugin/plugin.json', 'author'),
        ('plugin.json', 'author'),
        ('package.json', 'author'),
        ('.claude-plugin/marketplace.json', 'owner'),
    ):
        path = PLUGIN_ROOT / relative
        if not path.is_file():
            continue
        try:
            with open(path, encoding='utf-8') as f:
                manifest = json.load(f)
        except ValueError:
            continue  # 파싱 실패는 check_structure 가 이미 보고한다

        found = author_name(manifest.get(key))
        if found != PLUGIN_AUTHOR:
            problems.append(
                f"{relative} 의 {key} 가 '{found}' (기대 '{PLUGIN_AUTHOR}')"
            )

        # marketplace.json 은 plugins[].author 도 갖는다
        for entry in manifest.get('plugins') or []:
            found = author_name(entry.get('author'))
            if found != PLUGIN_AUTHOR:
                problems.append(
                    f"{relative} 의 plugins[{entry.get('name')}].author 가 "
                    f"'{found}' (기대 '{PLUGIN_AUTHOR}')"
                )

    return problems


def check_structure():
    """
    plugin 파일 구조를 검증한다

    Returns:
        문제 리스트
    """
    print("\n[1/5] plugin 구조 검증...")
    problems = []

    for relative in REQUIRED_FILES:
        path = PLUGIN_ROOT / relative
        if not path.is_file():
            problems.append(f"필수 파일 없음: {relative}")

    for name in REQUIRED_COMMANDS:
        path = PLUGIN_ROOT / 'commands' / f'{name}.md'
        if not path.is_file():
            problems.append(f"필수 command 없음: commands/{name}.md")

    # plugin.json / marketplace.json / package.json 이름·버전 정합성
    versions = {}
    for relative, name_key in (
        ('.claude-plugin/plugin.json', 'name'),
        ('.claude-plugin/marketplace.json', 'name'),
        ('plugin.json', 'name'),
        ('package.json', 'name'),
    ):
        path = PLUGIN_ROOT / relative
        if not path.is_file():
            continue
        try:
            with open(path, encoding='utf-8') as f:
                manifest = json.load(f)
        except ValueError as exc:
            problems.append(f"{relative} 파싱 실패: {exc}")
            continue

        for key in ('name', 'version', 'description'):
            if key not in manifest:
                problems.append(f"{relative} 에 '{key}' 없음")

        if manifest.get(name_key) != PLUGIN_NAME:
            problems.append(
                f"{relative} 의 name 이 '{manifest.get(name_key)}' "
                f"(기대 '{PLUGIN_NAME}')"
            )
        if 'version' in manifest:
            versions[relative] = manifest['version']

    if len(set(versions.values())) > 1:
        problems.append(f"버전 불일치: {versions}")

    problems.extend(check_author_consistency())

    # hooks.json 형식
    hooks_path = PLUGIN_ROOT / 'hooks' / 'hooks.json'
    if hooks_path.is_file():
        try:
            with open(hooks_path, encoding='utf-8') as f:
                hooks = json.load(f).get('hooks', {})
            for event in REQUIRED_HOOK_EVENTS:
                if event not in hooks:
                    problems.append(f"hooks.json 에 '{event}' 없음")

            # PreToolUse 가 전체 도구를 덮는지 확인
            pre = hooks.get('PreToolUse', [])
            matchers = [entry.get('matcher') for entry in pre]
            if '*' not in matchers:
                problems.append(
                    "hooks.json PreToolUse 에 matcher '*' 가 없습니다. "
                    "새 도구가 추가되면 경계가 열립니다"
                )
        except ValueError as exc:
            problems.append(f"hooks.json 파싱 실패: {exc}")

    # command frontmatter
    for name in REQUIRED_COMMANDS:
        path = PLUGIN_ROOT / 'commands' / f'{name}.md'
        if not path.is_file():
            continue
        text = path.read_text(encoding='utf-8')
        if not text.startswith('---'):
            problems.append(f"commands/{name}.md 에 frontmatter 없음")
        elif 'description:' not in text.split('---')[1]:
            problems.append(f"commands/{name}.md frontmatter 에 description 없음")

    # skill frontmatter
    skill_path = PLUGIN_ROOT / 'skills' / 'clinical-conventions' / 'SKILL.md'
    if skill_path.is_file():
        text = skill_path.read_text(encoding='utf-8')
        if 'name:' not in text or 'description:' not in text:
            problems.append("SKILL.md frontmatter 에 name 또는 description 없음")

    for item in problems:
        print(f"  FAIL {item}")
    if not problems:
        print(f"  OK   필수 파일 {len(REQUIRED_FILES):,}개, "
              f"command {len(REQUIRED_COMMANDS):,}개 모두 존재")

    return problems


def run_test_script(label, script_name, index, total):
    """
    테스트 스크립트를 실행한다

    Args:
        label: 표시할 이름
        script_name: 스크립트 파일명
        index: 현재 순번
        total: 전체 수

    Returns:
        (성공 여부, 요약 문자열)
    """
    print(f"\n[{index}/{total}] {label} ({script_name})...")

    import os
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'

    result = subprocess.run(
        [sys.executable, str(TESTS_DIR / script_name)],
        capture_output=True,
        timeout=900,
        env=env,
    )

    stdout = (result.stdout or b'').decode('utf-8', errors='replace')

    # 결과 요약 줄만 추출
    summary = ''
    for line in stdout.splitlines():
        if '결과:' in line or '모든 검증 통과' in line or '실패:' in line:
            summary = line.strip()

    if result.returncode == 0:
        print(f"  OK   {summary}")
        return True, summary

    print(f"  FAIL {summary or f'exit {result.returncode}'}")
    # 실패 상세 출력
    for line in stdout.splitlines():
        if line.strip().startswith('- ') or 'FAIL' in line:
            print(f"       {line.strip()}")

    return False, summary


def main():
    """메인 함수"""
    print("=" * 80)
    print("gxpllm 전체 검증")
    print("=" * 80)

    all_ok = True
    results = []

    structure_problems = check_structure()
    if structure_problems:
        all_ok = False
    results.append(('plugin 구조', not structure_problems,
                    f"{len(structure_problems):,}건 문제" if structure_problems else '정상'))

    total = len(TEST_SCRIPTS) + 1
    for index, (label, script) in enumerate(TEST_SCRIPTS, start=2):
        ok, summary = run_test_script(label, script, index, total)
        results.append((label, ok, summary))
        if not ok:
            all_ok = False

    print(f"\n{'=' * 80}")
    print("검증 요약")
    print("=" * 80)
    for label, ok, summary in results:
        status = 'PASS' if ok else 'FAIL'
        print(f"  [{status}] {label:<20} {summary}")

    print(f"\n{'=' * 80}")
    if all_ok:
        print("전체 통과 — 배포 가능")
        print("=" * 80)
        print("""
  배포 전 확인할 것

  1. 실제 PC 에서 SAS 9.4 runner 를 검증하십시오
       이 테스트 환경에는 SAS 가 없어 건너뛰었습니다

  2. R 이 설치된 환경에서 R runner 를 검증하십시오

  3. DGX Spark 의 vLLM 이 기동 중인지 확인하십시오
       .mcp.json 의 GXPLLM_ENDPOINT

  4. 로컬 LLM 의 SAS 코드 생성 품질을 실측하십시오 (docs/development.md 11.1)
       이 결과가 나쁘면 설계를 조정해야 합니다
""")
    else:
        print("검증 실패 — 배포하지 마십시오")
        print("=" * 80)

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
