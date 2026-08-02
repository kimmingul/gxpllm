"""
셸 명령 검사 hook (PreToolUse, Bash)

세 가지를 차단한다.
1. 임상 데이터 경로를 참조하는 셸 명령 (파일 접근 hook 우회 방지)
2. SAS / Python / R 직접 실행 (runner 우회 방지 -> 로그/manifest 누락 방지)
3. 난독화된 명령 (base64 인코딩 등, 판정 불가하므로 차단)

정책
- 차단 시 exit code 2, 사유는 stderr 로 출력
- 예외가 발생해도 차단한다 (fail-closed)
- runner 경유 호출은 항상 허용

한계
  정규식 기반 판정은 완전하지 않다. 이 hook 의 목적은 악의적 우회 방지가 아니라
  실수 방지와 감사 증적 확보다. 이 한계는 CSV 문서에 기재한다.
"""

from _bootstrap import read_hook_payload  # sys.path 설정 + 견고한 입력 파서

import re
import shlex
import sys
import os
from pathlib import Path

# ============================================================================
# 설정 영역 - 여기서 수정하세요
# ============================================================================

EXIT_ALLOW = 0
EXIT_BLOCK = 2

# runner 경유 호출 (항상 허용)
#
# **반드시 구간 선두에서 match() 로 판정한다. search() 를 쓰면 안 된다.**
# 예전에는 앵커 없는 search() 였고, 그래서 명령 어디에든 runner 경로
# 문자열이 있기만 하면 구간 전체가 면제됐다. 실제로 아래가 통과했다.
#     python -c "print(1)" # scripts/run_sas.py
#     python -c "print(1)" --note scripts/run_python.py
# 주석이나 인자에 경로를 끼워 넣는 것만으로 인터프리터 차단이 풀렸다.
# test_false_positives.py 의 MUST_BLOCK_COMMANDS 에 두 케이스가 있다.
RUNNER_ALLOW_PATTERN = re.compile(
    r'^\s*(?:py|python[0-9.]*)(?:\.exe)?\s+'
    r'(?:[^\s;&|<>"\']*[\\/])?scripts[\\/]+'
    r'(?:run_sas|run_python|run_r|verify_audit|compare_outputs'
    r'|init_study|benchmark_codegen)\.py\b',
    re.IGNORECASE,
)

# plugin 자체를 검사·시험하는 개발용 명령
#
# CLAUDE.md 와 CONTRIBUTING.md 가 지정한 필수 명령인데 인터프리터 차단에
# 걸렸다. 개발자가 자기 저장소에서 문서에 적힌 명령을 못 돌리면 plugin 을
# 끄게 되고, 그것이 가장 확실한 경계 붕괴다.
#
# **RUNNER_ALLOW_PATTERN 에 넣지 않는다.** 그쪽은 명령 문자열 어디서든
# 부분 일치하면 구간 전체를 모든 검사에서 면제한다
# ('python -c "..." # scripts/run_sas.py' 로 우회 가능).
# 이쪽은 다음 셋으로 좁힌다.
#   1. 구간 **선두에서 끝까지** 정확히 일치해야 한다 (^...$)
#   2. 인자는 장옵션(--flag[=value]) 만 허용한다 — -c / -m 은 못 들어온다
#   3. check_direct_exec 안에서만 면제한다 — 난독화·변수확장·git·재귀탐색·
#      데이터경로 검사는 그대로 적용된다
DEV_COMMAND_PATTERN = re.compile(
    r'^\s*(?:py|python[0-9.]*)(?:\.exe)?\s+'
    r'(?:tests[\\/]+(?:run_all|test_[a-z0-9_]+)\.py'
    r'|scripts[\\/]+verify_environment\.py)'
    r'(?:\s+--[a-z][a-z0-9-]*(?:[=\s]+(?:"[^"]*"|\'[^\']*\'|[^\s"\';&|<>]+))?)*'
    r'\s*$',
    re.IGNORECASE,
)

# 셸 래퍼 — 명령 문자열 검사를 우회하는 통로이므로 전면 차단한다
#   powershell -File script.ps1  : 명령줄에 데이터 경로가 없고 본문은 검사 대상이 아님
#   cmd.exe /c "python -c ..."   : 따옴표 안이 한 토큰이라 인터프리터 판정을 빠져나감
SHELL_WRAPPER_BASENAMES = {
    'cmd', 'cmd.exe',
    'powershell', 'powershell.exe',
    'pwsh', 'pwsh.exe',
    'sh', 'bash', 'zsh', 'dash', 'ksh', 'ash', 'hush', 'csh', 'tcsh', 'fish',
    'mksh', 'pdksh', 'yash', 'xonsh', 'elvish', 'nu', 'busybox',
    'wsl', 'wsl.exe',
    'start', 'start.exe',
    'wscript', 'wscript.exe', 'cscript', 'cscript.exe',
}

