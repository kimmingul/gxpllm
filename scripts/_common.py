"""
runner 공통 모듈

세 언어(SAS / Python / R) runner 가 공유하는 실행 골격을 제공한다.
- study 설정 로드 및 run 디렉터리 준비
- GXPLLM-META 블록 기반 입출력 추적
- assertions.jsonl 수집 및 판정
- manifest.json 생성
- 감사 로그 append
- 인코딩 자동 감지 파일 읽기

각 언어 runner 는 execute() 함수만 구현하면 된다.
"""

import sys
from pathlib import Path

# plugin 루트를 sys.path 에 추가
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

import json
import os
import subprocess
from datetime import datetime

from gxpllm.core import (
    load_config, make_run_id, prepare_run_dir, now_iso,
    current_user, current_hostname, sha256_file, sha256_text, canonical_json,
    read_program_meta, describe_file, relative_to_root, append_audit,
    ensure_purpose, classify_path,
)

# ============================================================================
# 설정 영역 - 여기서 수정하세요
# ============================================================================

MANIFEST_VERSION = '1.0'
ASSERTIONS_JSONL = 'assertions.jsonl'
ASSERTIONS_JSON = 'assertions.json'
ENV_SNAPSHOT_PATH = 'logs/env_snapshot.json'

# purpose 별로 실패로 간주할 로그 심각도
#
# CRITICAL 은 exploratory 에서도 실패로 처리한다.
# SettingWithCopy, 다대다 MERGE, All-NaN 같은 CRITICAL 은 "조용히 잘못된 결과"를
# 만드는 것들이므로, 탐색적 분석이라도 통과시키면 잘못된 가설이 만들어진다.
# WARNING 만 purpose 에 따라 완화한다.
SEVERITY_FAIL_ON = {
    'exploratory':          ('ERROR', 'CRITICAL'),
    'qc':                   ('ERROR', 'CRITICAL', 'WARNING'),
    'submission_candidate': ('ERROR', 'CRITICAL', 'WARNING'),
}

# assertion 이 0건이면 "검증이 통과했다" 가 아니라 "검증이 없었다" 다.
#
# 실패 수만 보면 assertion 이 통째로 빠진 프로그램이 PASSED 로 나온다.
# 잘린 LLM 응답, assertion 호출을 빠뜨린 코드가 모두 이 경로로 빠져나간다.
#
# qc / submission_candidate 는 예외를 허용하지 않는다.
# exploratory 는 --allow-no-assertions 로만 열 수 있고, 그 사실이
# manifest 와 감사 로그에 남는다.
ASSERTION_REQUIRED_ALWAYS = ('qc', 'submission_candidate')

# 선언되지 않은 산출물 탐지 대상 디렉터리
#
# 프로그램이 GXPLLM-META 에 없는 파일을 만들면 감사 증적이 끊긴다.
# **Opus 가 읽을 수 있는 모든 디렉터리를 감시해야 한다.**
# output/tables 만 감시하면 macros/leak.csv 로 우회된다.
WATCHED_OUTPUT_DIRS = (
    'output/tables',
    'output/figures',
    'output/listings',
    'docs',
    'programs',
    'macros',
    'templates',
    'spec',
    'validation',
)

# 스냅샷에서 내용 해시를 계산할 최대 파일 크기
# mtime 만 쓰면 os.utime 으로 복원해 탐지를 피할 수 있다.
SNAPSHOT_HASH_MAX_BYTES = 64 * 1024 * 1024

ENCODING_CANDIDATES = ('utf-8-sig', 'utf-8', 'cp949', 'euc-kr', 'latin-1')

# 산출물 분류 (경계 판정에 사용)
OUTPUT_CLASSIFICATION = {
    'output/tables':   'table',
    'output/figures':  'figure',
    'output/listings': 'listing',
}


# ============================================================================
# 파일 읽기 (인코딩 자동 감지)
# ============================================================================

