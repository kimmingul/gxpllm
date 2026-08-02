"""
Independent Programming 대조 도구

primary 프로그램과 QC 프로그램의 수치 출력을 결정론적으로 비교한다.

중요: 대조는 LLM 이 아니라 이 스크립트가 한다.
      LLM 에게 "두 결과가 같은가?"를 묻지 않는다.
      불일치가 나오면 그것이 double programming 의 성과이며,
      어느 쪽이 맞는지는 사람이 판정한다.

입력 형식
  두 파일 모두 JSON 이어야 하며, 다음 중 하나의 구조를 갖는다.
  - 평면 딕셔너리: {"n_safety": 241, "n_teae": 187}
  - 중첩 딕셔너리: {"overall": {"n": 241}, "arm_a": {"n": 120}}
  - 레코드 리스트: [{"key": "SOC1", "n": 12, "pct": 5.0}, ...]

사용:
    python scripts/compare_outputs.py --primary output/tables/t.json --qc output/tables/qc_t.json
    python scripts/compare_outputs.py --primary p.json --qc q.json --tolerance 0.01 --json
"""

import _common  # noqa: F401  (sys.path 설정)

import argparse
import json
import sys
from pathlib import Path

from gxpllm.core import load_config, append_audit, sha256_file, relative_to_root

# ============================================================================
# 설정 영역 - 여기서 수정하세요
# ============================================================================

DEFAULT_TOLERANCE = 0.0
RECORD_KEY_CANDIDATES = ('key', 'id', 'label', 'term', 'pt', 'soc', 'parameter', 'row')
MAX_REPORTED_DIFFS = 100


# ============================================================================
# 메인 로직
# ============================================================================

def load_json(path):
    """
    JSON 파일을 읽는다

    Args:
        path: 파일 경로

    Returns:
        파싱된 객체

    Raises:
        SystemExit: 읽기 실패 시
    """
    p = Path(path)
    if not p.is_file():
        print(f"오류: 파일을 찾을 수 없습니다: {path}")
        sys.exit(2)

    try:
        with open(p, 'r', encoding='utf-8') as f:
            return json.load(f)
    except ValueError as exc:
        print(f"오류: JSON 파싱 실패 ({path}): {exc}")
        sys.exit(2)


def detect_record_key(records):
    """
    레코드 리스트에서 식별자로 쓸 필드를 찾는다

    Args:
        records: 딕셔너리 리스트

    Returns:
        키 필드명. 찾지 못하면 None
    """
    if not records or not isinstance(records[0], dict):
        return None

    for candidate in RECORD_KEY_CANDIDATES:
        if candidate in records[0]:
            return candidate

    # 첫 문자열 필드를 키로 쓴다
    for name, value in records[0].items():
        if isinstance(value, str):
            return name

    return None


def flatten(obj, prefix=''):
    """
    중첩 구조를 평면 딕셔너리로 만든다

    Args:
        obj: 대상 객체
        prefix: 키 접두사

    Returns:
        {경로: 값} 딕셔너리
    """
    flat = {}

    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            flat.update(flatten(value, path))
        return flat

    if isinstance(obj, list):
        record_key = detect_record_key(obj)
        for index, item in enumerate(obj):
            if record_key and isinstance(item, dict) and record_key in item:
                path = f"{prefix}[{item[record_key]}]" if prefix else f"[{item[record_key]}]"
            else:
                path = f"{prefix}[{index}]" if prefix else f"[{index}]"
            flat.update(flatten(item, path))
        return flat

    flat[prefix] = obj
    return flat


def values_match(a, b, tolerance):
    """
    두 값이 일치하는지 판정한다

    Args:
        a: 첫 번째 값
        b: 두 번째 값
        tolerance: 숫자 허용 오차

    Returns:
        일치하면 True
    """
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False

    if isinstance(a, bool) or isinstance(b, bool):
        return a == b

    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= tolerance

    return str(a).strip() == str(b).strip()


def compare(primary, qc, tolerance):
    """
    두 결과를 비교한다

    Args:
        primary: primary 결과 객체
        qc: QC 결과 객체
        tolerance: 숫자 허용 오차

    Returns:
        (일치 수, 불일치 리스트, primary 전용 키, QC 전용 키)
    """
    flat_primary = flatten(primary)
    flat_qc = flatten(qc)

    common = sorted(set(flat_primary) & set(flat_qc))
    only_primary = sorted(set(flat_primary) - set(flat_qc))
    only_qc = sorted(set(flat_qc) - set(flat_primary))

    matched = 0
    diffs = []

    for key in common:
        a, b = flat_primary[key], flat_qc[key]
        if values_match(a, b, tolerance):
            matched += 1
        else:
            diff = None
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                diff = b - a
            diffs.append({'key': key, 'primary': a, 'qc': b, 'diff': diff})

    return matched, diffs, only_primary, only_qc


