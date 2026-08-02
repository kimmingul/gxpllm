"""
gxpllm assertion 라이브러리 (Python)

분석 프로그램이 데이터 무결성과 임상 정합성을 검증하고 결과를 기록한다.
결과는 assertions.jsonl 에 한 줄씩 append 되며, runner 가 이를 모아 판정한다.

설계 원칙
- assertion 은 실패해도 예외를 던지지 않는다 (전체 검증 결과를 모으기 위함)
  단, strict=True 이면 즉시 중단한다
- 판정 로직은 runner 의 공통 검증기가 담당한다. 여기서는 관측값 기록에 집중
- SAS(gxpllm_assert.sas) / R(gxpllm_assert.R) 과 동일한 출력 형식을 유지한다

사용:
    from gxpllm_assert import (
        assert_rowcount, assert_join_loss, assert_unique,
        assert_domain, assert_missingness, assert_denominator, reconcile,
    )

    assert_rowcount(adsl, expected_min=1, label='ADSL_LOADED')
    assert_unique(adsl, keys=['USUBJID'], label='ADSL_UNIQUE_SUBJ')
"""

import json
import os
from datetime import datetime
from pathlib import Path

# ============================================================================
# 설정 영역 - 여기서 수정하세요
# ============================================================================

ASSERTIONS_FILENAME = 'assertions.jsonl'
RUN_DIR_ENV = 'GXPLLM_RUN_DIR'

RESULT_PASS = 'PASS'
RESULT_FAIL = 'FAIL'


class AssertionFailure(Exception):
    """strict 모드에서 assertion 실패 시 발생하는 예외"""


# ============================================================================
# 기록 유틸
# ============================================================================

def _run_dir():
    """
    assertion 기록 디렉터리를 돌려준다

    runner 가 GXPLLM_RUN_DIR 환경변수로 전달한다.
    runner 밖에서 실행되면 현재 디렉터리에 기록한다.

    Returns:
        기록 디렉터리 Path
    """
    return Path(os.environ.get(RUN_DIR_ENV) or os.getcwd())


def _emit(record):
    """
    assertion 결과를 assertions.jsonl 에 append 한다

    Args:
        record: 기록할 딕셔너리

    Returns:
        기록된 딕셔너리
    """
    record.setdefault('ts', datetime.now().astimezone().isoformat(timespec='seconds'))
    record.setdefault('language', 'python')

    path = _run_dir() / ASSERTIONS_FILENAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'a', encoding='utf-8', newline='\n') as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + '\n')
    except OSError:
        pass

    return record


def _finish(label, rule, result, message, observed, expected, strict, extra=None):
    """
    assertion 결과를 확정하고 기록한다

    Args:
        label: assertion 식별자
        rule: 규칙 종류
        result: PASS / FAIL
        message: 사람이 읽을 설명
        observed: 관측값
        expected: 기대값 딕셔너리
        strict: True 이면 실패 시 예외를 던진다
        extra: 추가로 기록할 필드

    Returns:
        result == PASS 이면 True

    Raises:
        AssertionFailure: strict=True 이고 실패한 경우
    """
    record = {
        'label': label,
        'rule': rule,
        'result': result,
        'message': message,
        'observed': observed,
        'expected': expected,
    }
    if extra:
        record.update(extra)

    _emit(record)
    print(f"  [{result}] {label}: {message}")

    if result == RESULT_FAIL and strict:
        raise AssertionFailure(f"{label}: {message}")

    return result == RESULT_PASS


def _nrow(df):
    """
    데이터프레임의 행 수를 돌려준다 (pandas / polars 모두 지원)

    Args:
        df: 데이터프레임 또는 len() 가능한 객체

    Returns:
        행 수 정수
    """
    if hasattr(df, 'height'):        # polars
        return int(df.height)
    if hasattr(df, 'shape'):         # pandas
        return int(df.shape[0])
    return len(df)


# ============================================================================
# 데이터 무결성 assertion
# ============================================================================

