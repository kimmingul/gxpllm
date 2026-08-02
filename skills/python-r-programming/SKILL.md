---
name: python-r-programming
description: 임상 분석용 Python 및 R 프로그램 작성 규약. GXPLLM-META 헤더, assertion 라이브러리 사용법, 환경 잠금(uv.lock/renv.lock), 조용히 틀리는 pandas/dplyr 패턴, traceback 정제. Python 또는 R로 임상 분석 프로그램을 작성하거나 검토할 때, 실행 로그를 해석할 때 사용합니다.
---

# Python / R 임상 분석 프로그램 규약

## SAS와의 결정적 차이

| | SAS 9.4 | Python | R |
|---|---|---|---|
| 로그 자동 생성 | 예 (`.log`) | **아니오** — runner가 생성 | **아니오** — runner가 생성 |
| 환경 잠금 | 설치 버전 고정 | `uv.lock` | `renv.lock` |
| 재현성 위험 | 낮음 | **높음** | **높음** |

**제출 경로(`submission_candidate`)에서는 환경 잠금이 필수입니다.**
runner가 lock 파일이 없으면 실행을 거부합니다.

Python/R은 패키지 마이너 버전 변경으로 결과가 달라질 수 있습니다.
6개월 뒤 CSR 검토 중 재실행했는데 숫자가 다르면 규제상 곤란해집니다.

---

## Python

### 프로그램 헤더 — 필수

```python
"""
Table 14.3.1 이상반응 요약 (TEAE)

GXPLLM-META-BEGIN
program      : t_14_3_1.py
purpose      : Table 14.3.1 SOC/PT 별 TEAE 요약
sap_ref      : docs/sap.md#table-14-3-1
inputs       : data/derived/adsl.parquet, data/derived/adae.parquet
outputs      : output/tables/t_14_3_1.rtf
analysis_set : Safety Set (SAFFL='Y')
count_unit   : subject
author       : local-llm/Qwen3.6-35B-A3B
GXPLLM-META-END
"""
```

### assertion 라이브러리

```python
import os
import sys

sys.path.insert(0, os.environ['GXPLLM_PLUGIN_ROOT'] + '/scripts')
import gxpllm_assert as na
```

| 함수 | 용도 |
|---|---|
| `na.assert_rowcount(df, label=, expected_min=, expected_max=, expected_n=)` | 행 수 |
| `na.assert_rowcount_delta(before, after, label=, max_loss_rate=, allow_increase=False)` | 변환 전후 |
| `na.assert_join_loss(left, merged, key=, label=, max_loss_rate=0.0)` | 병합 손실 |
| `na.assert_unique(df, keys=, label=)` | key 유일성 |
| `na.assert_domain(df, column=, allowed=, label=)` | 값 도메인 |
| `na.assert_missingness(df, column=, label=, max_rate=)` | 결측률 |
| `na.assert_date_order(df, earlier=, later=, label=)` | 날짜 순서 |
| `na.assert_analysis_set(df, flag_column=, flag_value='Y', label=, expected_n=)` | 분석군 |
| `na.assert_denominator(df, subject_column='USUBJID', denominator=, label=)` | 분모 |
| `na.assert_le(actual, limit, label=, expression=)` | 정합성 |
| `na.assert_sum_equals(parts, total, label=)` | 합계 정합 |
| `na.assert_coding_version(actual, expected, dictionary, label=)` | 코딩 사전 버전 |
| `na.summary()` | 요약 출력 (프로그램 끝에서 호출) |

### 환경변수

runner가 전달합니다.

| 변수 | 내용 |
|---|---|
| `GXPLLM_STUDY_ROOT` | study 루트 절대경로 |
| `GXPLLM_RUN_DIR` | run 디렉터리 (assertion 기록 위치) |
| `GXPLLM_RUN_ID` | run 식별자 |
| `GXPLLM_PLUGIN_ROOT` | plugin 루트 |

### 조용히 틀리는 pandas 패턴

**merge에 validate를 반드시 지정**

```python
# 위험 — adae에 USUBJID 중복이 있으면 행이 늘어남
merged = adsl.merge(adae, on='USUBJID', how='left')

# 안전 — 관계를 명시하면 위반 시 예외 발생
merged = adsl.merge(adae, on='USUBJID', how='left', validate='one_to_many')
na.assert_join_loss(adsl, merged, key='USUBJID', label='AE_JOIN')
```

