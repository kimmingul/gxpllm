"""
gxpllm 공통 코어 모듈

hook과 runner가 함께 사용하는 기반 기능을 제공한다.
- study 설정 탐색 및 로드
- 파일 해시 계산
- run_id 생성
- 경로 차단 판정 (데이터 경계)
- 감사 로그 해시 체인 append / 검증

중요: 이 모듈은 표준 라이브러리만 사용한다.
      hook은 서드파티 패키지가 없는 환경에서도 반드시 동작해야 한다.
"""

import hashlib
import json
import os
import re
import socket
import sys
import uuid
from datetime import datetime
from pathlib import Path

# ============================================================================
# 설정 영역 - 여기서 수정하세요
# ============================================================================

CONFIG_RELATIVE_PATH = '.gxpllm/config.json'
AUDIT_RELATIVE_PATH = 'audit/audit.jsonl'
RUNS_RELATIVE_PATH = 'logs/runs'

HASH_CHUNK_SIZE = 1024 * 1024
GENESIS_HASH = '0' * 64

META_BEGIN = 'GXPLLM-META-BEGIN'
META_END = 'GXPLLM-META-END'
META_LIST_KEYS = ('inputs', 'outputs')

# ----------------------------------------------------------------------------
# 접근 정책: study 루트 안은 기본 거부(default-deny) + 허용 목록
#
# 차단 목록 방식은 우회 경로가 남는다. 특히 logs/ 의 stdout/stderr 는
# runner 가 실행한 프로그램의 출력이므로 PHI 가 그대로 담길 수 있다.
# 따라서 study 루트 안에서는 "명시적으로 허용된 것만" 읽을 수 있게 한다.
# ----------------------------------------------------------------------------

# Opus 가 읽을 수 있는 디렉터리 (study 루트 기준 상대경로)
# 이 목록은 plugin 코드에만 존재한다. study 설정으로 덮어쓸 수 없다.
DEFAULT_ALLOWED_DIRS = (
    'docs',              # Protocol, SAP, DMP, Data Dictionary
    'programs',          # SAS / Python / R 코드 (데이터 아님)
    'macros',            # SAS 매크로
    'templates',         # 분석 템플릿
    'spec',              # 사양서
    'validation',        # 밸리데이션 문서
    'output/tables',     # 집계 표
    'output/figures',    # 그림
    'audit',             # 감사 로그 (구조화 메타데이터)
    '.gxpllm',            # study 설정
    'scripts',           # runner 스크립트 (study 안에 복사된 경우)
)

# Opus 가 쓸 수 있는 디렉터리 (읽기 허용 목록의 부분집합)
# 감사 증적과 설정은 Opus 가 고칠 수 없어야 한다.
#   .gxpllm/  : allowed_dirs 자가 확장 방지 (정책 루트 보호)
#   audit/   : 차단 기록 말소 방지
#   logs/    : manifest 위조 방지
WRITABLE_DIRS = (
    'docs',
    'programs',
    'macros',
    'templates',
    'spec',
    'validation',
)

# logs/ 는 전체 거부하되, 아래 파일명만 예외로 허용한다.
# manifest.json / assertions.json / env_snapshot.json 은 구조화 메타데이터이며
# 원본 데이터 값을 담지 않는다. stdout.txt / execution.log 는 담을 수 있으므로 거부.
DEFAULT_ALLOWED_LOG_FILES = (
    'manifest.json',
    'assertions.json',
    'env_snapshot.json',
)

# study 루트 밖에서도 항상 차단할 확장자 (임상 데이터 고유 포맷)
DEFAULT_BLOCKED_EXTENSIONS = (
    '.sas7bdat', '.sas7bndx', '.sd2', '.sd7',
    '.xpt', '.cport',
    '.sav', '.dta',
)

# 눈가림 상태에서 차단할 키워드 (output/ 산출물에만 적용)
# docs/ 의 randomization_plan.md 같은 정당한 문서를 막지 않기 위해 범위를 좁힌다
BLIND_KEYWORDS = (
    'trt01a', 'trt01p', 'trtp', 'trta', 'trtan',
    'actarm', 'unblind', 'randcode',
)
BLIND_SCOPE_PREFIXES = ('output',)