def main():
    """메인 함수"""
    parser = argparse.ArgumentParser(
        description='primary 와 QC 프로그램의 수치 출력을 대조합니다'
    )
    parser.add_argument('--primary', required=True, help='primary 결과 JSON 경로')
    parser.add_argument('--qc', required=True, help='QC 결과 JSON 경로')
    parser.add_argument('--tolerance', type=float, default=DEFAULT_TOLERANCE,
                        help=f'숫자 허용 오차 (기본 {DEFAULT_TOLERANCE})')
    parser.add_argument('--json', action='store_true', help='결과를 JSON 으로 출력')
    args = parser.parse_args()

    if not args.json:
        print("=" * 80)
        print("Independent Programming 대조")
        print("=" * 80)
        print(f"  primary  : {args.primary}")
        print(f"  qc       : {args.qc}")
        print(f"  tolerance: {args.tolerance}")

    primary = load_json(args.primary)
    qc = load_json(args.qc)

    matched, diffs, only_primary, only_qc = compare(primary, qc, args.tolerance)
    total = matched + len(diffs)
    ok = not diffs and not only_primary and not only_qc

    if not args.json:
        print(f"\n[1/3] 공통 항목 비교...")
        print(f"  비교 항목: {total:,}건")
        print(f"  일치     : {matched:,}건")
        print(f"  불일치   : {len(diffs):,}건")

        if diffs:
            print(f"\n  불일치 목록:")
            for item in diffs[:MAX_REPORTED_DIFFS]:
                diff_text = f" (차이 {item['diff']:+,})" if item['diff'] is not None else ""
                print(f"    {item['key']}")
                print(f"      primary = {item['primary']}")
                print(f"      qc      = {item['qc']}{diff_text}")
            if len(diffs) > MAX_REPORTED_DIFFS:
                print(f"    ... 외 {len(diffs) - MAX_REPORTED_DIFFS:,}건")

        print(f"\n[2/3] 한쪽에만 있는 항목...")
        if only_primary:
            print(f"  primary 에만: {len(only_primary):,}건")
            for key in only_primary[:20]:
                print(f"    {key}")
        if only_qc:
            print(f"  qc 에만     : {len(only_qc):,}건")
            for key in only_qc[:20]:
                print(f"    {key}")
        if not only_primary and not only_qc:
            print(f"  없음")

    # 감사 로그 기록
    if not args.json:
        print(f"\n[3/3] 감사 로그 기록...")

    config = load_config(Path(args.primary).parent, required=False)
    if config:
        study_root = Path(config['root'])
        append_audit(study_root, {
            'event': 'qc_comparison',
            'primary': relative_to_root(args.primary, study_root),
            'qc': relative_to_root(args.qc, study_root),
            'primary_sha256': sha256_file(args.primary),
            'qc_sha256': sha256_file(args.qc),
            'tolerance': args.tolerance,
            'compared': total,
            'matched': matched,
            'mismatched': len(diffs),
            'only_primary': len(only_primary),
            'only_qc': len(only_qc),
            'result': 'MATCH' if ok else 'MISMATCH',
        })
        if not args.json:
            print(f"  기록 완료")
    elif not args.json:
        print(f"  건너뜀 (.gxpllm/config.json 을 찾을 수 없음)")

    result = {
        'primary': args.primary,
        'qc': args.qc,
        'tolerance': args.tolerance,
        'compared': total,
        'matched': matched,
        'mismatched': len(diffs),
        'diffs': diffs[:MAX_REPORTED_DIFFS],
        'only_primary': only_primary,
        'only_qc': only_qc,
        'ok': ok,
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"\n{'=' * 80}")
        print(f"{'일치' if ok else '불일치 발견'}")
        print("=" * 80)
        if not ok:
            print("\n  어느 쪽이 맞는지는 사람이 판정하십시오.")
            print("  LLM 에게 판단시키지 마십시오. 두 구현 모두 같은 모델이 만들었다면")
            print("  같은 오해를 공유할 수 있습니다.")

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