def read_text_auto(path, preferred_encoding=None):
    """
    인코딩을 자동 감지해서 텍스트 파일을 읽는다

    한국어 Windows 환경의 SAS 9.4 는 로그를 CP949 로 기록하는 경우가 많다.
    chardet 이 설치되어 있으면 함께 사용하고, 없으면 후보 목록으로 시도한다.

    Args:
        path: 파일 경로
        preferred_encoding: 우선 시도할 인코딩 (config 지정값)

    Returns:
        (텍스트, 사용한 인코딩). 파일이 없으면 ('', None)
    """
    p = Path(path)
    if not p.is_file():
        return '', None

    raw = p.read_bytes()
    if not raw:
        return '', 'empty'

    candidates = []
    if preferred_encoding:
        candidates.append(preferred_encoding)

    try:
        import chardet
        detected = chardet.detect(raw[:200000])
        if detected.get('encoding') and detected.get('confidence', 0) >= 0.6:
            candidates.append(detected['encoding'])
    except ImportError:
        pass

    candidates.extend(ENCODING_CANDIDATES)

    seen = set()
    for encoding in candidates:
        if not encoding or encoding.lower() in seen:
            continue
        seen.add(encoding.lower())
        try:
            return raw.decode(encoding), encoding
        except (UnicodeDecodeError, LookupError):
            continue

    return raw.decode('utf-8', errors='replace'), 'utf-8(replace)'


# ============================================================================
# assertion 수집
# ============================================================================

def collect_assertions(run_dir):
    """
    각 언어가 append 한 assertions.jsonl 을 모아 assertions.json 으로 정리한다

    Args:
        run_dir: run 디렉터리 Path

    Returns:
        (assertion 리스트, 통과 수, 실패 수)
    """
    jsonl_path = Path(run_dir) / ASSERTIONS_JSONL
    assertions = []

    if jsonl_path.is_file():
        text, _ = read_text_auto(jsonl_path)
        for line_no, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                assertions.append(json.loads(line))
            except ValueError:
                assertions.append({
                    'label': f'MALFORMED_LINE_{line_no}',
                    'rule': 'internal',
                    'result': 'FAIL',
                    'message': f'assertions.jsonl {line_no}행을 해석할 수 없습니다',
                })

    passed = sum(1 for a in assertions if str(a.get('result', '')).upper() == 'PASS')
    failed = sum(1 for a in assertions if str(a.get('result', '')).upper() == 'FAIL')
    return assertions, passed, failed


def write_assertions_json(run_dir, run_id, language, assertions, passed, failed):
    """
    수집된 assertion 을 assertions.json 으로 기록한다

    Args:
        run_dir: run 디렉터리 Path
        run_id: run 식별자
        language: sas / python / r
        assertions: assertion 리스트
        passed: 통과 수
        failed: 실패 수

    Returns:
        기록된 파일 Path
    """
    path = Path(run_dir) / ASSERTIONS_JSON
    payload = {
        'run_id': run_id,
        'language': language,
        'total': len(assertions),
        'passed': passed,
        'failed': failed,
        'assertions': assertions,
    }
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


# ============================================================================
# 출력 검증
# ============================================================================

def classify_output(relative_path):
    """
    산출물의 분류를 판정한다 (경계 정책에 사용)

    Args:
        relative_path: study 루트 기준 상대경로

    Returns:
        'table' / 'figure' / 'listing' / 'other'
    """
    normalized = str(relative_path).replace('\\', '/').lower()
    for prefix, label in OUTPUT_CLASSIFICATION.items():
        if normalized.startswith(prefix + '/') or normalized == prefix:
            return label
    return 'other'


def verify_outputs(declared_outputs, study_root):
    """
    GXPLLM-META 에 선언된 산출물이 실제로 생성됐는지 확인한다

    Args:
        declared_outputs: 선언된 상대경로 리스트
        study_root: study 루트 경로

    Returns:
        (산출물 메타 리스트, 누락된 경로 리스트)
    """
    results = []
    missing = []

    for relative in declared_outputs:
        path = Path(study_root) / relative
        info = describe_file(path, study_root)
        info['declared'] = True
        info['classification'] = classify_output(relative)
        results.append(info)
        if not info.get('exists'):
            missing.append(relative)

    return results, missing


def snapshot_output_dirs(study_root):
    """
    감시 대상 디렉터리의 현재 파일 상태를 기록한다

    실행 전후를 비교해 GXPLLM-META 에 선언되지 않은 산출물을 탐지한다.

    **mtime 이 아니라 내용 해시로 비교한다.**
    mtime 만 쓰면 프로그램이 os.utime 으로 원래 시각을 복원해 탐지를 피할 수 있다.

    Args:
        study_root: study 루트 경로

    Returns:
        {상대경로: 지문 문자열} 딕셔너리
    """
    snapshot = {}
    root = Path(study_root)

    for relative_dir in WATCHED_OUTPUT_DIRS:
        directory = root / relative_dir
        if not directory.is_dir():
            continue
        for path in directory.rglob('*'):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
                if stat.st_size <= SNAPSHOT_HASH_MAX_BYTES:
                    fingerprint = f"{stat.st_size}:{sha256_file(path)}"
                else:
                    # 큰 파일은 해시 계산이 비싸므로 크기와 mtime 으로 대체한다
                    fingerprint = f"{stat.st_size}:{stat.st_mtime}"
                snapshot[relative_to_root(path, root)] = fingerprint
            except OSError:
                continue

    return snapshot