# 검색 도구(Grep/Glob)는 허용 디렉터리를 명시해야 한다.
# 범위 미지정 또는 study 루트 지정은 data/ 를 훑게 되므로 거부한다.
SEARCH_REQUIRES_EXPLICIT_SCOPE = True

VALID_PURPOSES = ('exploratory', 'qc', 'submission_candidate')


# ============================================================================
# 설정 탐색 / 로드
# ============================================================================

def find_study_root(start_dir=None):
    """
    시작 디렉터리에서 위로 올라가며 .gxpllm/config.json 을 가진 디렉터리를 찾는다

    Args:
        start_dir: 탐색 시작 디렉터리. None이면 현재 작업 디렉터리

    Returns:
        (study_root: Path 또는 None, config: dict)
        config.json 파싱에 실패하면 (study_root, {}) 를 돌려준다
    """
    start = Path(start_dir or os.getcwd())
    try:
        current = start.resolve()
    except OSError:
        current = start

    for candidate in [current] + list(current.parents):
        config_path = candidate / CONFIG_RELATIVE_PATH
        try:
            if config_path.is_file():
                with open(config_path, 'r', encoding='utf-8') as f:
                    return candidate, json.load(f)
        except (OSError, ValueError):
            return candidate, {}

    return None, {}


def load_config(start_dir=None, required=False):
    """
    study 설정을 읽고 root 경로를 보정해서 돌려준다

    Args:
        start_dir: 탐색 시작 디렉터리
        required: True이면 설정을 찾지 못했을 때 예외를 던진다

    Returns:
        config 딕셔너리. 'root' 키에 study 루트 절대경로가 채워진다

    Raises:
        FileNotFoundError: required=True인데 설정을 찾지 못한 경우
    """
    root, config = find_study_root(start_dir)

    if root is None:
        if required:
            raise FileNotFoundError(
                f"{CONFIG_RELATIVE_PATH} 를 찾을 수 없습니다. "
                f"study 디렉터리 안에서 실행하십시오. (탐색 시작: {start_dir or os.getcwd()})"
            )
        return {}

    config = dict(config)
    # config에 root가 있어도 실제 발견 위치를 우선한다 (복사/이동 대응)
    config['root'] = str(root)
    return config


def get_allowed_dirs(config=None):
    """
    Opus 가 읽을 수 있는 디렉터리 목록을 돌려준다

    **study 설정(config.json)으로 덮어쓸 수 없다.**
    .gxpllm/ 은 Opus 가 쓸 수 있는 영역이므로, 여기서 config 를 신뢰하면
    allowed_dirs 에 'data' 를 추가해 정책 전체를 무력화할 수 있다.
    정책은 plugin 코드에만 존재한다.

    Args:
        config: 호환성을 위해 받지만 사용하지 않는다

    Returns:
        슬래시 구분 소문자 상대경로 튜플
    """
    return tuple(
        str(p).replace('\\', '/').strip('/').lower()
        for p in DEFAULT_ALLOWED_DIRS if str(p).strip()
    )


def get_allowed_log_files(config=None):
    """
    logs/ 안에서 예외적으로 읽기 허용할 파일명 목록을 돌려준다

    **study 설정으로 덮어쓸 수 없다** (get_allowed_dirs 와 같은 이유).

    Args:
        config: 호환성을 위해 받지만 사용하지 않는다

    Returns:
        소문자 파일명 튜플
    """
    return tuple(str(p).strip().lower() for p in DEFAULT_ALLOWED_LOG_FILES if str(p).strip())


def get_blocked_extensions(config=None):
    """
    차단 대상 확장자 목록을 돌려준다

    **study 설정으로 덮어쓸 수 없다** (get_allowed_dirs 와 같은 이유).
    설정으로 목록을 비우면 임상 데이터 파일이 그대로 열린다.

    Args:
        config: 호환성을 위해 받지만 사용하지 않는다

    Returns:
        소문자 확장자 튜플 (점 포함)
    """
    normalized = []
    for ext in DEFAULT_BLOCKED_EXTENSIONS:
        ext = str(ext).strip().lower()
        if ext and not ext.startswith('.'):
            ext = '.' + ext
        if ext:
            normalized.append(ext)
    return tuple(normalized)