def assert_rowcount(df, label, expected_min=None, expected_max=None,
                    expected_n=None, strict=False, expected_exact=None):
    """
    데이터셋 행 수가 기대 범위 안인지 검증한다

    Args:
        df: 검사할 데이터프레임
        label: assertion 식별자 (예: 'SAFETY_SET')
        expected_min: 최소 행 수
        expected_max: 최대 행 수
        expected_n: 정확히 일치해야 할 행 수 (SAS/R 과 동일한 이름)
        strict: True 이면 실패 시 예외
        expected_exact: expected_n 의 옛 이름. 하위 호환용

    Returns:
        통과하면 True
    """
    # SAS(%gxpllm_assert_rowcount) 와 R(gxpllm_assert_rowcount) 은 expected_n 을 쓴다.
    # 세 언어의 파라미터 이름이 다르면 로컬 LLM 이 생성한 코드가 언어별로 깨진다.
    if expected_n is None and expected_exact is not None:
        expected_n = expected_exact

    n = _nrow(df)
    expected = {}
    problems = []

    if expected_n is not None:
        expected['exact'] = expected_n
        if n != expected_n:
            problems.append(f"기대 {expected_n:,}행이 아닌 {n:,}행")
    if expected_min is not None:
        expected['min'] = expected_min
        if n < expected_min:
            problems.append(f"{n:,}행 < 최소 {expected_min:,}행")
    if expected_max is not None:
        expected['max'] = expected_max
        if n > expected_max:
            problems.append(f"{n:,}행 > 최대 {expected_max:,}행")

    result = RESULT_FAIL if problems else RESULT_PASS
    message = '; '.join(problems) if problems else f"{n:,}행"

    return _finish(label, 'rowcount', result, message, n, expected, strict)


def assert_rowcount_delta(before, after, label, expected_delta=None,
                          max_loss_rate=None, allow_increase=False, strict=False):
    """
    변환 전후 행 수 변화가 의도한 것인지 검증한다

    임상 데이터에서 필터/병합 후 행 수가 조용히 변하는 것을 잡는다

    Args:
        before: 변환 전 데이터프레임 또는 행 수
        after: 변환 후 데이터프레임 또는 행 수
        label: assertion 식별자
        expected_delta: 기대하는 행 수 변화량 (음수 가능)
        max_loss_rate: 허용 손실률 (0.0 ~ 1.0)
        allow_increase: True 이면 행 증가를 허용
        strict: True 이면 실패 시 예외

    Returns:
        통과하면 True
    """
    n_before = before if isinstance(before, int) else _nrow(before)
    n_after = after if isinstance(after, int) else _nrow(after)
    delta = n_after - n_before
    loss_rate = (n_before - n_after) / n_before if n_before else 0.0

    expected = {}
    problems = []

    if expected_delta is not None:
        expected['delta'] = expected_delta
        if delta != expected_delta:
            problems.append(f"기대 변화 {expected_delta:+,} 이 아닌 {delta:+,}")

    if max_loss_rate is not None:
        expected['max_loss_rate'] = max_loss_rate
        if loss_rate > max_loss_rate:
            problems.append(
                f"손실률 {loss_rate:.2%} > 허용 {max_loss_rate:.2%} "
                f"({n_before:,} -> {n_after:,})"
            )

    if not allow_increase and delta > 0:
        problems.append(f"행이 {delta:,}건 증가했습니다 ({n_before:,} -> {n_after:,}) "
                        f"— 다대다 병합을 의심하십시오")

    result = RESULT_FAIL if problems else RESULT_PASS
    message = '; '.join(problems) if problems else f"{n_before:,} -> {n_after:,} ({delta:+,})"

    return _finish(label, 'rowcount_delta', result, message,
                   {'before': n_before, 'after': n_after, 'delta': delta,
                    'loss_rate': round(loss_rate, 6)},
                   expected, strict)