# 데이터 경로를 시사하는 패턴 (토큰 단위 판정으로 못 잡는 경우 보완)
DATA_PATH_PATTERNS = [
    (re.compile(r'[\\/]data[\\/]', re.IGNORECASE),          'reference to data/ directory'),
    (re.compile(r'(^|[\s"\'])data[\\/]', re.IGNORECASE),    'reference to data/ directory'),
    (re.compile(r'[\\/]listings?[\\/]', re.IGNORECASE),     'reference to listings/ directory'),
    (re.compile(r'[\\/]logs[\\/]', re.IGNORECASE),          'reference to logs/ directory'),
    (re.compile(r'\.sas7bdat\b', re.IGNORECASE),            'reference to SAS dataset'),
    (re.compile(r'\.sas7bndx\b', re.IGNORECASE),            'reference to SAS index'),
    (re.compile(r'\.xpt\b', re.IGNORECASE),                 'reference to SAS transport file'),
    (re.compile(r'\.(parquet|feather)\b', re.IGNORECASE),   'reference to analysis data file'),
    (re.compile(r'\.(rds|rdata|rda)\b', re.IGNORECASE),     'reference to R data file'),
    (re.compile(r'\b(stdout|stderr)\.txt\b', re.IGNORECASE), 'reference to execution output file'),
    (re.compile(r'\bexecution\.(log|lst)\b', re.IGNORECASE), 'reference to execution log'),
    (re.compile(r'\bprofile\.json\b', re.IGNORECASE),        'reference to data profile'),
]

# 인터프리터 실행 파일 이름 (토큰의 basename 과 대조)
# 경로/따옴표가 어떻게 붙어도 basename 으로 잡히므로 정규식보다 견고하다
#
# python 을 전면 차단하는 이유
#   python -c "..." 는 임의 코드 실행 창이다. 경로를 문자열 결합으로 만들면
#   (Path(root)/'da'+'ta'/'raw'/x) 명령 문자열에 data 가 나타나지 않아
#   어떤 정규식으로도 잡을 수 없다. 따라서 runner 경유만 허용한다.
INTERPRETER_BASENAMES = {
    'sas':          'SAS',
    'sas.exe':      'SAS',
    'rscript':      'R',
    'rscript.exe':  'R',
    'rterm':        'R',
    'rterm.exe':    'R',
    'r':            'R',
    'r.exe':        'R',
    'ipython':      'Python (ipython)',
    'ipython.exe':  'Python (ipython)',
    'python':       'Python',
    'python.exe':   'Python',
    'python3':      'Python',
    'python3.exe':  'Python',
    'pythonw':      'Python',
    'pythonw.exe':  'Python',
    'py':           'Python',
    'py.exe':       'Python',
    'node':         'Node.js',
    'node.exe':     'Node.js',
    'perl':         'Perl',
    'perl.exe':     'Perl',
    'ruby':         'Ruby',
    'ruby.exe':     'Ruby',
    # Python 코드를 실행하는 도구 (인터프리터를 감싼다)
    'coverage':     'Python (coverage run)',
    'tox':          'Python (tox)',
    'nox':          'Python (nox)',
    'pipenv':       'Python (pipenv)',
    'pdm':          'Python (pdm)',
    'hatch':        'Python (hatch)',
    'poetry':       'Python (poetry)',
    'conda':        'Python (conda)',
}

PYTHON_BASENAME_PATTERN = re.compile(r'^python[0-9.]*(\.exe)?$', re.IGNORECASE)

# 인터프리터 이름을 basename 으로 갖는 정당한 study 경로
# 이것만 예외로 두고 나머지는 전부 실행 파일 후보로 본다.
# programs/leak/python 처럼 임의 위치에 인터프리터를 심는 것을 막는다.
KNOWN_LANGUAGE_DIRS = frozenset({
    'programs/python',
    'programs/r',
    'programs/sas',
    'programs/qc/python',
    'programs/qc/r',
    'programs/qc/sas',
})

# 명령 문자열 어디에 있든 잡아야 하는 인터프리터 호출 패턴
# cmd.exe /c "python -c print(1)" 처럼 따옴표 안에 숨은 경우를 잡는다
INTERPRETER_INLINE_PATTERNS = [
    (re.compile(r'\bpython[0-9.]*(\.exe)?\s+-[cm]\b', re.IGNORECASE), 'Python (inline)'),
    (re.compile(r'\bpython[0-9.]*(\.exe)?\s+\S+\.py\b', re.IGNORECASE), 'Python (script)'),
    (re.compile(r'\bRscript\b', re.IGNORECASE),                        'R'),
    (re.compile(r'\bR\s+-e\b', re.IGNORECASE),                         'R (inline)'),
    (re.compile(r'\bsas(\.exe)?\s+-', re.IGNORECASE),                  'SAS'),
    (re.compile(r'\bnode\s+-e\b', re.IGNORECASE),                      'Node.js (inline)'),
    (re.compile(r'\bperl\s+-e\b', re.IGNORECASE),                      'Perl (inline)'),
    (re.compile(r'\bruby\s+-e\b', re.IGNORECASE),                      'Ruby (inline)'),
    (re.compile(r'-File\s+\S+\.(ps1|bat|cmd|sh)\b', re.IGNORECASE),    'script file execution'),
    (re.compile(r'\S+\.(ps1|bat|cmd|vbs|sh)\b', re.IGNORECASE),        'script file execution'),
]