| validate | 의미 |
|---|---|
| `one_to_one` | 양쪽 모두 key 유일 |
| `one_to_many` | 왼쪽만 유일 (ADSL → ADAE) |
| `many_to_one` | 오른쪽만 유일 |
| `many_to_many` | 검증 없음 — **임상 데이터에서 쓰지 마십시오** |

**SettingWithCopyWarning은 무시하면 안 됩니다**

```python
# 위험 — 뷰에 대입해 원본이 안 바뀔 수 있음
saf = adsl[adsl['SAFFL'] == 'Y']
saf['AGEGR'] = np.where(saf['AGE'] >= 65, '>=65', '<65')   # 경고

# 안전
saf = adsl[adsl['SAFFL'] == 'Y'].copy()
saf['AGEGR'] = np.where(saf['AGE'] >= 65, '>=65', '<65')
```

**결측 처리**

```python
# 위험 — NaN 비교는 항상 False, 조용히 제외됨
flagged = df[df['AVAL'] < 100]

# 안전 — 의도를 명시
flagged = df[df['AVAL'].notna() & (df['AVAL'] < 100)]

# groupby는 기본적으로 NaN 그룹을 버림
counts = df.groupby('AEDECOD').size()                # NaN 제외
counts = df.groupby('AEDECOD', dropna=False).size()  # NaN 포함
```

**dtype 혼합**

```python
# 위험 — 컬럼에 숫자와 문자가 섞이면 object가 되고 정렬/비교가 깨짐
df = pd.read_csv('adsl.csv')                # DtypeWarning

# 안전 — dtype 명시
df = pd.read_csv('adsl.csv', dtype={'USUBJID': str, 'AGE': 'Int64'})
```

### runner가 스캔하는 CRITICAL 경고

| 규칙 | 의미 |
|---|---|
| `SETTING_WITH_COPY` | 뷰에 대입 — 원본이 안 바뀜 |
| `DTYPE_WARNING` | 컬럼 타입 혼합 |
| `FUTURE_WARNING` | 다음 버전에서 동작 변경 — 재현성 위험 |
| `RUNTIME_WARNING` | invalid value, divide by zero, overflow |
| `NUMPY_ALL_NAN` | 빈 슬라이스 평균, 자유도 0 |

runner는 `-W always`로 실행하므로 경고가 억제되지 않습니다.

### traceback 주의

**Python traceback에는 데이터 값이 딸려 나옵니다.**

```
ValueError: cannot merge on 'USUBJID': found duplicates ['ABC-301-0042', ...]
```

runner가 원문은 로컬 로그에만 남기고, Opus에게는 예외 타입 + 위치만 정제해 전달합니다.
직접 traceback을 Opus에게 복사하지 마십시오.

---

## R

### 프로그램 헤더 — 필수

```r
# GXPLLM-META-BEGIN
# program      : f_14_2_1.R
# purpose      : Figure 14.2.1 무진행생존 Kaplan-Meier 곡선
# sap_ref      : docs/sap.md#figure-14-2-1
# inputs       : data/derived/adtte.parquet
# outputs      : output/figures/f_14_2_1.png
# analysis_set : Full Analysis Set (FASFL='Y')
# GXPLLM-META-END
```

### assertion 라이브러리

```r
source(file.path(Sys.getenv("GXPLLM_PLUGIN_ROOT"), "scripts", "gxpllm_assert.R"))
```

| 함수 | 용도 |
|---|---|
| `gxpllm_assert_rowcount(df, label=, expected_min=)` | 행 수 |
| `gxpllm_assert_rowcount_delta(before, after, label=, max_loss_rate=)` | 변환 전후 |
| `gxpllm_assert_unique(df, keys=, label=)` | key 유일성 |
| `gxpllm_assert_domain(df, column=, allowed=, label=)` | 값 도메인 |
| `gxpllm_assert_missingness(df, column=, label=, max_rate=)` | 결측률 |
| `gxpllm_assert_date_order(df, earlier=, later=, label=)` | 날짜 순서 |
| `gxpllm_assert_analysis_set(df, flag_column=, flag_value="Y", label=)` | 분석군 |
| `gxpllm_assert_denominator(df, subject_column="USUBJID", denominator=, label=)` | 분모 |
| `gxpllm_assert_le(actual, limit, label=)` | 정합성 |
| `gxpllm_assert_sum_equals(parts, total, label=)` | 합계 정합 |
| `gxpllm_assert_summary()` | 요약 출력 (프로그램 끝) |