def assert_join_loss(left, merged, key, label, max_loss_rate=0.0, strict=False):
    """
    병합에서 행이 유실되지 않았는지 검증한다

    SAS 의 다대다 MERGE, pandas 의 how='inner' 로 인한 조용한 손실을 잡는다

    Args:
        left: 병합 전 왼쪽 데이터프레임
        merged: 병합 결과 데이터프레임
        key: 병합 키 (문자열 또는 리스트)
        label: assertion 식별자
        max_loss_rate: 허용 손실률
        strict: True 이면 실패 시 예외

    Returns:
        통과하면 True
    """
    keys = [key] if isinstance(key, str) else list(key)

    try:
        left_keys = set(map(tuple, left[keys].astype(str).values.tolist()))
        merged_keys = set(map(tuple, merged[keys].astype(str).values.tolist()))
    except Exception as exc:
        return _finish(label, 'join_loss', RESULT_FAIL,
                       f"키 비교 실패: {type(exc).__name__}", None,
                       {'key': keys, 'max_loss_rate': max_loss_rate}, strict)

    lost = left_keys - merged_keys
    loss_rate = len(lost) / len(left_keys) if left_keys else 0.0

    n_left, n_merged = _nrow(left), _nrow(merged)
    problems = []

    if loss_rate > max_loss_rate:
        problems.append(
            f"키 {len(lost):,}개 유실 ({loss_rate:.2%} > 허용 {max_loss_rate:.2%})"
        )
    if n_merged > n_left:
        problems.append(
            f"병합 후 행이 늘었습니다 ({n_left:,} -> {n_merged:,}) — 다대다 병합"
        )

    result = RESULT_FAIL if problems else RESULT_PASS
    message = '; '.join(problems) if problems else (
        f"키 유실 없음 (행 {n_left:,} -> {n_merged:,})"
    )

    return _finish(label, 'join_loss', result, message,
                   {'left_keys': len(left_keys), 'merged_keys': len(merged_keys),
                    'lost_keys': len(lost), 'loss_rate': round(loss_rate, 6),
                    'rows_before': n_left, 'rows_after': n_merged},
                   {'key': keys, 'max_loss_rate': max_loss_rate}, strict)


def assert_unique(df, keys, label, strict=False):
    """
    key 조합이 유일한지 검증한다

    USUBJID, USUBJID+PARAMCD+AVISITN 같은 분석 데이터셋의 키 무결성을 확인한다

    Args:
        df: 검사할 데이터프레임
        keys: 키 컬럼명 (문자열 또는 리스트)
        label: assertion 식별자
        strict: True 이면 실패 시 예외

    Returns:
        통과하면 True
    """
    key_list = [keys] if isinstance(keys, str) else list(keys)

    try:
        subset = df[key_list]
        n_total = _nrow(subset)
        if hasattr(subset, 'drop_duplicates'):
            n_unique = _nrow(subset.drop_duplicates())
        else:
            n_unique = _nrow(subset.unique())
    except Exception as exc:
        return _finish(label, 'unique', RESULT_FAIL,
                       f"검사 실패: {type(exc).__name__}", None,
                       {'keys': key_list}, strict)

    duplicates = n_total - n_unique
    result = RESULT_FAIL if duplicates else RESULT_PASS
    message = (f"중복 {duplicates:,}건 ({'+'.join(key_list)})"
               if duplicates else f"유일 ({'+'.join(key_list)}, {n_unique:,}건)")

    return _finish(label, 'unique', result, message,
                   {'total': n_total, 'unique': n_unique, 'duplicates': duplicates},
                   {'keys': key_list, 'duplicates': 0}, strict)