def detect_undeclared_outputs(before, study_root, declared_outputs):
    """
    선언되지 않은 산출물을 탐지한다

    프로그램이 GXPLLM-META 에 없는 파일을 만들면 감사 증적이 끊긴다.
    특히 output/tables 에 피험자 단위 데이터를 덤프하면
    Opus 가 읽을 수 있는 영역으로 데이터가 넘어온다.

    Args:
        before: 실행 전 스냅샷
        study_root: study 루트 경로
        declared_outputs: GXPLLM-META 에 선언된 산출물 상대경로 리스트

    Returns:
        미선언 산출물 정보 리스트
    """
    after = snapshot_output_dirs(study_root)
    declared = {
        str(p).replace('\\', '/').strip('/').lower()
        for p in (declared_outputs or [])
    }

    undeclared = []
    for relative, fingerprint in after.items():
        normalized = relative.replace('\\', '/').strip('/').lower()
        if normalized in declared:
            continue
        # 내용이 바뀌지 않은 기존 파일은 제외 (해시 비교이므로 utime 복원으로 못 피한다)
        if relative in before and before[relative] == fingerprint:
            continue

        path = Path(study_root) / relative
        info = describe_file(path, study_root)
        info['classification'] = classify_output(relative)
        info['change'] = 'created' if relative not in before else 'modified'
        undeclared.append(info)

    # 삭제된 파일도 기록한다 (증적 은폐 탐지)
    for relative in before:
        if relative not in after:
            normalized = relative.replace('\\', '/').strip('/').lower()
            if normalized in declared:
                continue
            undeclared.append({
                'path': relative,
                'exists': False,
                'classification': classify_output(relative),
                'change': 'deleted',
            })

    return undeclared


def describe_inputs(declared_inputs, study_root):
    """
    선언된 입력 데이터셋의 해시와 크기를 기록한다

    Args:
        declared_inputs: 선언된 상대경로 리스트
        study_root: study 루트 경로

    Returns:
        (입력 메타 리스트, 존재하지 않는 경로 리스트)
    """
    results = []
    missing = []

    for relative in declared_inputs:
        path = Path(study_root) / relative
        info = describe_file(path, study_root)
        results.append(info)
        if not info.get('exists'):
            missing.append(relative)

    return results, missing


# ============================================================================
# 환경 스냅샷
# ============================================================================

def load_env_snapshot(study_root):
    """
    SessionStart hook 이 남긴 환경 스냅샷을 읽는다

    Args:
        study_root: study 루트 경로

    Returns:
        (스냅샷 딕셔너리, 해시). 없으면 ({}, None)
    """
    path = Path(study_root) / ENV_SNAPSHOT_PATH
    if not path.is_file():
        return {}, None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            snapshot = json.load(f)
        return snapshot, snapshot.get('snapshot_sha256')
    except (OSError, ValueError):
        return {}, None


# ============================================================================
# manifest
# ============================================================================

def build_manifest(context):
    """
    manifest 딕셔너리를 구성한다

    Args:
        context: runner 가 채운 실행 컨텍스트 딕셔너리

    Returns:
        manifest 딕셔너리
    """
    return {
        'manifest_version': MANIFEST_VERSION,
        'run_id': context['run_id'],
        'study_id': context.get('study_id'),
        'language': context['language'],
        'purpose': context['purpose'],
        'program': context['program'],
        'user': current_user(),
        'hostname': current_hostname(),
        'started_at': context['started_at'],
        'finished_at': context['finished_at'],
        'duration_sec': context['duration_sec'],
        'exit_code': context['exit_code'],
        'command': context.get('command'),
        'inputs': context['inputs'],
        'outputs': context['outputs'],
        'missing_inputs': context.get('missing_inputs', []),
        'missing_outputs': context.get('missing_outputs', []),
        'undeclared_outputs': context.get('undeclared_outputs', []),
        'logs': context['logs'],
        'log_scan': context.get('log_scan', {}),
        'assertions': context['assertions_summary'],
        'environment': context['environment'],
        'blinded': context.get('blinded'),
        'sap_reference': context.get('sap_reference'),
        'meta': context.get('meta', {}),
        'result': context['result'],
        'failure_reasons': context.get('failure_reasons', []),
    }