# 토큰 판정으로 못 잡는 형태를 보완하는 정규식
DIRECT_EXEC_PATTERNS = [
    (re.compile(r'\bsaspy\b', re.IGNORECASE),                              'SAS (saspy)'),
    (re.compile(r'\bR\s+CMD\s+BATCH\b', re.IGNORECASE),                    'R'),
    (re.compile(r'\bjupyter\s+(nbconvert|execute|run|lab|notebook)\b', re.IGNORECASE), 'Jupyter'),
    (re.compile(r'\buv\s+run\b', re.IGNORECASE),                           'Python (uv run)'),
    (re.compile(r'\bsas\s+-sysin\b', re.IGNORECASE),                       'SAS'),
    (re.compile(r'\bconda\s+run\b', re.IGNORECASE),                        'Python (conda run)'),
    (re.compile(r'\bpoetry\s+run\b', re.IGNORECASE),                       'Python (poetry run)'),
]

# 난독화 / 판정 불가 명령
OBFUSCATION_PATTERNS = [
    (re.compile(r'-e(nc|ncoded|ncodedcommand)\b', re.IGNORECASE),  'PowerShell encoded command'),
    (re.compile(r'\bcertutil\b.*-decode', re.IGNORECASE),          'certutil decoding'),
    (re.compile(r'\bFromBase64String\b', re.IGNORECASE),           'base64 decoding'),
    (re.compile(r'\bbase64\s+-d\b', re.IGNORECASE),                'base64 decoding'),
    (re.compile(r'\bIEX\b|\bInvoke-Expression\b', re.IGNORECASE),  'dynamic command execution'),
    (re.compile(r'\bStart-Process\b', re.IGNORECASE),              'indirect process execution'),
    (re.compile(r'\$\(.*\)|`[^`]+`'),                              'command substitution'),
]

# study 트리를 재귀 탐색해 내용을 읽는 명령
# 경로 리터럴에 data 가 없어도 결과적으로 data/ 를 읽으므로 별도 차단이 필요하다
#
# 오탐을 줄이기 위해 "재귀" 요소가 있을 때만 차단한다.
# 예: 'Select-String -Path docs\*.md' 는 허용, '-Recurse' 가 붙으면 차단.
#     범위가 허용 디렉터리인지는 check_data_reference 가 별도로 판정한다.
RECURSIVE_SEARCH_PATTERNS = [
    (re.compile(r'\bfindstr\b[^|;&]*\s/[a-z]*s\b', re.IGNORECASE),           'findstr recursive search'),
    (re.compile(r'-Recurse\b', re.IGNORECASE),                               'recursive traversal (-Recurse)'),
    (re.compile(r'\bdir\b[^|;&]*\s/s\b', re.IGNORECASE),                     'dir /s'),
    (re.compile(r'\b(rg|ripgrep|ack|ag)\b', re.IGNORECASE),                  'recursive text search'),
    (re.compile(r'\bgrep\b[^|;&]*\s-[a-zA-Z]*[rR]\b'),                       'grep -r'),
    (re.compile(r'\bfind\b[^|;&]*-(exec|type\s+f)', re.IGNORECASE),          'find recursive traversal'),
    (re.compile(r'\brobocopy\b|\bxcopy\b', re.IGNORECASE),                   'bulk file copy'),
    (re.compile(r'\bJoin-Path\b', re.IGNORECASE),                            'Join-Path path assembly'),
    (re.compile(r'\bCompress-Archive\b|\b7z\b|\btar\s+-?c', re.IGNORECASE),  'archive creation'),
    (re.compile(r'\bfor\s+/[dfrl]\b', re.IGNORECASE),                        'for loop execution'),
    (re.compile(r'\bForEach-Object\b|\|\s*%\s*\{', re.IGNORECASE),           'ForEach-Object iteration'),
    (re.compile(r'\btree\b', re.IGNORECASE),                                 'tree (exposes directory structure)'),
]

# git 하위명령 중 파일 내용을 출력하는 것
# 정규식으로 'git\s+show' 를 찾으면 'git -C . show' 나 'git --no-pager show' 에 빗나간다.
# 따라서 전역 옵션을 건너뛰고 실제 하위명령 토큰을 찾아 대조한다.
GIT_CONTENT_SUBCOMMANDS = {
    'grep':         'git grep (searches every tracked file)',
    'show':         'git show (prints object contents)',
    'cat-file':     'git cat-file (prints object contents)',
    'archive':      'git archive (extracts the whole tree)',
    'bundle':       'git bundle (extracts the whole repository)',
    'format-patch': 'git format-patch (prints commit contents)',
    'whatchanged':  'git whatchanged (prints patches)',
}

# git 하위명령 + 이 플래그 조합이면 파일 내용이 출력된다
GIT_CONTENT_FLAGS = {
    'log':   ('-p', '--patch', '-u', '--unified', '-U', '--full-diff'),
    'stash': ('-p', '--patch'),
    'diff':  ('--all',),
}

# git diff 에 커밋 참조가 두 개 이상 오면 커밋 간 전체 내용이 출력된다
# 'git diff programs/x.sas' (작업 트리 비교) 는 허용해야 하므로 개수로 구분한다
GIT_DIFF_MAX_REFS = 1

# 커밋 범위 표기 — 토큰 하나이므로 개수 검사를 빠져나간다
#   HEAD~1..HEAD   master...HEAD   HEAD^!
GIT_RANGE_PATTERN = re.compile(r'\.\.\.?|\^!')

