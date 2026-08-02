"""
감사 로그 해시 체인 검증 테스트

변조 탐지가 실제로 작동하는지 확인한다.
- 정상 체인은 통과
- 내용 변조, 항목 삭제, 항목 삽입, 순서 변경은 탐지
- 전체 재작성 위조는 HMAC 키가 있으면 탐지 (키가 study 트리 밖에 있으므로)

grok 검토 H5 대응: 키 없는 순수 SHA-256 체인은 전체 재작성으로 위조 가능했다.

실행:
    python tests/test_audit.py
"""

import json
import sys
import tempfile
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from gxpllm.core import (  # noqa: E402
    append_audit, verify_audit_chain, audit_path_for,
    compute_entry_hash, canonical_json, GENESIS_HASH,
)

# ============================================================================
# 설정 영역 - 여기서 수정하세요
# ============================================================================

SAMPLE_EVENTS = [
    {'event': 'session_started', 'session_id': 's1'},
    {'event': 'run_started', 'run_id': 'r1', 'language': 'sas'},
    {'event': 'run_finished', 'run_id': 'r1', 'exit_code': 0, 'result': 'PASSED'},
    {'event': 'access_blocked', 'tool': 'Read', 'target': 'data/raw/adsl.sas7bdat'},
    {'event': 'run_started', 'run_id': 'r2', 'language': 'python'},
]


# ============================================================================
# 메인 로직
# ============================================================================

def build_sample_audit(study_root):
    """
    테스트용 감사 로그를 만든다

    Args:
        study_root: study 루트 Path

    Returns:
        audit.jsonl 경로
    """
    for event in SAMPLE_EVENTS:
        append_audit(study_root, dict(event))
    return audit_path_for(study_root)