### runner가 자동 주입하는 것 — 중복 설정하지 마십시오

```r
options(warn = 1)              # exploratory/qc, 경고 즉시 출력
options(warn = 2)              # submission_candidate, 경고를 오류로 승격
options(digits = 15)
options(stringsAsFactors = FALSE)
renv::restore(prompt = FALSE)  # renv.lock 이 있으면
print(sessionInfo())           # 프로그램 종료 시
```

runner는 `Rscript --vanilla`로 실행합니다.
`.Rprofile`, `.RData`, 환경변수 로딩이 모두 꺼져 이전 세션이 결과에 영향을 주지 않습니다.

### 조용히 틀리는 R 패턴

**dplyr join에 relationship 명시**

```r
# 위험 — 다대다 조인이 조용히 행을 늘림
merged <- left_join(adsl, adae, by = "USUBJID")

# 안전 — 관계 위반 시 경고/오류
merged <- left_join(adsl, adae, by = "USUBJID", relationship = "one-to-many")
gxpllm_assert_rowcount_delta(adae, merged, label = "AE_JOIN", allow_increase = FALSE)
```

**강제 변환으로 NA 발생**

```r
# 위험 — "NA", "", ">100" 같은 값이 조용히 NA가 됨
df$AVAL <- as.numeric(df$AVAL)      # Warning: NAs introduced by coercion

# 안전 — 변환 전후 결측 수를 비교
before <- sum(is.na(df$AVAL))
df$AVAL <- as.numeric(df$AVAL)
gxpllm_assert_missingness(df, "AVAL", label = "AVAL_COERCE", max_rate = 0.05)
```

**벡터 재활용**

```r
# 위험 — 길이가 안 맞으면 조용히 재활용됨
df$flag <- c("Y", "N")   # Warning: longer object length is not a multiple

# 안전 — 길이를 명시적으로 맞춤
df$flag <- ifelse(df$AGE >= 65, "Y", "N")
```

**if의 조건 길이**

```r
# 위험 — R 4.2 이전에는 첫 원소만 쓰고 조용히 넘어감
if (df$AGE >= 65) { ... }

# 안전
if (all(df$AGE >= 65)) { ... }
```

**factor level**

```r
# 위험 — 정의되지 않은 level 대입 시 NA 발생
df$ARM <- factor(df$ARM, levels = c("A", "B"))
df$ARM[1] <- "C"   # Warning: invalid factor level, NA generated
```

### runner가 스캔하는 CRITICAL 경고

| 규칙 | 의미 |
|---|---|
| `NAS_INTRODUCED` | 강제 변환으로 NA 발생 |
| `MANY_TO_MANY` | dplyr 다대다 조인 |
| `LONGER_OBJECT` | 벡터 길이 불일치 재활용 |
| `INVALID_FACTOR` | 정의되지 않은 factor level |
| `ARGUMENT_LENGTH` | 조건문 길이 > 1 |
| `NAN_PRODUCED` | NaN 발생 |

### locale

정렬 순서가 locale에 의존합니다. 한국어 환경에서는 명시하십시오.

```r
Sys.setlocale("LC_ALL", "Korean_Korea.949")
```

---

## 두 언어 공통

### 환경 잠금 준비

```bash
# Python
cd D:\clinical\ABC-301
uv init
uv add pandas polars pyarrow numpy scipy statsmodels lifelines
copy uv.lock .gxpllm\env\uv.lock

# R
R -e 'renv::init(); renv::snapshot()'
copy renv.lock .gxpllm\env\renv.lock
```

### 실행

```bash
python scripts/run_python.py --program programs/python/t_14_3_1.py --purpose exploratory
python scripts/run_r.py      --program programs/r/f_14_2_1.R      --purpose exploratory
```

**직접 실행하지 마십시오.** hook이 차단합니다.

### 하지 말 것

- 실제 피험자 ID, 실제 수치, 자유기술 텍스트를 코드에 하드코딩
- 원본 데이터 수정 (`data/raw/`는 읽기 전용)
- `output/` 밖에 산출물 쓰기
- `validate=` / `relationship=` 없는 join
- 경고 억제 (`warnings.filterwarnings('ignore')`, `suppressWarnings()`)
- assertion 없이 데이터 변환
