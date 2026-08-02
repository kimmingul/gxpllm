---
description: SAP를 기계 판독 가능한 assertion 명세로 변환합니다
argument-hint: [table/figure ID 또는 비워두면 전체]
allowed-tools: Read, Write
---

# SAP → assertion 명세 변환

SAP의 정의를 검증 가능한 규칙으로 옮깁니다.
`/write-program`이 이 명세를 보고 코드에 assertion을 넣습니다.

## 왜 필요한가

기계적 무결성 검증(행 수, 결측률)만으로는 **"기계적으로 완전하지만 임상적으로 틀린 분석"**을
막지 못합니다. 분석군, 분모, 카운트 단위, TEAE window가 틀려도 assertion은 전부 통과합니다.

SAP의 정의를 명시적 규칙으로 옮겨야 잡을 수 있습니다.

## 대상

$1 (비어 있으면 전체)

## 입력

`docs/sap.md`

## 출력

`docs/assertions_spec.yaml`

## 형식

```yaml
study_id: ABC-301
sap_version: "2.0"
sap_approved: 2026-07-15
derived_at: 2026-08-02

# ---------------------------------------------------------------------------
# 분석군 정의 — flag 변수와 값을 명시해야 코드가 됩니다
# ---------------------------------------------------------------------------
analysis_sets:
  safety:
    flag: SAFFL
    value: "Y"
    label: "Safety Set"
    expected_n: 241        # SAP에 명시된 경우만. 없으면 생략
  fas:
    flag: FASFL
    value: "Y"
    label: "Full Analysis Set"
  pps:
    flag: PPROTFL
    value: "Y"
    label: "Per-Protocol Set"

# ---------------------------------------------------------------------------
# 데이터셋별 key — 유일성 검증에 사용
# ---------------------------------------------------------------------------
dataset_keys:
  adsl:  [USUBJID]
  adae:  [USUBJID, AESEQ]
  adlb:  [USUBJID, PARAMCD, AVISITN]
  advs:  [USUBJID, PARAMCD, AVISITN]
  adtte: [USUBJID, PARAMCD]

# ---------------------------------------------------------------------------
# 변수 도메인 — Data Dictionary와 SAP를 대조해 도출
# ---------------------------------------------------------------------------
domains:
  SEX:    ["M", "F"]
  SAFFL:  ["Y", "N"]
  FASFL:  ["Y", "N"]
  AESER:  ["Y", "N"]
  AESEV:  ["MILD", "MODERATE", "SEVERE"]

# ---------------------------------------------------------------------------
# 날짜 순서
# ---------------------------------------------------------------------------
date_orders:
  - { earlier: RFSTDTC, later: RFENDTC, label: STUDY_PERIOD }
  - { earlier: TRTSDT,  later: TRTEDT,  label: TREATMENT_PERIOD }
  - { earlier: ASTDT,   later: AENDT,   label: AE_PERIOD }

# ---------------------------------------------------------------------------
# 코딩 사전 — DMP와 일치해야 합니다
# ---------------------------------------------------------------------------
coding:
  meddra:   "27.0"
  whodrug:  "GLOBALB3Mar26"

# ---------------------------------------------------------------------------
# 표별 명세
# ---------------------------------------------------------------------------
tables:
  t_14_1_1:
    title: "인구통계학적 특성"
    analysis_set: safety
    count_unit: subject
    denominator: safety.unique_subjects
    inputs: [adsl]
    reconciliation:
      - "sum(arm_counts) == overall_count"
      - "each_cell_n <= denominator"

  t_14_3_1:
    title: "이상반응 요약 (TEAE)"
    analysis_set: safety
    count_unit: subject          # event가 아님 — 이 한 줄이 표를 바꿉니다
    denominator: safety.unique_subjects
    inputs: [adsl, adae]
    teae_window:
      start: TRTSDT
      end: "TRTEDT + 30"
      flag: TRTEMFL              # 이미 계산된 flag가 있으면 그것을 씁니다
    partial_date_rule: impute_first_of_month
    missing_handling:
      exclude_from_denominator: false
    coding:
      dictionary: MedDRA
      version_ref: coding.meddra
    reconciliation:
      - "ae_subject_count <= denominator"
      - "sum(arm_counts) == overall_count"
      - "serious_count <= total_ae_count"
      - "sum(soc_counts) >= overall_count"   # 한 피험자가 여러 SOC 가능

  f_14_2_1:
    title: "무진행생존 Kaplan-Meier 곡선"
    analysis_set: fas
    count_unit: subject
    inputs: [adtte]
    censoring_rule: "SAP 6.3.2 참조 — 마지막 평가 시점에서 중도절단"
    reconciliation:
      - "events + censored == total_subjects"

# ---------------------------------------------------------------------------
# SAP에서 확인할 수 없었던 항목 — 반드시 남기십시오
# ---------------------------------------------------------------------------
unresolved:
  - table: t_14_3_1
    issue: "partial date가 첫 투여일 이전이 되는 경우 처리 규칙이 SAP에 없음"
    action: "통계책임자 확인 필요"
```

## 도출 절차

### 1단계: 분석군

SAP의 분석군 정의에서 **flag 변수명과 값**을 찾습니다.
서술만 있고 변수명이 없으면 `docs/data_dictionary.md`에서 대응 변수를 찾되,
확신할 수 없으면 `unresolved`에 남기십시오.

### 2단계: 표별 명세

각 table shell에서 추출합니다.

| 항목 | SAP에서 찾을 위치 |
|---|---|
| 분석군 | 표 머리 또는 분석군 정의 절 |
| 분모 | "N=" 표기, 백분율 계산 기준 |
| **카운트 단위** | "피험자 수" vs "발현 건수" |
| TEAE window | 안전성 분석 절 |
| partial date | 결측 데이터 처리 절 |
| baseline | 유효성 분석 절 |
| 반올림 | 표 작성 규약 절 |

### 3단계: 정합성 규칙 도출

**검증 가능한 관계식으로 씁니다.** 흔히 쓰는 것:

```yaml
- "ae_subject_count <= denominator"           # AE 피험자 수는 분모 이하
- "sum(arm_counts) == overall_count"          # 군별 합 = 전체
- "serious_count <= total_ae_count"           # SAE는 AE의 부분집합
- "screened >= randomized >= treated"         # cohort flow 단조성
- "events + censored == total_subjects"       # 생존분석 완결성
- "each_visit_n <= analysis_set_n"            # 방문별 분모 상한
```

### 4단계: 미해결 항목 정리

SAP에서 판단할 수 없었던 것을 `unresolved`에 나열합니다.

**추측으로 채우지 마십시오.** 이 항목들이 곧 통계책임자에게 확인할 목록입니다.

## 주의

- SAP가 승인되지 않았으면 `sap_approved`를 비우고, 이 명세도 잠정임을 표시하십시오.
- Data Dictionary의 `추정 의미`가 검토(⬜)되지 않은 변수를 근거로 삼았다면 명시하십시오.
- 이 파일은 코드 생성의 명세가 됩니다. **여기서 모호하면 그대로 잘못된 분석이 됩니다.**