# git 전역 옵션 중 값을 받는 것 (다음 토큰을 건너뛰어야 한다)
GIT_GLOBAL_OPTIONS_WITH_VALUE = ('-C', '-c', '--git-dir', '--work-tree', '--namespace')

# 경로 구분자 없이 디렉터리 이름만 나오는 형태 (git grep -- data)
#
# 모든 명령에 적용하면 'echo update the data dictionary' 같은 정당한 문장이 막힌다.
# 파일을 읽는 명령의 인자일 때만 적용한다.
BARE_DIR_TOKENS = ('data', 'listings', 'logs')

FILE_READING_COMMANDS = {
    'git', 'ls', 'dir', 'type', 'cat', 'more', 'less', 'head', 'tail',
    'findstr', 'grep', 'select-string', 'get-content', 'gc',
    'copy', 'cp', 'move', 'mv', 'xcopy', 'robocopy',
    'get-childitem', 'gci', 'tree', 'du', 'wc',
}

# 인자를 실행하지 않는 명령
#
# 'where python' 이나 'echo use SAS output' 처럼 인터프리터 이름이 인자로 와도
# 실행이 아니다. 이 명령들은 인터프리터 토큰 검사에서 제외한다.
#
# **실행 능력이 없는 명령만 넣을 것.**
# POSIX 'command' 는 인자를 실제로 실행하므로 여기에 넣으면 안 된다
# ('command python -c ...' 가 통째로 검사를 건너뛴다).
# 'command -v' 조회 형태만 아래 QUERY_ONLY_FLAGS 로 별도 허용한다.
NON_EXECUTING_COMMANDS = {
    'where', 'which', 'whereis', 'whatis',
    'get-command', 'gcm',
    'echo', 'write-output', 'write-host', 'printf',
}

# 'command' 는 이 플래그가 있을 때만 조회로 본다 (command -v python)
QUERY_ONLY_COMMANDS = {'command'}
QUERY_ONLY_FLAGS = ('-v', '-V')

# 명령 위치의 변수 확장 — hook 은 확장 결과를 알 수 없으므로 판정 불가
#
# 'echo %DATE%' 나 'echo check $nobs' 같은 정당한 사용까지 막지 않기 위해
# **구간의 첫 토큰이 변수일 때만** 차단한다.
# 'set PY=python && %PY% -c ...' 는 두 번째 구간의 첫 토큰이 %PY% 라 잡힌다.
COMMAND_POSITION_VARIABLE = re.compile(
    # Windows %VAR% 와 부분/치환 확장 %VAR:~0,10% %VAR:a=b%
    r'^\s*(%[A-Za-z_][A-Za-z0-9_]*(:[^%]*)?%'
    r'|![A-Za-z_][A-Za-z0-9_]*(:[^!]*)?!'     # 지연확장 !VAR! !VAR:~0%
    r'|\$\{?[A-Za-z_][A-Za-z0-9_:]*\}?'       # POSIX/PowerShell  $VAR ${VAR} $env:VAR
    r'|&\s*[\'"]?\$)'                         # PowerShell 호출 연산자 & $cmd
)

# 명령을 간접 실행하는 형태 (인터프리터 이름이 토큰으로 안 보이는 경우)
INDIRECT_EXEC_PATTERNS = [
    (re.compile(r'\bfor\s+/?%%?[A-Za-z]\b', re.IGNORECASE),      'for loop execution'),
    (re.compile(r'\bforfiles\b', re.IGNORECASE),                 'forfiles'),
    (re.compile(r'\bwmic\b', re.IGNORECASE),                     'wmic'),
    (re.compile(r'\bmshta\b', re.IGNORECASE),                    'mshta'),
    (re.compile(r'\bsetx\b', re.IGNORECASE),                     'setx (persists an environment variable)'),
    (re.compile(r'\brundll32\b|\bregsvr32\b', re.IGNORECASE),    'execution via DLL'),
    (re.compile(r'\bschtasks\b|\bat\s+\d', re.IGNORECASE),       'scheduled execution'),
    (re.compile(r'\bStart-Job\b|\bInvoke-Command\b', re.IGNORECASE), 'PowerShell remote/job execution'),
]

GUIDANCE = """
  Correct way to run programs:
    python scripts/run_sas.py    --program programs/sas/t_dm.sas    --purpose exploratory
    python scripts/run_python.py --program programs/python/t_ae.py  --purpose exploratory
    python scripts/run_r.py      --program programs/r/f_km.R        --purpose exploratory

  Going through a runner records the following automatically:
    - SHA-256 of every input dataset
    - execution log (SAS .log / Python and R logs)
    - assertion results
    - manifest.json and the audit log chain
"""


# ============================================================================
# 메인 로직
# ============================================================================

def split_segments(command):
    """
    셸 명령을 연결 연산자 기준으로 분해한다

    'python scripts/run_sas.py --program x && python -c "..."' 처럼
    허용 명령 뒤에 우회 명령을 붙이는 것을 막기 위해 각 구간을 독립 판정한다.

    Args:
        command: 셸 명령 문자열

    Returns:
        구간 문자열 리스트
    """
    segments = re.split(r'&&|\|\||;|\||&|\bthen\b|\belse\b|\bdo\b', command)
    return [s.strip() for s in segments if s.strip()]


