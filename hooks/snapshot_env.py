"""
실행 환경 스냅샷 hook (SessionStart)

세션 시작 시 분석 환경을 한 번 캡처해 logs/env_snapshot.json 에 기록한다.
매 run 마다 다시 계산하지 않고 manifest 에서 해시로만 참조한다.

캡처 항목
- SAS 버전 및 설치 경로
- Python 버전, uv.lock 해시
- R 버전, renv.lock 해시
- OS, 사용자, 호스트명

정책
- 절대 차단하지 않는다. 항상 exit 0
- 외부 프로세스 호출은 짧은 타임아웃을 건다 (세션 시작을 지연시키지 않는다)
"""

from _bootstrap import read_hook_payload  # sys.path 설정 + 견고한 입력 파서

import json
import platform
import subprocess
import sys
import os
from pathlib import Path

# ============================================================================
# 설정 영역 - 여기서 수정하세요
# ============================================================================

SNAPSHOT_RELATIVE_PATH = 'logs/env_snapshot.json'
SUBPROCESS_TIMEOUT_SEC = 15
EXIT_OK = 0

LOCK_FILES = {
    'uv_lock': '.gxpllm/env/uv.lock',
    'renv_lock': '.gxpllm/env/renv.lock',
}


# ============================================================================
# 메인 로직
# ============================================================================

def run_capture(cmd, timeout=SUBPROCESS_TIMEOUT_SEC):
    """
    외부 명령을 실행해 stdout 을 돌려준다

    실패해도 예외를 던지지 않는다

    Args:
        cmd: 실행할 명령 리스트
        timeout: 타임아웃 (초)

    Returns:
        stdout 문자열. 실패 시 None
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        output = (result.stdout or b'') + (result.stderr or b'')
        return output.decode('utf-8', errors='replace').strip()
    except Exception:
        return None


def detect_sas_version(config):
    """
    SAS 버전을 확인한다

    Args:
        config: config 딕셔너리

    Returns:
        (버전 문자열 또는 None, 실행 파일 경로 또는 None)
    """
    sas_exe = config.get('sas_exe')
    if not sas_exe or not Path(sas_exe).is_file():
        return None, sas_exe

    # SAS 9.4 Windows: -version 은 버전 문자열을 출력하고 즉시 종료한다
    output = run_capture([sas_exe, '-version', '-nosplash', '-noterminal'])
    if output:
        for line in output.splitlines():
            if 'SAS' in line and any(ch.isdigit() for ch in line):
                return line.strip()[:200], sas_exe

    return 'unknown (실행 파일은 존재)', sas_exe


def detect_r_version():
    """
    R 버전을 확인한다

    Returns:
        버전 문자열 또는 None
    """
    output = run_capture(['Rscript', '--vanilla', '-e', 'cat(R.version.string)'])
    if output:
        return output.splitlines()[0].strip()[:200]
    return None


def hash_lock_files(study_root):
    """
    환경 잠금 파일들의 해시를 계산한다

    Args:
        study_root: study 루트 경로

    Returns:
        {키: 해시 또는 None} 딕셔너리
    """
    from gxpllm.core import sha256_file

    result = {}
    for key, relative in LOCK_FILES.items():
        path = Path(study_root) / relative
        result[key] = sha256_file(path) if path.is_file() else None
    return result


def build_snapshot(study_root, config):
    """
    환경 스냅샷을 구성한다

    Args:
        study_root: study 루트 경로
        config: config 딕셔너리

    Returns:
        스냅샷 딕셔너리
    """
    from gxpllm.core import now_iso, current_user, current_hostname

    sas_version, sas_exe = detect_sas_version(config)

    snapshot = {
        'captured_at': now_iso(),
        'study_id': config.get('study_id'),
        'user': current_user(),
        'hostname': current_hostname(),
        'os': platform.platform(),
        'machine': platform.machine(),
        'sas': {
            'version': sas_version,
            'exe': sas_exe,
        },
        'python': {
            'version': platform.python_version(),
            'implementation': platform.python_implementation(),
            'executable': sys.executable,
        },
        'r': {
            'version': detect_r_version(),
        },
        'locks': hash_lock_files(study_root),
        'blinded': config.get('blinded'),
    }
    return snapshot


def main():
    """메인 함수"""
    try:
        payload = read_hook_payload()
    except Exception:
        payload = {}

    try:
        from gxpllm.core import find_study_root, append_audit, sha256_text, canonical_json

        cwd = payload.get('cwd') or os.getcwd()
        study_root, config = find_study_root(cwd)
        if study_root is None:
            sys.exit(EXIT_OK)

        snapshot = build_snapshot(study_root, config)
        snapshot_sha = sha256_text(canonical_json(snapshot))
        snapshot['snapshot_sha256'] = snapshot_sha

        snapshot_path = Path(study_root) / SNAPSHOT_RELATIVE_PATH
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        with open(snapshot_path, 'w', encoding='utf-8') as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)

        append_audit(study_root, {
            'event': 'session_started',
            'session_id': payload.get('session_id'),
            'source': payload.get('source'),
            'env_snapshot_sha256': snapshot_sha,
            'blinded': config.get('blinded'),
        })

    except Exception:
        pass

    sys.exit(EXIT_OK)


if __name__ == "__main__":
    main()