def assert_domain(df, column, allowed, label, allow_missing=True, strict=False):
    """
    값이 허용 도메인 안인지 검증한다

    SEX in {M, F}, SAFFL in {Y, N} 같은 코드 목록 검증

    Args:
        df: 검사할 데이터프레임
        column: 검사할 컬럼명
        allowed: 허용 값 집합
        label: assertion 식별자
        allow_missing: True 이면 결측을 허용
        strict: True 이면 실패 시 예외

    Returns:
        통과하면 True
    """
    allowed_set = set(allowed)

    try:
        series = df[column]
        values = set(series.dropna().unique().tolist()) if hasattr(series, 'dropna') \
            else set(v for v in series.to_list() if v is not None)
        n_missing = int(series.isna().sum()) if hasattr(series, 'isna') else 0
    except Exception as exc:
        return _finish(label, 'domain', RESULT_FAIL,
                       f"검사 실패: {type(exc).__name__}", None,
                       {'column': column, 'allowed': sorted(map(str, allowed_set))},
                       strict)

    unexpected = values - allowed_set
    problems = []

    if unexpected:
        preview = sorted(map(str, unexpected))[:5]
        problems.append(f"허용되지 않은 값 {len(unexpected):,}종: {', '.join(preview)}")
    if n_missing and not allow_missing:
        problems.append(f"결측 {n_missing:,}건")

    result = RESULT_FAIL if problems else RESULT_PASS
    message = '; '.join(problems) if problems else (
        f"{column} 모두 허용 도메인 안 ({len(values):,}종)"
    )

    return _finish(label, 'domain', result, message,
                   {'distinct': len(values), 'unexpected': len(unexpected),
                    'missing': n_missing},
                   {'column': column, 'allowed': sorted(map(str, allowed_set)),
                    'allow_missing': allow_missing},
                   strict)


def assert_missingness(df, column, label, max_rate=None, expected_rate=None,
                       tolerance=0.01, strict=False):
    """
    결측률이 기대 범위 안인지 검증한다

    Args:
        df: 검사할 데이터프레임
        column: 검사할 컬럼명
        label: assertion 식별자
        max_rate: 허용 최대 결측률 (0.0 ~ 1.0)
        expected_rate: 기대 결측률
        tolerance: expected_rate 허용 오차
        strict: True 이면 실패 시 예외

    Returns:
        통과하면 True
    """
    try:
        series = df[column]
        n_total = _nrow(df)
        n_missing = int(series.isna().sum()) if hasattr(series, 'isna') \
            else int(series.is_null().sum())
        rate = n_missing / n_total if n_total else 0.0
    except Exception as exc:
        return _finish(label, 'missingness', RESULT_FAIL,
                       f"검사 실패: {type(exc).__name__}", None,
                       {'column': column}, strict)

    expected = {}
    problems = []

    if max_rate is not None:
        expected['max_rate'] = max_rate
        if rate > max_rate:
            problems.append(f"결측률 {rate:.2%} > 허용 {max_rate:.2%}")

    if expected_rate is not None:
        expected['expected_rate'] = expected_rate
        expected['tolerance'] = tolerance
        if abs(rate - expected_rate) > tolerance:
            problems.append(
                f"결측률 {rate:.2%} 가 기대 {expected_rate:.2%} ±{tolerance:.2%} 를 벗어남"
            )

    result = RESULT_FAIL if problems else RESULT_PASS
    message = '; '.join(problems) if problems else (
        f"{column} 결측 {n_missing:,}건 ({rate:.2%})"
    )

    return _finish(label, 'missingness', result, message,
                   {'missing': n_missing, 'total': n_total, 'rate': round(rate, 6)},
                   {'column': column, **expected}, strict)


def assert_date_order(df, earlier, later, label, allow_equal=True,
                      allow_missing=True, strict=False):
    """
    날짜 순서가 논리적인지 검증한다 (TRTSDT <= TRTEDT 등)

    Args:
        df: 검사할 데이터프레임
        earlier: 먼저여야 할 날짜 컬럼명
        later: 나중이어야 할 날짜 컬럼명
        label: assertion 식별자
        allow_equal: True 이면 같은 날짜를 허용
        allow_missing: True 이면 결측 행을 검사에서 제외
        strict: True 이면 실패 시 예외

    Returns:
        통과하면 True
    """
    try:
        subset = df[[earlier, later]]
        if allow_missing and hasattr(subset, 'dropna'):
            subset = subset.dropna()

        if allow_equal:
            violations = int((subset[earlier] > subset[later]).sum())
        else:
            violations = int((subset[earlier] >= subset[later]).sum())
        n_checked = _nrow(subset)
    except Exception as exc:
        return _finish(label, 'date_order', RESULT_FAIL,
                       f"검사 실패: {type(exc).__name__}", None,
                       {'earlier': earlier, 'later': later}, strict)

    result = RESULT_FAIL if violations else RESULT_PASS
    operator = '<=' if allow_equal else '<'
    message = (f"{earlier} {operator} {later} 위반 {violations:,}건"
               if violations else f"{earlier} {operator} {later} 정상 ({n_checked:,}건 검사)")

    return _finish(label, 'date_order', result, message,
                   {'violations': violations, 'checked': n_checked},
                   {'earlier': earlier, 'later': later,
                    'allow_equal': allow_equal, 'violations': 0},
                   strict)