def tokenize(command):
    """
    셸 명령을 토큰으로 분해한다

    shlex 파싱에 실패하면 공백 기준으로 나눈다

    Args:
        command: 셸 명령 문자열

    Returns:
        토큰 문자열 리스트
    """
    try:
        return shlex.split(command, posix=False)
    except ValueError:
        return command.split()


def check_obfuscation(command):
    """
    난독화된 명령인지 검사한다

    Args:
        command: 셸 명령 문자열

    Returns:
        차단 사유 문자열. 문제 없으면 None
    """
    for pattern, label in OBFUSCATION_PATTERNS:
        if pattern.search(command):
            return f"Contains {label}, so the command cannot be inspected"
    return None


def token_basename(token):
    """
    토큰에서 실행 파일 basename 을 추출한다

    따옴표, 경로 구분자, .exe 접미사를 제거한다.
    'env.exe' 와 'env' 를 같게 보기 위해 확장자를 벗긴다.

    Args:
        token: 명령 토큰 문자열

    Returns:
        소문자 basename 문자열
    """
    cleaned = token.strip().strip('"').strip("'")
    cleaned = cleaned.replace('\\', '/').rstrip('/')
    name = cleaned.rsplit('/', 1)[-1].lower()

    for suffix in ('.exe', '.com', '.bat', '.cmd'):
        if name.endswith(suffix):
            return name[:-len(suffix)]

    return name


def is_command_candidate(token, base_dir=None, study_root=None):
    """
    토큰이 실행 파일을 가리키는지 판단한다

    경로 구분자가 있는 토큰은 보통 데이터/코드 경로다.
    'ruff check programs/python' 의 'programs/python' 은 디렉터리이지
    인터프리터가 아니다. 이것을 인터프리터로 오인하면 대량 오탐이 발생한다.

    실행 파일로 보는 조건
      - 경로 구분자가 없다              python, env
      - 실행 파일 확장자를 가진다        C:/Python310/python.exe
      - POSIX 절대경로다                 /usr/bin/python
      - 명시적 상대 실행 표기다          ./python
      - basename 이 인터프리터 이름인데 study 하위 디렉터리가 아니다
                                        C:/Python310/python, ../python

    마지막 조건이 핵심이다.
    "Windows 인터프리터는 항상 .exe" 라는 가정은 틀렸다 (py 런처, 포터블 배포).
    그렇다고 모든 경로의 basename 을 검사하면 'ruff check programs/python' 이
    막힌다. 둘을 가르는 것은 **그 경로가 study 트리 안의 하위 경로인가**이다.
    인터프리터는 study 안에 설치되지 않는다.
      programs/python          study 하위 -> 인터프리터 아님
      C:/Python310/python      study 밖   -> 인터프리터

    Args:
        token: 명령 토큰 문자열
        base_dir: 상대경로 해석 기준 디렉터리 (도구 호출 시점의 cwd)
        study_root: study 루트 Path 또는 None

    Returns:
        실행 파일 후보면 True
    """
    cleaned = token.strip().strip('"').strip("'")
    if not cleaned or cleaned.startswith('-'):
        return False

    normalized = cleaned.replace('\\', '/')

    if '/' not in normalized:
        return True

    lowered = normalized.lower()
    if lowered.endswith(('.exe', '.com', '.bat', '.cmd')):
        return True

    # POSIX 절대경로 (드라이브 문자가 없는 경우만)
    if normalized.startswith('/') and not (len(normalized) >= 3 and normalized[2] == ':'):
        return True

    basename = token_basename(cleaned)
    is_interpreter_name = (
        basename in INTERPRETER_BASENAMES or PYTHON_BASENAME_PATTERN.match(basename)
    )

    # 표준 언어 디렉터리는 실행 파일이 아니다 (programs/python 은 디렉터리)
    # './' 접두보다 먼저 확인해야 './programs/python' 오탐이 없다
    if is_interpreter_name and _is_known_language_dir(cleaned, base_dir, study_root):
        return False

    if normalized.startswith('./'):
        return True

    if is_interpreter_name:
        return True

    return False


def _is_known_language_dir(path_text, base_dir, study_root):
    """
    경로가 study 의 표준 언어 디렉터리인지 확인한다

    인터프리터 이름을 가진 경로 중 정당한 것은 딱 이것들뿐이다.
        programs/python   programs/r   programs/sas

    "study 하위면 전부 제외" 로 하면 안 된다.
    programs/ 와 macros/ 는 Opus 가 **쓸 수 있는** 영역이므로,
    programs/leak/python 에 포터블 인터프리터를 두고 실행할 수 있다
    (6차 검토 발견). 정확히 알려진 경로만 예외로 둔다.

    Args:
        path_text: 검사할 경로 문자열
        base_dir: 상대경로 해석 기준 디렉터리
        study_root: study 루트 Path 또는 None

    Returns:
        표준 언어 디렉터리면 True
    """
    if study_root is None:
        return False

    for base in (base_dir, study_root):
        if not base:
            continue
        try:
            candidate = Path(path_text)
            if not candidate.is_absolute():
                candidate = Path(base) / candidate
            relative = candidate.resolve().relative_to(Path(study_root).resolve())
        except (OSError, ValueError, RuntimeError):
            continue

        normalized = str(relative).replace('\\', '/').strip('/').lower()
        if normalized in KNOWN_LANGUAGE_DIRS:
            return True

    return False