def write_manifest(run_dir, manifest):
    """
    manifest.json 을 기록한다

    Args:
        run_dir: run 디렉터리 Path
        manifest: manifest 딕셔너리

    Returns:
        (파일 Path, manifest SHA-256)
    """
    path = Path(run_dir) / 'manifest.json'
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return path, sha256_text(canonical_json(manifest))


# ============================================================================
# 판정
# ============================================================================

def decide_result(purpose, exit_code, log_findings, assertion_failed,
                  missing_inputs, missing_outputs, undeclared_outputs=None,
                  language=None, assertion_total=None, allow_no_assertions=False):
    """
    실행 성공 여부를 판정한다

    Args:
        purpose: exploratory / qc / submission_candidate
        exit_code: 프로세스 종료 코드
        log_findings: 로그 스캔 결과 리스트
        assertion_failed: 실패한 assertion 수
        missing_inputs: 존재하지 않는 입력 경로 리스트
        missing_outputs: 생성되지 않은 산출물 경로 리스트
        undeclared_outputs: 선언되지 않은 산출물 리스트
        language: sas / python / r (종료 코드 해석에 사용)
        assertion_total: 기록된 assertion 총 건수 (None 이면 검사하지 않음)
        allow_no_assertions: assertion 0건을 의도한 것으로 볼지 여부

    Returns:
        (result 문자열, 실패 사유 리스트)
    """
    reasons = []
    fail_on = SEVERITY_FAIL_ON.get(purpose, SEVERITY_FAIL_ON['exploratory'])

    if exit_code != 0:
        # SAS 만 1(WARNING) 을 정상 범위로 볼 수 있다.
        # Python/R 의 exit 1 은 예외 발생을 뜻하므로 항상 실패다.
        sas_warning_only = (language == 'sas' and exit_code == 1
                            and 'WARNING' not in fail_on)
        if not sas_warning_only:
            reasons.append(f"종료 코드 {exit_code}")

    for severity in fail_on:
        count = sum(1 for f in log_findings if f.get('severity') == severity)
        if count:
            reasons.append(f"로그 {severity} {count}건")

    if assertion_failed:
        reasons.append(f"assertion 실패 {assertion_failed}건")
    elif assertion_total == 0:
        if purpose in ASSERTION_REQUIRED_ALWAYS:
            reasons.append(
                f"assertion 0건 — {purpose} 는 검증 없는 실행을 통과시키지 않습니다. "
                f"프로그램이 잘렸거나 assertion 호출이 빠졌는지 확인하십시오"
            )
        elif not allow_no_assertions:
            reasons.append(
                "assertion 0건 — 검증 장치가 없는 실행입니다. "
                "프로그램이 잘렸거나 assertion 호출이 빠졌는지 확인하십시오. "
                "assertion 이 필요 없는 작업이면 --allow-no-assertions 를 붙이십시오"
            )

    if missing_inputs:
        reasons.append(f"입력 데이터셋 누락 {len(missing_inputs)}건: {', '.join(missing_inputs[:3])}")

    if missing_outputs:
        reasons.append(f"선언된 산출물 미생성 {len(missing_outputs)}건: {', '.join(missing_outputs[:3])}")

    if undeclared_outputs:
        paths = [item.get('path', '?') for item in undeclared_outputs[:3]]
        reasons.append(
            f"선언되지 않은 산출물 {len(undeclared_outputs)}건 생성: {', '.join(paths)} "
            f"— GXPLLM-META 의 outputs 에 선언하거나 프로그램을 수정하십시오"
        )

    return ('FAILED' if reasons else 'PASSED'), reasons


# ============================================================================
# 실행 골격
# ============================================================================

