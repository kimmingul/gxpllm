---
name: clinical-conventions
description: 임상시험 데이터 분석의 표준 용어, CDISC 규약, TLF 형식, 분석군 정의, 데이터 경계 정책. 임상 데이터 분석 작업을 할 때, SAP/Protocol/DMP를 다룰 때, ADaM/SDTM 데이터셋을 참조할 때, TLF를 만들거나 검토할 때 사용합니다.
---

# 임상시험 데이터 분석 규약

## 데이터 경계 — 가장 먼저 확인할 것

이 환경에서 Opus는 임상 데이터에 접근할 수 없습니다. hook이 결정론적으로 차단합니다.

| 구분 | 접근 |
|---|---|
| `docs/` — Protocol, SAP, DMP, Data Dictionary | 가능 |
| `programs/` — SAS/Python/R 코드 | 가능 |
| `output/tables/` — 집계 표 | 가능 |
| `output/figures/` — 그림 | 가능 |
| `logs/runs/*/manifest.json`, `assertions.json` | 가능 |
| `data/` — 원본 및 파생 데이터셋 | **차단** |
| `output/listings/` — 피험자 단위 목록 | **차단** |
| `logs/runs/*/stdout.txt`, `execution.log` | **차단** |

**Table과 Figure는 되고 Listing은 안 됩니다.** TLF 중 L만 성질이 다릅니다 — 피험자 단위입니다.

데이터가 필요한 작업은 로컬 LLM(MCP `local-coder`)에게 코드를 작성시키고
runner로 실행한 뒤 집계 결과를 읽습니다.

## 용어

| 약어 | 원어 | 국문 |
|---|---|---|
| SAP | Statistical Analysis Plan | 통계분석계획서 |
| DMP | Data Management Plan | 자료관리계획서 |
| CSR | Clinical Study Report | 임상시험결과보고서 |
| TLF / TFL | Tables, Listings, Figures | 표·목록·그림 |
| CRF | Case Report Form | 증례기록서 |
| AE / SAE | Adverse Event / Serious AE | 이상반응 / 중대한 이상반응 |
| TEAE | Treatment-Emergent Adverse Event | 치료 중 발생 이상반응 |
| SDTM | Study Data Tabulation Model | CDISC 원자료 표준 |
| ADaM | Analysis Data Model | CDISC 분석용 데이터 표준 |
| IB | Investigator's Brochure | 시험자자료집 |
| eTMF | electronic Trial Master File | 전자 시험 마스터 파일 |
| IRB | Institutional Review Board | 기관생명윤리위원회 |

## 분석군 (Analysis Sets)

**서술이 아니라 flag 변수와 값으로 명시해야 코드가 됩니다.**

| 분석군 | 표준 flag | 정의 |
|---|---|---|
| Safety Set | `SAFFL = 'Y'` | 시험약을 1회 이상 투여받은 피험자 |
| Full Analysis Set (FAS) | `FASFL = 'Y'` | 무작위배정되고 유효성 평가가 1회 이상 있는 피험자 |
| Per-Protocol Set (PPS) | `PPROTFL = 'Y'` | 주요 위반이 없는 FAS 피험자 |
| Randomized Set | `RANDFL = 'Y'` | 무작위배정된 피험자 |
| Completers | `COMPLFL = 'Y'` | 시험을 완료한 피험자 |

**안전성 분석은 Safety Set, 유효성 분석은 FAS가 기본입니다.**

## 주요 ADaM 데이터셋

| 데이터셋 | 내용 | 단위 |
|---|---|---|
| ADSL | Subject-Level Analysis Dataset | 피험자 1행 |
| ADAE | Adverse Events | 이상반응 1행 |
| ADLB | Laboratory | 피험자×검사×방문 |
| ADVS | Vital Signs | 피험자×측정×방문 |
| ADTTE | Time-to-Event | 피험자×평가변수 |
| ADEX | Exposure | 투약 기록 |
| ADCM | Concomitant Medications | 병용약 |

## 자주 쓰는 ADaM 변수

| 변수 | 의미 | 주의 |
|---|---|---|
| `USUBJID` | Unique Subject ID | 전 데이터셋 공통 key |
| `SUBJID` | Subject ID (시험 내) | USUBJID와 다름 |
| `TRT01P` / `TRT01A` | 계획/실제 치료군 | **눈가림 상태에서는 접근 불가** |
| `SAFFL`, `FASFL`, `PPROTFL` | 분석군 flag | |
| `TRTSDT` / `TRTEDT` | 첫/마지막 투여일 | TEAE window 기준 |
| `AVAL` / `AVALC` | 분석값 (숫자/문자) | |
| `BASE` / `CHG` / `PCHG` | baseline / 변화량 / 변화율 | |
| `PARAMCD` / `PARAM` | 평가변수 코드/명 | key 구성요소 |
| `AVISIT` / `AVISITN` | 분석 방문 | key 구성요소 |
| `ABLFL` | Baseline Record Flag | |
| `ANL01FL` | Analysis Flag | 분석 대상 레코드 |
| `AETERM` / `AEDECOD` / `AEBODSYS` | AE 원문 / MedDRA PT / SOC | AETERM은 자유기술 |
| `AESER` / `AESEV` / `AEREL` | 중대성 / 중증도 / 인과관계 | |
| `TRTEMFL` | Treatment Emergent Flag | TEAE 판정 |