def check_direct_exec(command, tokens, base_dir=None, study_root=None):
    """
    runner 를 우회한 인터프리터 직접 실행인지 검사한다

    판정 원칙
      1. **모든 토큰에서 실행 파일 후보를 찾는다.** 접두 래퍼 목록 방식은
         근본적으로 불완전하다. env / timeout / busybox / taskset / firejail /
         systemd-run / pipenv run 처럼 래퍼는 끝없이 나오고,
         .exe 접미사와 /T 같은 플래그 변형까지 더해진다.
      2. **경로 구분자로 실행 파일과 데이터 경로를 구분한다.**
         'ruff check programs/python' 의 programs/python 은 디렉터리이지
         인터프리터가 아니다 (is_command_candidate 참조).
      3. 셸 래퍼(cmd, powershell, sh)는 어느 위치에 있든 차단한다.
      4. 따옴표 안 우회를 잡기 위해 명령 문자열 전체에서 인터프리터 패턴도 본다.

    Args:
        command: 셸 명령 문자열
        tokens: 분해된 토큰 리스트

    Returns:
        차단 사유 문자열. 문제 없으면 None
    """
    if RUNNER_ALLOW_PATTERN.match(command):
        return None

    # plugin 개발용 명령은 인터프리터 차단만 면제한다.
    # 다른 검사(난독화, 변수 확장, git, 재귀 탐색, 데이터 경로)는 그대로 돈다.
    if DEV_COMMAND_PATTERN.match(command):
        return None

    def blocked(label):
        return (
            f"Direct execution of {label} is blocked. "
            f"Go through a runner so the log and manifest are recorded"
        )

    if not tokens:
        return None

    # 인자를 실행하지 않는 명령은 인터프리터 검사 대상이 아니다
    # (where python, echo use SAS output)
    first = token_basename(tokens[0])
    if first in NON_EXECUTING_COMMANDS:
        return None

    # 'command' 는 인자를 실행한다. -v / -V 조회 형태만 예외로 허용한다
    if first in QUERY_ONLY_COMMANDS:
        rest = [t.strip().strip('"').strip("'") for t in tokens[1:]]
        if rest and rest[0] in QUERY_ONLY_FLAGS:
            return None

    # 1. 모든 토큰에서 실행 파일 후보를 검사한다
    for token in tokens:
        if not is_command_candidate(token, base_dir, study_root):
            continue

        basename = token_basename(token)
        if not basename:
            continue

        if basename in SHELL_WRAPPER_BASENAMES:
            return (
                f"Shell wrapper ({basename}) execution is blocked. "
                f"A wrapper can hide arbitrary commands inside quotes or a script "
                f"file, bypassing command inspection"
            )

        if basename in INTERPRETER_BASENAMES:
            return blocked(INTERPRETER_BASENAMES[basename])

        if PYTHON_BASENAME_PATTERN.match(basename):
            return blocked('Python')

    # 2. 명령 문자열 전체에서 인터프리터 패턴 (따옴표 안 우회 대응)
    for pattern, label in INTERPRETER_INLINE_PATTERNS:
        if pattern.search(command):
            return blocked(label)

    # 3. 간접 실행 형태 (실행될 명령을 검사할 수 없는 경우)
    for pattern, label in INDIRECT_EXEC_PATTERNS:
        if pattern.search(command):
            return f"{label} is blocked. The command it would run cannot be inspected"

    # 4. 정규식 보완
    for pattern, label in DIRECT_EXEC_PATTERNS:
        if pattern.search(command):
            return blocked(label)

    return None


def check_variable_expansion(segment, tokens):
    """
    실행될 수 있는 위치에 변수 확장을 쓰는지 검사한다

    hook 은 변수 확장 결과를 알 수 없다.
    'set PY=python && %PY% -c "..."' 처럼 확장 후에야 인터프리터가 드러나면
    어떤 패턴으로도 잡을 수 없다.

    다만 **인자를 실행하지 않는 명령(echo 등)에서는 허용**한다.
    전면 차단하면 다음이 모두 막혀 오탐이 커진다.
        echo Today is %DATE%
        echo check $nobs in log
        set PURPOSE=exploratory
        git log --format=%H

    반대로 'call %PY% -c ...' 처럼 접두가 붙어도 실행되므로,
    구간 선두뿐 아니라 모든 토큰을 본다.

    Args:
        segment: 검사할 명령 구간
        tokens: 분해된 토큰 리스트

    Returns:
        차단 사유 문자열. 문제 없으면 None
    """
    if RUNNER_ALLOW_PATTERN.match(segment):
        return None

    if not tokens:
        return None

    # 인자를 실행하지 않는 명령은 변수를 인자로 써도 안전하다
    first = token_basename(tokens[0])
    if first in NON_EXECUTING_COMMANDS:
        return None

    # set VAR=값 은 대입일 뿐이다 (확장 결과가 실행되지 않는다)
    if first == 'set' and len(tokens) >= 2 and '=' in tokens[1]:
        return None

    for token in tokens:
        cleaned = token.strip().strip('"').strip("'")
        if COMMAND_POSITION_VARIABLE.match(cleaned):
            return (
                f"Variable expansion ({cleaned[:40]}) is blocked. "
                f"The expanded result cannot be inspected, so it can hide arbitrary commands"
            )

    return None


