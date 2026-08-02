---
description: Protocol과 Data Dictionary로 SAP(통계분석계획서) 초안을 작성합니다
argument-hint: [특정 섹션 또는 비워두면 전체]
allowed-tools: Read, Write, Glob
---

# SAP 초안 작성

Protocol과 Data Dictionary를 근거로 SAP(Statistical Analysis Plan) 초안을 작성합니다.

**이 작업에는 피험자 데이터가 필요하지 않습니다.** Opus가 직접 수행하는 것이 적절합니다.

## 입력

- `docs/protocol.md` — 임상시험계획서
- `docs/data_dictionary.md` — 실제 데이터 구조 (없으면 `/build-dictionary` 먼저)
- `docs/dmp.md` — 자료관리계획서 (있으면)

## 범위

$1 (비어 있으면 전체)

## SAP에 반드시 포함할 것

이후 `/write-program`이 코드의 명세로 삼는 부분입니다.
**여기서 모호하면 그대로 잘못된 분석이 됩니다.**

### 1. 분석군 (Analysis Sets)

각 분석군마다 **flag 변수명과 값**을 명시합니다. 서술만으로는 코드가 못 씁니다.

```
Safety Set          : SAFFL = 'Y'    시험약을 1회 이상 투여받은 피험자
Full Analysis Set   : FASFL = 'Y'    무작위배정되고 유효성 평가가 1회 이상 있는 피험자
Per-Protocol Set    : PPROTFL = 'Y'  주요 위반이 없는 FAS 피험자
```

### 2. Estimand

주요 평가변수마다 다음 5요소를 명시합니다.
- 대상 집단
- 평가변수
- intercurrent event 처리 전략
- population-level summary
- 결측 처리

### 3. 표별 명세 (table shell마다)

| 항목 | 왜 필요한가 |
|---|---|
| 분석군 | 어떤 flag로 필터할지 |
| **분모** | 무엇을 100%로 볼지 |
| **카운트 단위** | subject인가 event인가 — 이 한 줄이 표를 바꿉니다 |
| 층화 변수 | 치료군, 방문차수 등 |
| 결측 처리 | 분모 제외인지 별도 범주인지 |
| 표시 반올림 | 소수 자릿수 |
| 정합성 규칙 | 검증 가능한 관계식 |

### 4. TEAE 정의 (AE 관련 표)

```
TEAE window   : 첫 투여일 이후 ~ 마지막 투여일 + 30일
partial date  : 월만 있으면 해당 월 1일로 대체, 연도만 있으면 1월 1일
                단, 첫 투여일 이전이 되면 첫 투여일로
코딩 사전     : MedDRA 27.0 (DMP와 일치해야 함)
```

### 5. Baseline 정의

```
baseline : 첫 투여 이전 마지막 non-missing 측정값
           같은 날 여러 측정이 있으면 시각이 늦은 것
           시각이 없으면 마지막 레코드
```

### 6. 정합성 규칙 (reconciliation)

**검증 가능한 관계식으로 씁니다.** `/write-program`이 이것을 assertion으로 변환합니다.

```
- ae_subject_count <= denominator
- sum(arm_counts) == overall_count
- screened >= randomized >= treated >= completed
- 각 방문의 분모 <= 해당 분석군 피험자 수
```

## 작성 절차

### 1단계: Protocol에서 추출

목적, 시험 설계, 평가변수, 통계 방법, 표본 크기 근거를 읽습니다.

### 2단계: Data Dictionary와 대조

Protocol에 서술된 변수가 실제 데이터에 어떤 이름으로 있는지 확인합니다.

**불일치를 발견하면 반드시 명시하십시오.**
- Protocol에는 있는데 데이터에 없는 변수
- 데이터에 있는데 Protocol에 없는 변수
- 값 도메인이 Protocol 서술과 다른 경우

Data Dictionary의 `추정 의미` 컬럼이 아직 검토(⬜)되지 않았다면,
그 변수를 근거로 한 부분은 **잠정**으로 표시하십시오.

### 3단계: table shell 작성

각 표마다 행/열 구조와 위 명세를 함께 적습니다.

### 4단계: 미결 항목 정리

SAP 문서 끝에 `## 확인이 필요한 항목` 절을 두고, 판단할 수 없었던 것을 나열합니다.
**추측으로 채우지 마십시오.**

## 출력

`docs/sap.md`

문서 머리에 다음을 넣습니다.

```markdown
# Statistical Analysis Plan — {STUDY_ID}

버전: 0.1 (초안)
작성: Claude Opus (초안) — **통계책임자 검토 필수**
근거 문서: docs/protocol.md, docs/data_dictionary.md
작성일: {날짜}

> 이 문서는 LLM이 작성한 초안입니다. 승인 전까지 분석에 사용하지 마십시오.
```

## 주의

- SAP는 규제 문서입니다. 초안일 뿐이며 통계책임자 검토와 승인이 필요합니다.
- 피험자 데이터를 근거로 값을 채우지 마십시오. 실제 피험자 수 등은 검토 시 확정합니다.
