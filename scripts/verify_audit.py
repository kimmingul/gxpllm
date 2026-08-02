"""
감사 로그 무결성 검증 도구

audit/audit.jsonl 의 해시 체인을 검증하고, run 산출물과의 정합성을 확인한다.

검증 항목
- 해시 체인: seq 연속성, prev_hash 연결, entry_hash 재계산 일치
- manifest 존재: run_finished 항목이 가리키는 manifest.json 이 실제로 있는가
- manifest 해시: 기록된 SHA-256 과 현재 파일이 일치하는가
- 로그 파일 해시: manifest 가 기록한 실행 로그가 변조되지 않았는가

주기적으로(예: 월 1회) 실행해 결과를 보관하면 감사 대응 증적이 된다.

사용:
    python scripts/verify_audit.py --study D:\\clinical\\ABC-301
    python scripts/verify_audit.py --study . --json
"""

import _common  # noqa: F401  (sys.path 설정)

import argparse
import json
import sys
from pathlib import Path

from gxpllm.core import (
    load_config, audit_path_for, verify_audit_chain,
    sha256_file, sha256_text, canonical_json,
    audit_key_path, load_or_create_audit_key,
)

# ============================================================================
# 설정 영역 - 여기서 수정하세요
# ============================================================================

RUNS_RELATIVE_PATH = 'logs/runs'


# ============================================================================
# 메인 로직
# ============================================================================