def check_git(tokens):
    """
    git 명령이 파일 내용을 출력하는지 검사한다

    전역 옵션을 건너뛰고 실제 하위명령을 찾는다.
    'git -C . show HEAD:data/raw/x.csv' 나 'git --no-pager log -p' 처럼
    옵션이 끼어도 잡아야 한다.

    Args:
        tokens: 분해된 토큰 리스트

    Returns:
        차단 사유 문자열. 문제 없으면 None
    """
    # git 은 어느 위치에 있어도 찾는다.
    # 'env git show HEAD' 나 'timeout 5 git log -p' 처럼 접두 한 겹만 있어도
    # 첫 토큰만 보면 내용 검사가 통째로 무력화된다.
    git_index = None
    for index, token in enumerate(tokens):
        if token_basename(token) == 'git':
            git_index = index
            break

    if git_index is None:
        return None

    # alias 는 임의의 하위명령이나 외부 명령을 심을 수 있다
    #   git -c alias.s=show s HEAD        -> show 로 우회
    #   git -c alias.x=!python x payload  -> 인터프리터 직접 실행
    #
    # 'alias.' 부분 문자열로 찾으면 커밋 메시지나 이메일 도메인에도 걸린다
    # (git commit -m "update alias.docs"). 실제 설정 형태만 본다.
    for position, token in enumerate(tokens):
        cleaned = token.strip().strip('"').strip("'").lower()
        if not cleaned.startswith('alias.'):
            continue
        previous = tokens[position - 1].strip().strip('"').strip("'").lower() \
            if position > 0 else ''
        if previous in ('-c', '--config', 'config') or cleaned.count('=') >= 1:
            if previous in ('-c', '--config', 'config'):
                return (
                    "Setting a git alias is blocked. "
                    "An alias can run an arbitrary subcommand or external command"
                )

    # 전역 옵션을 건너뛰고 하위명령을 찾는다
    index = git_index + 1
    while index < len(tokens):
        token = tokens[index].strip().strip('"').strip("'")
        if not token.startswith('-'):
            break
        if token in GIT_GLOBAL_OPTIONS_WITH_VALUE:
            index += 2
        else:
            index += 1

    if index >= len(tokens):
        return None

    subcommand = tokens[index].strip().strip('"').strip("'").lower()

    if subcommand in GIT_CONTENT_SUBCOMMANDS:
        return (
            f"{GIT_CONTENT_SUBCOMMANDS[subcommand]} is blocked. "
            f"If any data was committed, its contents would be exposed verbatim"
        )

    flags = GIT_CONTENT_FLAGS.get(subcommand)
    if flags:
        rest = [t.strip().strip('"').strip("'") for t in tokens[index + 1:]]
        for token in rest:
            for flag in flags:
                if token == flag or token.startswith(flag + '='):
                    return (
                        f"git {subcommand} {flag} is blocked. "
                        f"Committed file contents would be printed as a patch"
                    )
            # -U3 처럼 값이 붙은 형태
            if subcommand == 'log' and re.fullmatch(r'-U\d+', token):
                return (
                    "git log -U is blocked. "
                    "Committed file contents would be printed as a patch"
                )

    # git diff / log 에 커밋 범위가 오면 커밋 간 전체 내용이 출력된다
    if subcommand in ('diff', 'log', 'show'):
        rest = []
        for token in tokens[index + 1:]:
            cleaned = token.strip().strip('"').strip("'")
            # '--' 뒤는 전부 경로다. 커밋 참조로 세면 안 된다
            if cleaned == '--':
                break
            if cleaned.startswith('-'):
                continue
            rest.append(cleaned)

        # 범위 표기는 토큰 하나라 개수 검사를 빠져나간다 (HEAD~1..HEAD)
        for token in rest:
            if GIT_RANGE_PATTERN.search(token):
                return (
                    f"git {subcommand} with commit range notation is blocked ({token}). "
                    f"File contents across commits would be printed"
                )

        # 커밋 참조가 둘 이상이면 범위 비교다.
        # 경로처럼 보이는 토큰은 제외한다 (git diff HEAD -- programs/x.sas)
        if subcommand == 'diff':
            refs = [t for t in rest if '/' not in t and '\\' not in t and '.' not in t]
            if len(refs) > GIT_DIFF_MAX_REFS:
                return (
                    f"git diff with multiple commit references is blocked "
                    f"({', '.join(refs[:3])}). Full contents across commits would be printed"
                )

    return None


def check_recursive_search(command):
    """
    study 트리를 재귀 탐색해 내용을 읽는 명령인지 검사한다

    경로 리터럴에 data 가 없어도 결과적으로 data/ 아래를 읽으므로
    명령 형태 자체로 차단한다.

    Args:
        command: 셸 명령 문자열

    Returns:
        차단 사유 문자열. 문제 없으면 None
    """
    if RUNNER_ALLOW_PATTERN.match(command):
        return None

    for pattern, label in RECURSIVE_SEARCH_PATTERNS:
        if pattern.search(command):
            return (
                f"{label} is blocked. "
                f"Sweeping the study tree can expose subject data under data/. "
                f"To list files, use the Glob tool with an explicit allowed directory"
            )
    return None