## key 조합

| 데이터셋 | 유일 key |
|---|---|
| ADSL | `USUBJID` |
| ADAE | `USUBJID` + `AESEQ` |
| ADLB / ADVS | `USUBJID` + `PARAMCD` + `AVISITN` |
| ADTTE | `USUBJID` + `PARAMCD` |

**key 유일성 검증은 모든 분석 프로그램의 필수 assertion입니다.**

## TLF 번호 관례

| 번호 | 내용 |
|---|---|
| 14.1.x | 피험자 배치, 인구통계학적 특성, baseline 특성 |
| 14.2.x | 유효성 |
| 14.3.x | 안전성 (이상반응) |
| 14.3.4~5 | 임상검사, 활력징후 |
| 16.2.x | Listing (피험자 단위) |

접두사: `t_` (Table), `f_` (Figure), `l_` (Listing)

## 임상 분석에서 조용히 틀리는 지점

assertion이 반드시 잡아야 하는 것들입니다.

| 오류 | 증상 | 검증 |
|---|---|---|
| **다대다 병합** | 행 수가 조용히 늘어남 | 병합 전후 행 수 비교 |
| **분모 혼동** | 백분율이 틀림 | 분모 = 분석군 unique subject 수 확인 |
| **subject vs event** | AE 표에서 흔함 | SAP의 카운트 단위 명시와 대조 |
| **TEAE window** | 투여 전/후 AE가 섞임 | `TRTSDT` ~ `TRTEDT + N일` 범위 확인 |
| **partial date** | 결측 날짜 처리 불일치 | SAP의 대체 규칙과 대조 |
| **baseline 정의** | 어느 측정을 baseline으로 볼지 | 첫 투여 이전 마지막 non-missing |
| **결측 처리** | 분모에서 빼는지 별도 범주인지 | SAP 명시와 대조 |
| **치료군 불일치** | 계획(TRT01P)과 실제(TRT01A) 혼동 | 안전성은 실제, 유효성은 계획이 보통 |
| **visit window** | 방문 범위 밖 측정 포함 | SAP의 window 정의 |
| **단위 불일치** | 검사 단위가 기관마다 다름 | 표준 단위 변환 확인 |

## 눈가림 (Blinding)

Database lock 및 눈가림 해제 전에는:

- 치료군 관련 변수(`TRT01A`, `TRT01P`, `ACTARM`)에 접근할 수 없습니다
- 치료군 간 비교를 서술하지 마십시오
- `.gxpllm/config.json`의 `blinded: true`가 이 상태를 나타냅니다

눈가림 해제 후 `blinded: false`로 변경합니다.

## 소규모 셀 (재식별 위험)

Table에 `n=1` 또는 `n=2` 셀이 있으면 **재식별 위험을 검토**해야 합니다.

특히 위험한 조합:
- 희귀질환 시험
- 소규모 사이트
- 연령 극단값 + 성별 + 기관
- 희귀 병용약, 희귀 유전자 변이

발견하면 사용자에게 보고하십시오. 외부 공유 전 별도 판단이 필요합니다.

## 재현성 — 언어별 차이

| | SAS 9.4 | Python | R |
|---|---|---|---|
| 로그 자동 생성 | **예** (`.log`) | 아니오 (runner가 생성) | 아니오 (runner가 생성) |
| 반환코드 신뢰도 | **낮음** — 로그 스캔 필수 | 높음 | 중간 |
| 환경 잠금 | 설치 버전 고정 | `uv.lock` | `renv.lock` |
| 재현성 위험 | 낮음 | **높음** | **높음** |

**제출 경로(`submission_candidate`)는 SAS를 기본으로 하십시오.**
Python/R은 패키지 마이너 버전 변경으로 결과가 달라질 수 있습니다.

## purpose 구분

| purpose | 실패 조건 | 용도 |
|---|---|---|
| `exploratory` | ERROR만 | 탐색적 분석. **규제 결론에 쓰지 마십시오** |
| `qc` | ERROR + WARNING | Independent Programming |
| `submission_candidate` | ERROR + WARNING + CRITICAL NOTE | 제출 경로. 환경 잠금 필수 |

## LLM이 하지 않는 것

1. **숫자를 만들지 않습니다.** 계산은 SAS/Python/R만 합니다.
2. **데이터 무결성을 눈으로 검토하지 않습니다.** assertion이 검증합니다.
3. **QC 대조를 판단하지 않습니다.** `compare_outputs.py`가 비교하고 사람이 판정합니다.
4. **SAP에 없는 것을 정하지 않습니다.** 모호하면 사용자에게 확인합니다.