# ============================================================================
# 임상 정합성 assertion
# ============================================================================

def assert_analysis_set(df, flag_column, flag_value, label,
                        expected_n=None, strict=False):
    """
    분석군 flag 가 SAP 정의와 일치하는지 검증한다

    Args:
        df: 분석군 필터를 적용한 데이터프레임
        flag_column: 분석군 flag 컬럼명 (SAFFL, FASFL, PPROTFL 등)
        flag_value: 포함 조건 값 (보통 'Y')
        label: assertion 식별자
        expected_n: SAP 에 명시된 기대 피험자 수
        strict: True 이면 실패 시 예외

    Returns:
        통과하면 True
    """
    try:
        n = _nrow(df)
        wrong_flag = 0
        if flag_column in getattr(df, 'columns', []):
            series = df[flag_column]
            wrong_flag = int((series != flag_value).sum())
    except Exception as exc:
        return _finish(label, 'analysis_set', RESULT_FAIL,
                       f"검사 실패: {type(exc).__name__}", None,
                       {'flag': flag_column, 'value': flag_value}, strict)

    expected = {'flag': flag_column, 'value': flag_value}
    problems = []

    if wrong_flag:
        problems.append(f"{flag_column} != '{flag_value}' 인 행 {wrong_flag:,}건 포함")

    if expected_n is not None:
        expected['expected_n'] = expected_n
        if n != expected_n:
            problems.append(f"SAP 명시 {expected_n:,}명과 불일치 (실제 {n:,}명)")

    result = RESULT_FAIL if problems else RESULT_PASS
    message = '; '.join(problems) if problems else (
        f"{flag_column}='{flag_value}' {n:,}명"
    )

    return _finish(label, 'analysis_set', result, message,
                   {'n': n, 'wrong_flag_rows': wrong_flag}, expected, strict)


def assert_denominator(df, subject_column, denominator, label, strict=False):
    """
    분모가 해당 분석군의 unique subject 수와 일치하는지 검증한다

    Args:
        df: 분석군 데이터프레임
        subject_column: 피험자 식별자 컬럼명 (보통 USUBJID)
        denominator: 표에 사용한 분모 값
        label: assertion 식별자
        strict: True 이면 실패 시 예외

    Returns:
        통과하면 True
    """
    try:
        series = df[subject_column]
        actual = int(series.nunique()) if hasattr(series, 'nunique') \
            else len(set(series.to_list()))
    except Exception as exc:
        return _finish(label, 'denominator', RESULT_FAIL,
                       f"검사 실패: {type(exc).__name__}", None,
                       {'subject_column': subject_column, 'denominator': denominator},
                       strict)

    result = RESULT_PASS if actual == denominator else RESULT_FAIL
    message = (f"분모 {denominator:,} = unique {subject_column} {actual:,}"
               if result == RESULT_PASS
               else f"분모 {denominator:,} != unique {subject_column} {actual:,}")

    return _finish(label, 'denominator', result, message, actual,
                   {'subject_column': subject_column, 'denominator': denominator},
                   strict)


def reconcile(expression, observed, expected, label, strict=False):
    """
    임상 정합성 관계식을 검증한다

    SAP 의 reconciliation 규칙을 그대로 옮긴다.
    예: 'ae_subject_count <= denominator', 'sum(arm_counts) == overall_count'

    Args:
        expression: 사람이 읽을 관계식 문자열
        observed: 관측값
        expected: 기대값 또는 상한/하한
        label: assertion 식별자
        strict: True 이면 실패 시 예외

    Returns:
        통과하면 True
    """
    passed = bool(observed)
    result = RESULT_PASS if passed else RESULT_FAIL
    message = (f"{expression} 만족" if passed
               else f"{expression} 위반 (관측 {observed}, 기대 {expected})")

    return _finish(label, 'reconciliation', result, message, observed,
                   {'expression': expression, 'expected': expected}, strict)