def check_data_reference(command, tokens, study_root, config, base_dir=None):
    """
    임상 데이터 경로를 참조하는지 검사한다

    경로로 보이는 토큰에만 classify_path 를 적용한다.
    경로가 아닌 토큰(git, USUBJID)까지 판정하면 기본 거부 정책 때문에
    대량 오탐이 발생한다.

    Args:
        command: 셸 명령 문자열
        tokens: 분해된 토큰 리스트
        study_root: study 루트 Path 또는 None
        config: config 딕셔너리
        base_dir: 상대경로 해석 기준 디렉터리 (도구 호출 시점의 cwd)

    Returns:
        (차단 사유, 문제된 토큰) 튜플. 문제 없으면 (None, None)
    """
    from gxpllm.core import classify_path, looks_like_path

    first = token_basename(tokens[0]) if tokens else ''

    # 인자를 실행하지도 읽지도 않는 명령은 경로 검사에서 제외한다.
    # 'echo see logs/runs for results' 같은 설명 문장이 막히면
    # 사용자가 hook 을 끄게 되고, 그것이 가장 확실한 경계 붕괴다.
    if first in NON_EXECUTING_COMMANDS:
        return None, None

    # 파일을 읽는 명령일 때만 bare 디렉터리 이름을 검사한다.
    # 모든 명령에 적용하면 'echo update the data dictionary' 같은 문장이 막힌다.
    check_bare = first in FILE_READING_COMMANDS

    for token in tokens:
        cleaned = token.strip().strip('"').strip("'")
        if not cleaned:
            continue

        # 경로 구분자 없이 디렉터리 이름만 쓴 경우 (git grep -- data)
        if check_bare and cleaned.lower().strip('/\\') in BARE_DIR_TOKENS:
            return f"Blocked directory name ({cleaned})", cleaned

        if not looks_like_path(cleaned):
            continue

        reason = classify_path(cleaned, study_root, config, base_dir=base_dir)
        if reason:
            return reason, cleaned

    for pattern, label in DATA_PATH_PATTERNS:
        match = pattern.search(command)
        if match:
            return label, match.group(0)

    return None, None


def check_segment(segment, study_root, config, base_dir=None):
    """
    명령 한 구간을 검사한다

    Args:
        segment: 검사할 명령 구간
        study_root: study 루트 Path 또는 None
        config: config 딕셔너리
        base_dir: 상대경로 해석 기준 디렉터리

    Returns:
        (차단 사유, 대상) 튜플. 문제 없으면 (None, None)
    """
    tokens = tokenize(segment)

    reason = check_obfuscation(segment)
    if reason:
        return reason, segment[:120]

    reason = check_variable_expansion(segment, tokens)
    if reason:
        return reason, segment[:120]

    reason = check_direct_exec(segment, tokens, base_dir, study_root)
    if reason:
        return reason, segment[:120]

    reason = check_git(tokens)
    if reason:
        return reason, segment[:120]

    reason = check_recursive_search(segment)
    if reason:
        return reason, segment[:120]

    reason, offending = check_data_reference(segment, tokens, study_root, config, base_dir)
    if reason:
        return f"{reason} — '{offending}'", offending

    return None, None


def record_block(study_root, command, reason):
    """
    차단 사실을 감사 로그에 기록한다

    Args:
        study_root: study 루트 경로 (없으면 기록 생략)
        command: 차단된 명령
        reason: 차단 사유
    """
    if not study_root:
        return
    try:
        from gxpllm.core import append_audit
        append_audit(study_root, {
            'event': 'access_blocked',
            'tool': 'Bash',
            'target': str(command)[:500],
            'reason': reason,
            'hook': 'guard_bash',
        })
    except Exception:
        pass


def main():
    """메인 함수"""
    # --- 입력 파싱 ---------------------------------------------------------
    try:
        payload = read_hook_payload()
    except Exception as exc:
        print(f"[gxpllm-guard] Blocked: cannot parse hook input: {exc}", file=sys.stderr)
        sys.exit(EXIT_BLOCK)

    # --- 판정 --------------------------------------------------------------
    try:
        from gxpllm.core import find_study_root

        command = (payload.get('tool_input') or {}).get('command', '')
        if not isinstance(command, str) or not command.strip():
            sys.exit(EXIT_ALLOW)

        cwd = payload.get('cwd') or os.getcwd()
        study_root, config = find_study_root(cwd)

        # 연결 연산자로 나눈 각 구간을 독립 판정한다.
        # 'runner && python -c "..."' 처럼 허용 명령 뒤에 우회 명령을 붙이는 것을 막는다.
        for segment in split_segments(command):
            reason, target = check_segment(segment, study_root, config, base_dir=cwd)
            if reason:
                record_block(study_root, command, reason)
                print(
                    f"[gxpllm-guard] Command blocked\n"
                    f"  command: {command[:300]}\n"
                    f"  segment: {segment[:200]}\n"
                    f"  reason:  {reason}\n"
                    f"{GUIDANCE}",
                    file=sys.stderr,
                )
                sys.exit(EXIT_BLOCK)

        sys.exit(EXIT_ALLOW)

    except SystemExit:
        raise
    except Exception as exc:
        print(
            f"[gxpllm-guard] Blocked by internal error (fail-closed): "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        sys.exit(EXIT_BLOCK)


if __name__ == "__main__":
    main()