# ============================================================================
# 경로 차단 판정 (데이터 경계)
# ============================================================================

def _normalize_for_match(path_text):
    """
    경로 문자열을 비교용으로 정규화한다

    Windows 경로 구분자와 대소문자, 따옴표, 잉여 공백을 정리한다

    Args:
        path_text: 원본 경로 문자열

    Returns:
        슬래시 구분 소문자 문자열
    """
    text = str(path_text).strip().strip('"').strip("'")
    text = text.replace('\\', '/')
    while '//' in text:
        text = text.replace('//', '/')
    return text.lower()


def resolve_relative(target_path, study_root, base_dir=None):
    """
    경로를 study 루트 기준 상대경로로 변환한다

    '..' 와 심볼릭 링크를 해석한 뒤 판정하므로 상대경로 탈출을 막는다.
    상대경로는 base_dir(보통 현재 작업 디렉터리) 기준으로 해석한다.

    Args:
        target_path: 검사할 경로 문자열
        study_root: study 루트 Path 또는 None
        base_dir: 상대경로 해석 기준 디렉터리. None 이면 study_root 사용

    Returns:
        (상대경로 문자열 또는 None, study 루트 안이면 True)
        study_root 가 None 이거나 해석 실패면 (None, False)
    """
    if study_root is None:
        return None, False

    cleaned = str(target_path).strip().strip('"').strip("'")
    if not cleaned:
        return None, False

    try:
        root = Path(study_root).resolve()
        candidate = Path(cleaned)
        if not candidate.is_absolute():
            base = Path(base_dir) if base_dir else root
            candidate = base / candidate
        resolved = candidate.resolve()
    except (OSError, RuntimeError, ValueError):
        return None, False

    try:
        relative = resolved.relative_to(root)
    except ValueError:
        return None, False

    return _normalize_for_match(relative), True


def looks_like_path(value, extra_extensions=()):
    """
    문자열이 파일 경로로 보이는지 판단한다

    경로가 아닌 토큰(git, --flag, USUBJID)을 경로 판정에 넣으면
    기본 거부 정책 때문에 대량 오탐이 발생하므로 먼저 걸러낸다.

    Args:
        value: 검사할 문자열
        extra_extensions: 경로로 볼 추가 확장자 튜플

    Returns:
        경로 후보면 True
    """
    if not isinstance(value, str):
        return False

    stripped = value.strip().strip('"').strip("'")
    if not stripped or len(stripped) > 4096:
        return False

    if stripped.startswith('-'):
        return False

    # 경로 구분자가 있으면 경로로 본다
    if '/' in stripped or '\\' in stripped:
        return True

    # 드라이브 문자 (D:)
    if len(stripped) >= 2 and stripped[1] == ':':
        return True

    # 확장자를 가진 파일명 형태
    lowered = stripped.lower()
    known = tuple(DEFAULT_BLOCKED_EXTENSIONS) + tuple(extra_extensions) + (
        '.sas', '.py', '.r', '.md', '.json', '.yaml', '.yml',
        '.csv', '.tsv', '.xlsx', '.xls', '.parquet', '.feather',
        '.rds', '.rdata', '.rda', '.txt', '.log', '.lst', '.rtf', '.pdf',
    )
    return lowered.endswith(known)


def _is_under(relative, prefix):
    """
    상대경로가 특정 디렉터리 아래인지 확인한다

    Args:
        relative: 슬래시 구분 소문자 상대경로
        prefix: 비교할 디렉터리 (슬래시 구분 소문자)

    Returns:
        아래에 있으면 True
    """
    return relative == prefix or relative.startswith(prefix + '/')


def get_writable_dirs():
    """
    Opus 가 쓸 수 있는 디렉터리 목록을 돌려준다

    Returns:
        슬래시 구분 소문자 상대경로 튜플
    """
    return tuple(
        str(p).replace('\\', '/').strip('/').lower()
        for p in WRITABLE_DIRS if str(p).strip()
    )