def assert_le(actual, limit, label, expression=None, strict=False):
    """
    actual <= limit 관계를 검증한다 (reconcile 의 단축형)

    가장 흔한 정합성 규칙인 'AE subject count <= denominator' 용

    Args:
        actual: 관측값
        limit: 상한
        label: assertion 식별자
        expression: 관계식 설명 (없으면 자동 생성)
        strict: True 이면 실패 시 예외

    Returns:
        통과하면 True
    """
    expr = expression or f"{label} <= {limit:,}"
    passed = actual <= limit
    result = RESULT_PASS if passed else RESULT_FAIL
    message = (f"{expr} 만족 ({actual:,} <= {limit:,})" if passed
               else f"{expr} 위반 ({actual:,} > {limit:,})")

    return _finish(label, 'reconciliation', result, message, actual,
                   {'expression': expr, 'max': limit}, strict)


def assert_sum_equals(parts, total, label, tolerance=0, strict=False):
    """
    부분의 합이 전체와 일치하는지 검증한다

    치료군별 합계 = 전체 합계 같은 표 정합성 확인

    Args:
        parts: 부분값 리스트 또는 그 합
        total: 전체값
        label: assertion 식별자
        tolerance: 허용 오차
        strict: True 이면 실패 시 예외

    Returns:
        통과하면 True
    """
    part_sum = sum(parts) if hasattr(parts, '__iter__') else parts
    diff = abs(part_sum - total)
    passed = diff <= tolerance
    result = RESULT_PASS if passed else RESULT_FAIL
    message = (f"부분 합 {part_sum:,} = 전체 {total:,}" if passed
               else f"부분 합 {part_sum:,} != 전체 {total:,} (차이 {diff:,})")

    return _finish(label, 'reconciliation', result, message,
                   {'part_sum': part_sum, 'total': total, 'diff': diff},
                   {'expression': 'sum(parts) == total', 'tolerance': tolerance},
                   strict)


def assert_coding_version(actual_version, expected_version, dictionary, label,
                          strict=False):
    """
    MedDRA / WHODrug 코딩 사전 버전이 DMP 명시와 일치하는지 검증한다

    Args:
        actual_version: 데이터에 기록된 버전
        expected_version: DMP 에 명시된 버전
        dictionary: 사전 이름 ('MedDRA', 'WHODrug')
        label: assertion 식별자
        strict: True 이면 실패 시 예외

    Returns:
        통과하면 True
    """
    passed = str(actual_version).strip() == str(expected_version).strip()
    result = RESULT_PASS if passed else RESULT_FAIL
    message = (f"{dictionary} {actual_version} (DMP 일치)" if passed
               else f"{dictionary} 버전 불일치: 데이터 {actual_version} vs DMP {expected_version}")

    return _finish(label, 'coding_version', result, message, str(actual_version),
                   {'dictionary': dictionary, 'expected': str(expected_version)},
                   strict)


# ============================================================================
# 요약
# ============================================================================

def summary():
    """
    현재 run 의 assertion 결과를 요약해 출력한다

    프로그램 끝에서 호출하면 로그에 요약이 남는다

    Returns:
        (전체 수, 통과 수, 실패 수)
    """
    path = _run_dir() / ASSERTIONS_FILENAME
    if not path.is_file():
        print("\n  assertion 기록 없음")
        return 0, 0, 0

    total = passed = failed = 0
    failures = []

    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            total += 1
            if record.get('result') == RESULT_PASS:
                passed += 1
            else:
                failed += 1
                failures.append(f"{record.get('label')}: {record.get('message')}")

    print(f"\n  assertion 요약: {total:,}건 중 {passed:,}건 통과 / {failed:,}건 실패")
    for item in failures:
        print(f"    FAIL {item}")

    return total, passed, failed