def preflight(program_path, purpose, allow_no_assertions=False):
    """
    실행 전 준비를 수행한다

    Args:
        program_path: 실행할 프로그램 경로
        purpose: exploratory / qc / submission_candidate
        allow_no_assertions: assertion 0건을 의도한 것으로 볼지 여부

    Returns:
        준비된 컨텍스트 딕셔너리

    Raises:
        FileNotFoundError: 프로그램이나 study 설정을 찾을 수 없는 경우
        ValueError: purpose 가 유효하지 않은 경우
    """
    ensure_purpose(purpose)

    program = Path(program_path).resolve()
    if not program.is_file():
        raise FileNotFoundError(f"프로그램을 찾을 수 없습니다: {program}")

    config = load_config(program.parent, required=True)
    study_root = Path(config['root'])
    run_id = make_run_id()
    run_dir = prepare_run_dir(study_root, run_id)

    meta = read_program_meta(program)
    env_snapshot, env_sha = load_env_snapshot(study_root)

    return {
        'config': config,
        'study_root': study_root,
        'study_id': config.get('study_id'),
        'blinded': config.get('blinded'),
        'run_id': run_id,
        'run_dir': run_dir,
        'purpose': purpose,
        'allow_no_assertions': bool(allow_no_assertions),
        'meta': meta,
        'output_snapshot': snapshot_output_dirs(study_root),
        'sap_reference': meta.get('sap_ref'),
        'env_snapshot': env_snapshot,
        'env_snapshot_sha256': env_sha,
        'program': {
            'path': relative_to_root(program, study_root),
            'abs_path': str(program),
            'sha256': sha256_file(program),
            'lines': len(program.read_text(encoding='utf-8', errors='replace').splitlines()),
        },
    }


def run_subprocess(cmd, cwd, run_dir, env=None, timeout=None):
    """
    외부 프로세스를 실행하고 stdout/stderr 를 파일로 캡처한다

    Args:
        cmd: 실행할 명령 리스트
        cwd: 작업 디렉터리
        run_dir: run 디렉터리 Path (stdout.txt, stderr.txt 기록 위치)
        env: 환경변수 딕셔너리 (None 이면 현재 환경 상속)
        timeout: 타임아웃 (초)

    Returns:
        (exit_code, stdout bytes, stderr bytes)
    """
    merged_env = dict(os.environ)
    if env:
        merged_env.update({k: str(v) for k, v in env.items()})

    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            cwd=str(cwd),
            env=merged_env,
            timeout=timeout,
            check=False,
        )
        stdout, stderr = completed.stdout or b'', completed.stderr or b''
        exit_code = completed.returncode
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or b''
        stderr = (exc.stderr or b'') + f"\n[gxpllm] 타임아웃 {timeout}초 초과로 중단".encode('utf-8')
        exit_code = 124
    except OSError as exc:
        stdout = b''
        stderr = f"[gxpllm] 프로세스 실행 실패: {exc}".encode('utf-8')
        exit_code = 127

    (Path(run_dir) / 'stdout.txt').write_bytes(stdout)
    (Path(run_dir) / 'stderr.txt').write_bytes(stderr)

    return exit_code, stdout, stderr