def classify_path(target_path, study_root=None, config=None, base_dir=None, mode='read'):
    """
    해당 경로를 Opus 가 읽거나 써도 되는지 판정한다

    정책
      study 루트 안  : 기본 거부(default-deny). 허용 디렉터리 목록에 있어야 통과
      study 루트 밖  : 기본 허용. 단 임상 데이터 고유 확장자는 항상 거부
      logs/          : 전체 거부. manifest.json / assertions.json 등만 예외 허용
      눈가림 상태    : output/ 산출물의 치료군 관련 파일 거부

    차단 목록이 아니라 허용 목록을 쓰는 이유
      runner 가 실행한 프로그램의 stdout/stderr/로그에는 PHI 가 담길 수 있다.
      새 산출물 경로가 생길 때마다 차단 목록에 추가하는 방식은 반드시 누락된다.

    Args:
        target_path: 검사할 경로 문자열
        study_root: study 루트 Path 또는 None
        config: config 딕셔너리 또는 None
        base_dir: 상대경로 해석 기준 디렉터리 (보통 도구 호출 시점의 cwd)
        mode: 'read' 또는 'write'. write 는 더 좁은 허용 목록을 쓴다

    Returns:
        차단 사유 문자열. 허용되면 None
    """
    config = config or {}
    normalized = _normalize_for_match(target_path)
    if not normalized:
        return None

    # --- 1. 임상 데이터 고유 확장자는 위치와 무관하게 항상 거부 ---------------
    for ext in get_blocked_extensions(config):
        if normalized.endswith(ext):
            return f"임상 데이터 파일 확장자입니다 ({ext})"

    relative, inside = resolve_relative(target_path, study_root, base_dir)

    # --- 2. study 루트 밖: 기본 허용 -----------------------------------------
    if not inside:
        # study_root 를 못 찾은 상태에서 상대경로가 data/ 로 시작하면 보수적으로 거부
        if study_root is None and (normalized.startswith('data/')
                                   or normalized.startswith('./data/')
                                   or '/data/' in '/' + normalized):
            return "임상 데이터 디렉터리 경로로 보입니다 (study 설정을 찾을 수 없어 보수적으로 차단)"
        return None

    writing = str(mode).lower() == 'write'

    # --- 3. logs/ : 구조화 메타데이터만 읽기 허용, 쓰기는 전면 금지 ----------
    if _is_under(relative, 'logs'):
        if writing:
            return (
                f"logs/ 에는 쓸 수 없습니다 ({relative}). "
                f"실행 기록은 runner 만 생성합니다. manifest 를 직접 만들면 감사 증적이 무너집니다"
            )
        filename = relative.rsplit('/', 1)[-1]
        if filename in get_allowed_log_files(config):
            return None
        return (
            f"실행 로그는 읽을 수 없습니다 ({relative}). "
            f"프로그램 출력에는 피험자 데이터가 포함될 수 있습니다. "
            f"manifest.json 과 assertions.json 만 읽을 수 있습니다"
        )

    # --- 4. 허용 디렉터리 확인 (기본 거부) -----------------------------------
    allowed_dirs = get_allowed_dirs(config)
    in_allowed = any(_is_under(relative, prefix) for prefix in allowed_dirs)

    if not in_allowed:
        # study 루트 바로 아래의 문서 파일은 **읽기만** 허용 (README.md 등)
        # 쓰기까지 허용하면 루트에 PHI 를 기록하거나 config 를 위조할 수 있다
        if (not writing
                and '/' not in relative
                and relative.endswith(('.md', '.txt', '.json', '.yaml', '.yml'))):
            return None
        return (
            f"허용되지 않은 경로입니다 ({relative}). "
            f"{'쓸' if writing else '읽을'} 수 있는 위치: "
            f"{', '.join(get_writable_dirs() if writing else allowed_dirs)}"
        )

    # --- 4b. 쓰기는 더 좁은 허용 목록을 쓴다 ---------------------------------
    if writing:
        writable = get_writable_dirs()
        if not any(_is_under(relative, prefix) for prefix in writable):
            reason_map = {
                '.gxpllm': '설정을 고치면 경계 정책 자체를 무력화할 수 있습니다',
                'audit': '감사 기록을 고치면 차단 이력을 말소할 수 있습니다',
                'output/tables': '산출물은 runner 가 실행한 프로그램만 생성합니다',
                'output/figures': '산출물은 runner 가 실행한 프로그램만 생성합니다',
                'scripts': 'runner 는 plugin 이 관리합니다',
            }
            detail = ''
            for prefix, why in reason_map.items():
                if _is_under(relative, prefix):
                    detail = f" {why}."
                    break
            return (
                f"쓰기가 허용되지 않은 경로입니다 ({relative}).{detail} "
                f"쓸 수 있는 위치: {', '.join(writable)}"
            )

    # --- 5. 눈가림: output/ 산출물에만 적용 ----------------------------------
    if config.get('blinded', False):
        if any(_is_under(relative, prefix) for prefix in BLIND_SCOPE_PREFIXES):
            filename = relative.rsplit('/', 1)[-1]
            for keyword in BLIND_KEYWORDS:
                if keyword in filename:
                    return (
                        f"눈가림(blinded) 상태에서 치료군 관련 산출물은 읽을 수 없습니다 "
                        f"({keyword})"
                    )

    return None