def read_entries(audit_path):
    """
    감사 로그 항목을 모두 읽는다

    Args:
        audit_path: audit.jsonl 경로

    Returns:
        항목 딕셔너리 리스트
    """
    entries = []
    with open(audit_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def write_entries(audit_path, entries):
    """
    감사 로그 항목을 덮어쓴다

    Args:
        audit_path: audit.jsonl 경로
        entries: 기록할 항목 리스트
    """
    with open(audit_path, 'w', encoding='utf-8', newline='\n') as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')


def case_normal(study_root):
    """정상 체인은 통과해야 한다"""
    audit_path = build_sample_audit(study_root)
    ok, problems, count = verify_audit_chain(audit_path)
    return ok and count == len(SAMPLE_EVENTS), f"ok={ok}, count={count}, problems={problems[:2]}"


def case_tamper_content(study_root):
    """항목 내용을 바꾸면 탐지되어야 한다"""
    audit_path = build_sample_audit(study_root)
    entries = read_entries(audit_path)
    entries[2]['result'] = 'FAILED'          # PASSED -> FAILED 로 위조
    write_entries(audit_path, entries)
    ok, problems, _ = verify_audit_chain(audit_path)
    return (not ok), f"ok={ok}, problems={problems[:2]}"


def case_delete_entry(study_root):
    """중간 항목을 삭제하면 탐지되어야 한다"""
    audit_path = build_sample_audit(study_root)
    entries = read_entries(audit_path)
    del entries[3]                            # access_blocked 기록 은폐 시도
    write_entries(audit_path, entries)
    ok, problems, _ = verify_audit_chain(audit_path)
    return (not ok), f"ok={ok}, problems={problems[:2]}"


def case_insert_entry(study_root):
    """항목을 삽입하면 탐지되어야 한다"""
    audit_path = build_sample_audit(study_root)
    entries = read_entries(audit_path)
    forged = dict(entries[1])
    forged['run_id'] = 'r_forged'
    entries.insert(2, forged)
    write_entries(audit_path, entries)
    ok, problems, _ = verify_audit_chain(audit_path)
    return (not ok), f"ok={ok}, problems={problems[:2]}"


def case_reorder(study_root):
    """항목 순서를 바꾸면 탐지되어야 한다"""
    audit_path = build_sample_audit(study_root)
    entries = read_entries(audit_path)
    entries[1], entries[2] = entries[2], entries[1]
    write_entries(audit_path, entries)
    ok, problems, _ = verify_audit_chain(audit_path)
    return (not ok), f"ok={ok}, problems={problems[:2]}"


def case_full_rewrite_without_key(study_root):
    """
    키 없이 전체 재작성 위조를 시도하면 탐지되어야 한다

    grok 검토 H5: 순수 SHA-256 체인은 공격자가 genesis 부터 다시 계산해
    검증을 통과시킬 수 있었다. HMAC 키가 study 트리 밖에 있으므로
    키를 모르는 상태의 재작성은 실패해야 한다.
    """
    audit_path = build_sample_audit(study_root)

    # 공격자가 키를 모른다고 가정하고 순수 SHA-256 으로 체인을 새로 만든다
    forged_entries = []
    prev_hash = GENESIS_HASH
    for index, event in enumerate(SAMPLE_EVENTS, start=1):
        entry = dict(event)
        entry['ts'] = '2026-01-01T00:00:00+09:00'
        entry['user'] = 'attacker'
        entry['hostname'] = 'evil-pc'
        entry['seq'] = index
        entry['prev_hash'] = prev_hash
        entry['hash_alg'] = 'hmac-sha256'     # HMAC 인 척 위장
        payload = {k: v for k, v in entry.items() if k != 'entry_hash'}
        import hashlib
        entry['entry_hash'] = hashlib.sha256(
            canonical_json(payload).encode('utf-8')
        ).hexdigest()
        prev_hash = entry['entry_hash']
        forged_entries.append(entry)

    write_entries(audit_path, forged_entries)
    ok, problems, _ = verify_audit_chain(audit_path)
    return (not ok), f"ok={ok}, problems={problems[:2]}"


def case_hash_alg_downgrade(study_root):
    """
    hash_alg 다운그레이드 위조를 탐지해야 한다

    2차 검토 C-NEW-4: 항목이 자기 hash_alg 를 고르게 두면, 공격자가 키를 몰라도
    전체를 sha256 으로 갈아끼운 뒤 재서명해 검증을 통과시킬 수 있었다.
    키가 존재하면 모든 항목이 hmac-sha256 이어야 한다.
    """
    import hashlib

    audit_path = build_sample_audit(study_root)

    forged_entries = []
    prev_hash = GENESIS_HASH
    for index, event in enumerate(SAMPLE_EVENTS, start=1):
        entry = dict(event)
        entry['ts'] = '2026-01-01T00:00:00+09:00'
        entry['user'] = 'attacker'
        entry['hostname'] = 'evil-pc'
        entry['seq'] = index
        entry['prev_hash'] = prev_hash
        entry['hash_alg'] = 'sha256'          # 다운그레이드
        payload = {k: v for k, v in entry.items() if k != 'entry_hash'}
        entry['entry_hash'] = hashlib.sha256(
            canonical_json(payload).encode('utf-8')
        ).hexdigest()
        prev_hash = entry['entry_hash']
        forged_entries.append(entry)

    write_entries(audit_path, forged_entries)
    ok, problems, _ = verify_audit_chain(audit_path)
    return (not ok), f"ok={ok}, problems={problems[:2]}"


def case_hash_alg_removed(study_root):
    """hash_alg 필드를 아예 지운 재작성도 탐지해야 한다"""
    import hashlib

    audit_path = build_sample_audit(study_root)

    forged_entries = []
    prev_hash = GENESIS_HASH
    for index, event in enumerate(SAMPLE_EVENTS, start=1):
        entry = dict(event)
        entry['ts'] = '2026-01-01T00:00:00+09:00'
        entry['user'] = 'attacker'
        entry['hostname'] = 'evil-pc'
        entry['seq'] = index
        entry['prev_hash'] = prev_hash
        payload = {k: v for k, v in entry.items() if k != 'entry_hash'}
        entry['entry_hash'] = hashlib.sha256(
            canonical_json(payload).encode('utf-8')
        ).hexdigest()
        prev_hash = entry['entry_hash']
        forged_entries.append(entry)

    write_entries(audit_path, forged_entries)
    ok, problems, _ = verify_audit_chain(audit_path)
    return (not ok), f"ok={ok}, problems={problems[:2]}"


def case_truncate(study_root):
    """
    뒷부분을 잘라내는 것은 탐지되지 않는다 (알려진 한계)

    체인은 선형이므로 마지막 N 개를 지우면 남은 부분은 여전히 유효하다.
    이 한계는 문서에 명시하고, 외부 백업으로 보완해야 한다.
    """
    audit_path = build_sample_audit(study_root)
    entries = read_entries(audit_path)
    write_entries(audit_path, entries[:2])
    ok, _, count = verify_audit_chain(audit_path)
    # 탐지되지 않는 것이 정상 (알려진 한계 확인)
    return ok and count == 2, f"ok={ok}, count={count} (한계 확인: 후미 절단은 미탐지)"


TEST_CASES = [
    ('정상 체인 통과',                      case_normal),
    ('내용 변조 탐지',                      case_tamper_content),
    ('항목 삭제 탐지',                      case_delete_entry),
    ('항목 삽입 탐지',                      case_insert_entry),
    ('순서 변경 탐지',                      case_reorder),
    ('키 없는 전체 재작성 위조 탐지 (H5)',  case_full_rewrite_without_key),
    ('hash_alg 다운그레이드 탐지 (C-NEW-4)', case_hash_alg_downgrade),
    ('hash_alg 제거 재작성 탐지',           case_hash_alg_removed),
    ('후미 절단은 미탐지 (알려진 한계)',    case_truncate),
]


def main():
    """메인 함수"""
    print("=" * 80)
    print("감사 로그 해시 체인 검증")
    print("=" * 80)

    passed, failed = 0, []

    for index, (description, test_fn) in enumerate(TEST_CASES, start=1):
        print(f"\n[{index}/{len(TEST_CASES)}] {description}")
        with tempfile.TemporaryDirectory() as tmpdir:
            study_root = Path(tmpdir) / 'STUDY'
            study_root.mkdir(parents=True, exist_ok=True)
            try:
                ok, detail = test_fn(study_root)
            except Exception as exc:
                ok, detail = False, f"예외: {type(exc).__name__}: {exc}"

            if ok:
                passed += 1
                print(f"  OK   {detail}")
            else:
                failed.append(f"{description}: {detail}")
                print(f"  FAIL {detail}")

    print(f"\n{'=' * 80}")
    print(f"결과: {passed:,}건 통과 / {len(failed):,}건 실패")
    if failed:
        print("\n실패 목록:")
        for item in failed:
            print(f"  - {item}")
    print("=" * 80)

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