def finalize(context, log_findings=None):
    """
    실행 후 처리를 수행한다

    assertion 수집, 입출력 검증, manifest 작성, 감사 로그 append 를 한다

    Args:
        context: preflight 이후 runner 가 채운 컨텍스트
        log_findings: 로그 스캔 결과 리스트 (없으면 빈 리스트)

    Returns:
        (result 문자열, manifest 딕셔너리)
    """
    study_root = context['study_root']
    run_dir = context['run_dir']
    log_findings = log_findings or []

    # assertion 수집
    assertions, passed, failed = collect_assertions(run_dir)
    assertions_path = write_assertions_json(
        run_dir, context['run_id'], context['language'], assertions, passed, failed
    )

    # 입출력 검증
    inputs, missing_inputs = describe_inputs(context['meta'].get('inputs', []), study_root)
    outputs, missing_outputs = verify_outputs(context['meta'].get('outputs', []), study_root)

    # 선언되지 않은 산출물 탐지 (경계 우회 탐지)
    undeclared = detect_undeclared_outputs(
        context.get('output_snapshot') or {},
        study_root,
        context['meta'].get('outputs', []),
    )

    # 판정
    allow_no_assertions = bool(context.get('allow_no_assertions'))
    result, reasons = decide_result(
        context['purpose'], context['exit_code'], log_findings,
        failed, missing_inputs, missing_outputs,
        undeclared_outputs=undeclared,
        language=context.get('language'),
        assertion_total=len(assertions),
        allow_no_assertions=allow_no_assertions,
    )

    # 로그 스캔 요약
    severity_counts = {}
    for finding in log_findings:
        severity = finding.get('severity', 'UNKNOWN')
        severity_counts[severity] = severity_counts.get(severity, 0) + 1

    context.update({
        'inputs': inputs,
        'outputs': outputs,
        'missing_inputs': missing_inputs,
        'missing_outputs': missing_outputs,
        'undeclared_outputs': undeclared,
        'assertions_summary': {
            'path': relative_to_root(assertions_path, study_root),
            'total': len(assertions),
            'passed': passed,
            'failed': failed,
            'no_assertions_allowed': allow_no_assertions,
        },
        'log_scan': {
            'counts': severity_counts,
            'findings': log_findings[:200],
            'truncated': len(log_findings) > 200,
        },
        'environment': {
            'env_snapshot_sha256': context.get('env_snapshot_sha256'),
            'sas_version': (context.get('env_snapshot') or {}).get('sas', {}).get('version'),
            'python_version': (context.get('env_snapshot') or {}).get('python', {}).get('version'),
            'r_version': (context.get('env_snapshot') or {}).get('r', {}).get('version'),
            'os': (context.get('env_snapshot') or {}).get('os'),
            **context.get('environment_extra', {}),
        },
        'result': result,
        'failure_reasons': reasons,
    })

    manifest = build_manifest(context)
    manifest_path, manifest_sha = write_manifest(run_dir, manifest)

    append_audit(study_root, {
        'event': 'run_finished',
        'run_id': context['run_id'],
        'language': context['language'],
        'purpose': context['purpose'],
        'program': context['program']['path'],
        'program_sha256': context['program']['sha256'],
        'exit_code': context['exit_code'],
        'result': result,
        'assertions_total': len(assertions),
        'assertions_failed': failed,
        'no_assertions_allowed': allow_no_assertions,
        'log_error_count': severity_counts.get('ERROR', 0),
        'log_warning_count': severity_counts.get('WARNING', 0),
        'log_critical_count': severity_counts.get('CRITICAL', 0),
        'undeclared_output_count': len(undeclared),
        'manifest_sha256': manifest_sha,
        'manifest_path': relative_to_root(manifest_path, study_root),
    })

    return result, manifest


def announce_start(context):
    """
    run 시작을 감사 로그에 기록한다

    Args:
        context: preflight 결과 컨텍스트
    """
    append_audit(context['study_root'], {
        'event': 'run_started',
        'run_id': context['run_id'],
        'language': context['language'],
        'purpose': context['purpose'],
        'program': context['program']['path'],
        'program_sha256': context['program']['sha256'],
        'sap_reference': context.get('sap_reference'),
        'blinded': context.get('blinded'),
    })


def print_summary(context, result):
    """
    실행 결과 요약을 출력한다

    Args:
        context: 완료된 컨텍스트
        result: PASSED / FAILED
    """
    print(f"\n{'=' * 80}")
    print(f"{result} — run_id: {context['run_id']}")
    print("=" * 80)

    summary = context['assertions_summary']
    print(f"  종료 코드   : {context['exit_code']}")
    print(f"  소요 시간   : {context['duration_sec']:,.1f}초")
    print(f"  assertion   : 총 {summary['total']:,}건 — "
          f"{summary['passed']:,}건 통과 / {summary['failed']:,}건 실패")
    if summary['total'] == 0 and summary.get('no_assertions_allowed'):
        print(f"                --allow-no-assertions 로 0건을 허용했습니다 "
              f"(감사 로그에 기록됨)")

    counts = context['log_scan']['counts']
    if counts:
        parts = [f"{k} {v:,}건" for k, v in sorted(counts.items())]
        print(f"  로그 스캔   : {', '.join(parts)}")

    undeclared = context.get('undeclared_outputs') or []
    if undeclared:
        print(f"\n  선언되지 않은 산출물 {len(undeclared):,}건:")
        for item in undeclared[:10]:
            print(f"    {item.get('path')}  ({item.get('classification')})")
        print(f"\n    프로그램이 GXPLLM-META 에 없는 파일을 만들었습니다.")
        print(f"    감사 증적이 끊기고, output/tables 라면 경계 우회가 될 수 있습니다.")

    if context['failure_reasons']:
        print(f"\n  실패 사유:")
        for reason in context['failure_reasons']:
            print(f"    - {reason}")

    print(f"\n  산출물      : logs/runs/{context['run_id']}/")