def classify_search_scope(target_path, study_root=None, config=None, base_dir=None):
    """
    Grep / Glob 의 검색 범위가 안전한지 판정한다

    검색 도구는 범위를 명시하지 않으면 study 루트 전체를 훑으며,
    그 과정에서 data/ 안의 피험자 단위 값이 매칭 결과로 반환된다.
    따라서 허용 디렉터리를 명시한 경우에만 통과시킨다.

    Args:
        target_path: 검색 대상 경로 (None 이면 범위 미지정)
        study_root: study 루트 Path 또는 None
        config: config 딕셔너리 또는 None

    Returns:
        차단 사유 문자열. 안전하면 None
    """
    config = config or {}
    allowed_dirs = get_allowed_dirs(config)

    if study_root is None:
        return None

    if not target_path or not str(target_path).strip():
        if SEARCH_REQUIRES_EXPLICIT_SCOPE:
            return (
                f"검색 범위를 명시해야 합니다. "
                f"path 를 다음 중 하나로 지정하십시오: {', '.join(allowed_dirs)}"
            )
        return None

    relative, inside = resolve_relative(target_path, study_root, base_dir)

    if not inside:
        return None

    if any(_is_under(relative, prefix) for prefix in allowed_dirs):
        return None

    return (
        f"검색 범위가 허용 디렉터리 밖입니다 ({relative or '(study 루트)'}). "
        f"data/ 를 포함한 범위 검색은 피험자 데이터를 노출할 수 있습니다. "
        f"path 를 다음 중 하나로 지정하십시오: {', '.join(allowed_dirs)}"
    )


# ============================================================================
# 해시 / 식별자
# ============================================================================