def verify_manifests(study_root, audit_path):
    """
    감사 로그가 가리키는 manifest 들의 존재와 해시를 검증한다

    Args:
        study_root: study 루트 Path
        audit_path: audit.jsonl 경로

    Returns:
        (검사한 run 수, 문제 리스트)
    """
    problems = []
    checked = 0

    if not Path(audit_path).is_file():
        return 0, []

    with open(audit_path, 'r', encoding='utf-8') as f:
        for line_no, raw in enumerate(f, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except ValueError:
                continue

            if entry.get('event') != 'run_finished':
                continue

            checked += 1
            relative = entry.get('manifest_path')
            recorded_sha = entry.get('manifest_sha256')

            if not relative:
                problems.append(f"{line_no}행: run_finished 에 manifest_path 없음")
                continue

            manifest_file = Path(study_root) / relative
            if not manifest_file.is_file():
                problems.append(
                    f"{line_no}행: manifest 파일 없음 ({relative}) "
                    f"— run {entry.get('run_id')}"
                )
                continue

            try:
                with open(manifest_file, 'r', encoding='utf-8') as mf:
                    manifest = json.load(mf)
            except (OSError, ValueError) as exc:
                problems.append(f"{line_no}행: manifest 읽기 실패 ({relative}): {exc}")
                continue

            # manifest 본문 무결성 검증
            #
            # runner 는 canonical JSON 의 SHA-256 을 감사 로그에 기록한다.
            # 파일 바이트 해시가 아니므로 같은 방식으로 재계산해 대조해야 한다.
            # 이것을 하지 않으면 manifest 의 result / assertions 를 고쳐도
            # 감사 검증을 통과한다.
            if recorded_sha:
                recomputed = sha256_text(canonical_json(manifest))
                if recomputed != recorded_sha:
                    problems.append(
                        f"run {manifest.get('run_id')}: manifest 내용 해시 불일치 "
                        f"({relative}) — manifest 가 변조되었습니다"
                    )
            else:
                problems.append(
                    f"{line_no}행: manifest_sha256 이 기록되지 않았습니다 "
                    f"— run {entry.get('run_id')}"
                )

            logs = manifest.get('logs') or {}
            for log_key in ('execution_log', 'execution_lst'):
                log_relative = logs.get(log_key)
                if not log_relative:
                    continue
                log_file = Path(study_root) / log_relative
                if not log_file.is_file():
                    problems.append(
                        f"run {manifest.get('run_id')}: 실행 로그 없음 ({log_relative})"
                    )
                    continue

            recorded_log_sha = logs.get('log_sha256')
            log_relative = logs.get('execution_log')
            if recorded_log_sha and log_relative:
                log_file = Path(study_root) / log_relative
                if log_file.is_file():
                    actual = sha256_file(log_file)
                    if actual != recorded_log_sha:
                        problems.append(
                            f"run {manifest.get('run_id')}: 실행 로그 해시 불일치 "
                            f"({log_relative}) — 로그가 변조되었습니다"
                        )

    return checked, problems


def verify_orphan_runs(study_root, audit_path):
    """
    감사 로그에 없는 run 디렉터리를 찾는다

    runner 를 거치지 않고 만들어진 산출물이나, 감사 항목이 삭제된 흔적을 탐지한다

    Args:
        study_root: study 루트 Path
        audit_path: audit.jsonl 경로

    Returns:
        고아 run_id 리스트
    """
    runs_dir = Path(study_root) / RUNS_RELATIVE_PATH
    if not runs_dir.is_dir():
        return []

    on_disk = {p.name for p in runs_dir.iterdir() if p.is_dir()}
    in_audit = set()

    if Path(audit_path).is_file():
        with open(audit_path, 'r', encoding='utf-8') as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue
                if entry.get('run_id'):
                    in_audit.add(entry['run_id'])

    return sorted(on_disk - in_audit)


def main():
    """메인 함수"""
    parser = argparse.ArgumentParser(description='감사 로그 무결성을 검증합니다')
    parser.add_argument('--study', default='.', help='study 루트 경로')
    parser.add_argument('--json', action='store_true', help='결과를 JSON 으로 출력')
    args = parser.parse_args()

    config = load_config(args.study, required=False)
    if not config:
        print(f"오류: .gxpllm/config.json 을 찾을 수 없습니다 ({args.study})")
        sys.exit(2)

    study_root = Path(config['root'])
    audit_path = audit_path_for(study_root)

    if not args.json:
        print("=" * 80)
        print("감사 로그 무결성 검증")
        print("=" * 80)
        print(f"  study    : {config.get('study_id')}")
        print(f"  경로     : {audit_path}")
        print(f"  키 파일  : {audit_key_path()}")

    # --- 1/3 해시 체인 ------------------------------------------------------
    if not args.json:
        print(f"\n[1/3] 해시 체인 검증...")

    key = load_or_create_audit_key()
    chain_ok, chain_problems, entry_count = verify_audit_chain(audit_path)

    if not args.json:
        print(f"  항목 수  : {entry_count:,}건")
        print(f"  서명     : {'HMAC-SHA256' if key else 'SHA-256 (키 없음)'}")
        if chain_ok:
            print(f"  결과     : 체인 정상")
        else:
            print(f"  결과     : 문제 {len(chain_problems):,}건")
            for problem in chain_problems[:20]:
                print(f"    - {problem}")

    # --- 2/3 manifest 정합성 ------------------------------------------------
    if not args.json:
        print(f"\n[2/3] manifest 및 실행 로그 정합성...")

    run_count, manifest_problems = verify_manifests(study_root, audit_path)

    if not args.json:
        print(f"  검사한 run: {run_count:,}건")
        if manifest_problems:
            print(f"  문제       : {len(manifest_problems):,}건")
            for problem in manifest_problems[:20]:
                print(f"    - {problem}")
        else:
            print(f"  결과       : 정상")

    # --- 3/3 고아 run -------------------------------------------------------
    if not args.json:
        print(f"\n[3/3] 감사 기록 없는 run 디렉터리 확인...")

    orphans = verify_orphan_runs(study_root, audit_path)

    if not args.json:
        if orphans:
            print(f"  발견     : {len(orphans):,}건 — runner 를 거치지 않았거나 감사 항목이 삭제됨")
            for run_id in orphans[:20]:
                print(f"    - {run_id}")
        else:
            print(f"  결과     : 정상")

    # --- 결과 ---------------------------------------------------------------
    all_ok = chain_ok and not manifest_problems and not orphans

    result = {
        'study_id': config.get('study_id'),
        'audit_path': str(audit_path),
        'entry_count': entry_count,
        'run_count': run_count,
        'hash_alg': 'hmac-sha256' if key else 'sha256',
        'chain_ok': chain_ok,
        'chain_problems': chain_problems,
        'manifest_problems': manifest_problems,
        'orphan_runs': orphans,
        'ok': all_ok,
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"\n{'=' * 80}")
        print(f"{'검증 통과' if all_ok else '검증 실패'}")
        print("=" * 80)
        if not all_ok:
            print("\n  감사 증적에 문제가 있습니다. 조치 전까지 제출 경로 산출물을 사용하지 마십시오.")
        print("\n  참고: 해시 체인은 변조를 '탐지'할 뿐 '방지'하지 않습니다.")
        print("        audit/ 와 logs/ 를 백업되는 사내 공유 드라이브에 정기 동기화하십시오.")

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