def sha256_file(path):
    """
    파일의 SHA-256 을 계산한다

    Args:
        path: 파일 경로

    Returns:
        16진수 해시 문자열. 파일을 읽을 수 없으면 None
    """
    try:
        digest = hashlib.sha256()
        with open(path, 'rb') as f:
            while True:
                chunk = f.read(HASH_CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def sha256_text(text):
    """
    문자열의 SHA-256 을 계산한다

    Args:
        text: 대상 문자열

    Returns:
        16진수 해시 문자열
    """
    return hashlib.sha256(str(text).encode('utf-8')).hexdigest()


def canonical_json(obj):
    """
    해시 계산용 정규화 JSON 문자열을 만든다

    키 정렬, 공백 제거, 비ASCII 보존을 적용해 재현 가능한 직렬화를 보장한다

    Args:
        obj: 직렬화할 객체

    Returns:
        정규화된 JSON 문자열
    """
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def make_run_id():
    """
    run_id 를 생성한다

    Returns:
        '{YYYYMMDD}T{HHMMSS}-{6자리 hex}' 형식 문자열
    """
    stamp = datetime.now().strftime('%Y%m%dT%H%M%S')
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


def now_iso():
    """
    현재 시각을 로컬 타임존 포함 ISO-8601 문자열로 돌려준다

    Returns:
        ISO-8601 문자열
    """
    return datetime.now().astimezone().isoformat(timespec='seconds')


def current_user():
    """
    현재 사용자명을 돌려준다

    Returns:
        사용자명 문자열. 확인 불가 시 'unknown'
    """
    for getter in (lambda: os.environ.get('USERNAME'),
                   lambda: os.environ.get('USER'),
                   os.getlogin):
        try:
            value = getter()
            if value:
                return str(value)
        except Exception:
            continue
    return 'unknown'


def current_hostname():
    """
    현재 호스트명을 돌려준다

    Returns:
        호스트명 문자열. 확인 불가 시 'unknown'
    """
    try:
        return socket.gethostname()
    except Exception:
        return 'unknown'


# ============================================================================
# GXPLLM-META 블록 파싱
# ============================================================================

def parse_meta_block(text):
    """
    프로그램 소스에서 GXPLLM-META 블록을 파싱한다

    SAS(/* */), Python(docstring), R(#) 어느 형식이든 동일하게 처리한다.
    각 줄의 선행 주석 기호(*, #, //)와 공백을 제거한 뒤 'key : value' 로 읽는다.

    Args:
        text: 프로그램 소스 전체 문자열

    Returns:
        메타데이터 딕셔너리. inputs/outputs 는 리스트로 반환된다.
        블록이 없으면 빈 딕셔너리
    """
    if META_BEGIN not in text or META_END not in text:
        return {}

    block = text.split(META_BEGIN, 1)[1].split(META_END, 1)[0]
    meta = {}

    for raw_line in block.splitlines():
        line = raw_line.strip()
        # 선행 주석 기호 제거
        line = re.sub(r'^([#*/\-]+|\*/)\s*', '', line).strip()
        if not line or ':' not in line:
            continue

        key, _, value = line.partition(':')
        key = key.strip().lower()
        value = value.strip()
        if not key:
            continue

        if key in META_LIST_KEYS:
            meta[key] = [v.strip() for v in value.split(',') if v.strip()]
        else:
            meta[key] = value

    return meta


def read_program_meta(program_path):
    """
    프로그램 파일을 읽어 GXPLLM-META 블록을 파싱한다

    Args:
        program_path: 프로그램 파일 경로

    Returns:
        메타데이터 딕셔너리
    """
    try:
        text = Path(program_path).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return {}
    return parse_meta_block(text)


# ============================================================================
# 감사 로그 (해시 체인)
# ============================================================================

def audit_path_for(study_root):
    """
    study 루트에 대한 감사 로그 경로를 돌려준다

    Args:
        study_root: study 루트 경로

    Returns:
        audit.jsonl 의 Path
    """
    return Path(study_root) / AUDIT_RELATIVE_PATH


def read_last_audit_entry(audit_path):
    """
    감사 로그의 마지막 항목을 읽는다

    Args:
        audit_path: audit.jsonl 경로

    Returns:
        마지막 항목 딕셔너리. 파일이 없거나 비어 있으면 None
    """
    path = Path(audit_path)
    if not path.is_file():
        return None

    last = None
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                last = json.loads(line)
            except ValueError:
                # 손상된 줄은 무시하되 체인 검증에서 잡힌다
                continue
    return last


def audit_key_path():
    """
    감사 체인 HMAC 키 파일 경로를 돌려준다

    키를 study 트리 밖(사용자 프로필)에 두는 이유
      키가 study 안에 있으면 audit.jsonl 을 통째로 재작성한 뒤
      같은 키로 다시 서명해 검증을 통과시킬 수 있다.
      키를 밖에 두면 study 디렉터리만 조작해서는 위조가 성립하지 않는다.

    한계
      같은 PC 의 같은 사용자는 키 파일에도 접근할 수 있다.
      완전한 변조 방지가 아니라 위조 난이도를 높이는 조치다.
      규제 수준의 보증이 필요하면 WORM 저장소나 원격 타임스탬프가 필요하다.

    Returns:
        키 파일 Path
    """
    base = os.environ.get('GXPLLM_AUDIT_KEY_DIR')
    if base:
        return Path(base) / 'audit.key'

    home = Path(os.path.expanduser('~'))
    return home / '.gxpllm' / 'audit.key'


def load_or_create_audit_key():
    """
    감사 체인 HMAC 키를 읽는다. 없으면 새로 만든다

    Returns:
        키 바이트. 생성/읽기에 실패하면 None (이 경우 순수 SHA-256 으로 대체)
    """
    path = audit_key_path()

    try:
        if path.is_file():
            key = path.read_bytes().strip()
            if key:
                return key
    except OSError:
        return None

    try:
        import secrets
        key = secrets.token_hex(32).encode('ascii')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(key)
        # Windows: 소유자 외 접근 제한 (실패해도 진행)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return key
    except (OSError, ImportError):
        return None


def compute_entry_hash(entry, key=None):
    """
    감사 항목의 entry_hash 를 계산한다

    키가 있으면 HMAC-SHA256, 없으면 SHA-256 을 쓴다.
    entry_hash 자기 자신과 hash_alg 는 계산 대상에서 제외한다.

    Args:
        entry: 감사 항목 딕셔너리
        key: HMAC 키 바이트 또는 None

    Returns:
        16진수 해시 문자열
    """
    payload = {k: v for k, v in entry.items() if k not in ('entry_hash',)}
    message = canonical_json(payload).encode('utf-8')

    if key:
        import hmac
        return hmac.new(key, message, hashlib.sha256).hexdigest()

    return hashlib.sha256(message).hexdigest()


def append_audit(study_root, entry):
    """
    감사 로그에 항목을 append 하고 해시 체인을 유지한다

    Args:
        study_root: study 루트 경로
        entry: 기록할 딕셔너리. seq/ts/prev_hash/entry_hash 는 자동 부여된다

    Returns:
        기록된 항목 딕셔너리 (entry_hash 포함)
    """
    path = audit_path_for(study_root)
    path.parent.mkdir(parents=True, exist_ok=True)

    last = read_last_audit_entry(path)
    prev_hash = last['entry_hash'] if last and 'entry_hash' in last else GENESIS_HASH
    seq = last.get('seq', 0) if last else 0

    key = load_or_create_audit_key()

    record = dict(entry)
    record.setdefault('ts', now_iso())
    record.setdefault('user', current_user())
    record.setdefault('hostname', current_hostname())
    record['seq'] = seq + 1
    record['prev_hash'] = prev_hash
    record['hash_alg'] = 'hmac-sha256' if key else 'sha256'
    record['entry_hash'] = compute_entry_hash(record, key)

    # 동시 append 경합 방지: 파일 잠금 후 기록
    with open(path, 'a', encoding='utf-8', newline='\n') as f:
        _lock_file(f)
        try:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
            f.flush()
            os.fsync(f.fileno())
        finally:
            _unlock_file(f)

    return record


def _lock_file(handle):
    """
    파일에 배타 잠금을 건다 (플랫폼별 구현)

    두 프로세스가 동시에 append 하면 seq/prev_hash 가 충돌해
    체인이 조용히 분기된다. 이를 막는다.

    Args:
        handle: 열린 파일 핸들
    """
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except (OSError, ImportError):
        pass


def _unlock_file(handle):
    """
    파일 잠금을 해제한다

    Args:
        handle: 열린 파일 핸들
    """
    try:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (OSError, ImportError, ValueError):
        pass


def verify_audit_chain(audit_path):
    """
    감사 로그의 해시 체인 무결성을 검증한다

    Args:
        audit_path: audit.jsonl 경로

    Returns:
        (ok: bool, problems: list[str], entry_count: int)
    """
    path = Path(audit_path)
    problems = []

    if not path.is_file():
        return True, [], 0

    key = load_or_create_audit_key()
    expected_prev = GENESIS_HASH
    expected_seq = 1
    count = 0

    with open(path, 'r', encoding='utf-8') as f:
        for line_no, raw in enumerate(f, start=1):
            raw = raw.strip()
            if not raw:
                continue
            count += 1

            try:
                entry = json.loads(raw)
            except ValueError as exc:
                problems.append(f"{line_no}행: JSON 파싱 실패 ({exc})")
                return False, problems, count

            if entry.get('seq') != expected_seq:
                problems.append(
                    f"{line_no}행: seq 불일치 (기대 {expected_seq}, 실제 {entry.get('seq')})"
                )

            if entry.get('prev_hash') != expected_prev:
                problems.append(
                    f"{line_no}행: prev_hash 불일치 — 앞선 항목이 변조/삭제되었습니다"
                )

            # 알고리즘 다운그레이드 거부
            #
            # 항목이 자기 hash_alg 를 고르게 두면, 공격자가 전체를 sha256 으로
            # 갈아끼운 뒤 키 없이 재서명해 검증을 통과시킬 수 있다.
            # 따라서 키가 있으면 모든 항목이 HMAC 이어야 한다.
            entry_alg = entry.get('hash_alg')

            if key is not None and entry_alg != 'hmac-sha256':
                problems.append(
                    f"{line_no}행: 서명 알고리즘 다운그레이드 (기대 hmac-sha256, "
                    f"실제 {entry_alg}) — 체인이 재작성되었을 수 있습니다"
                )
            elif key is None and entry_alg == 'hmac-sha256':
                problems.append(
                    f"{line_no}행: HMAC 서명 항목인데 키를 읽을 수 없어 검증 불가"
                )
            else:
                entry_key = key if entry_alg == 'hmac-sha256' else None
                recomputed = compute_entry_hash(entry, entry_key)
                if entry.get('entry_hash') != recomputed:
                    problems.append(
                        f"{line_no}행: entry_hash 불일치 — 이 항목의 내용이 변조되었습니다"
                    )

            expected_prev = entry.get('entry_hash', GENESIS_HASH)
            expected_seq = entry.get('seq', expected_seq) + 1

    return (len(problems) == 0), problems, count


# ============================================================================
# run 디렉터리
# ============================================================================

def prepare_run_dir(study_root, run_id):
    """
    run 산출물 디렉터리를 생성한다

    Args:
        study_root: study 루트 경로
        run_id: run 식별자

    Returns:
        생성된 run 디렉터리 Path
    """
    run_dir = Path(study_root) / RUNS_RELATIVE_PATH / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def relative_to_root(path, study_root):
    """
    경로를 study 루트 기준 상대경로 문자열로 변환한다

    Args:
        path: 대상 경로
        study_root: study 루트 경로

    Returns:
        슬래시 구분 상대경로 문자열. 변환 불가 시 원본 문자열
    """
    try:
        return str(Path(path).resolve().relative_to(Path(study_root).resolve())).replace('\\', '/')
    except (ValueError, OSError):
        return str(path).replace('\\', '/')


def describe_file(path, study_root):
    """
    파일의 감사용 메타데이터를 만든다

    Args:
        path: 대상 파일 경로
        study_root: study 루트 경로

    Returns:
        path/sha256/bytes/mtime 을 담은 딕셔너리. 파일이 없으면 exists=False
    """
    p = Path(path)
    if not p.is_file():
        return {
            'path': relative_to_root(p, study_root),
            'exists': False,
        }

    stat = p.stat()
    return {
        'path': relative_to_root(p, study_root),
        'exists': True,
        'sha256': sha256_file(p),
        'bytes': stat.st_size,
        'mtime': datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec='seconds'),
    }


# ============================================================================
# 유틸
# ============================================================================

def ensure_purpose(purpose):
    """
    purpose 값을 검증한다

    Args:
        purpose: 검증할 문자열

    Returns:
        검증된 purpose 문자열

    Raises:
        ValueError: 허용되지 않은 값인 경우
    """
    if purpose not in VALID_PURPOSES:
        raise ValueError(
            f"purpose 는 {', '.join(VALID_PURPOSES)} 중 하나여야 합니다 (입력: {purpose})"
        )
    return purpose


def add_plugin_root_to_syspath():
    """
    이 모듈이 속한 plugin 루트를 sys.path 에 추가한다

    hook 스크립트가 'from gxpllm.core import ...' 를 쓸 수 있게 한다

    Returns:
        plugin 루트 Path
    """
    plugin_root = Path(__file__).resolve().parent.parent
    if str(plugin_root) not in sys.path:
        sys.path.insert(0, str(plugin_root))
    return plugin_root
