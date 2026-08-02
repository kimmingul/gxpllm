---
description: SAP의 table shell과 Data Dictionary로 SAS/Python/R 분석 프로그램을 생성합니다
argument-hint: <table/figure ID> [sas|python|r]
allowed-tools: mcp__local-coder__write_program, Read, Write, Glob
---

# 분석 프로그램 생성

SAP에 정의된 table shell을 구현하는 분석 프로그램을 로컬 LLM에게 작성시킵니다.

## 대상

$1 (예: `t_14_1_1`, `f_14_2_1`, `l_16_2_1`)
언어: $2 (미지정이면 아래 기준으로 판단)

## 언어 선택 기준

| 상황 | 선택 |
|---|---|
| 제출 경로(`submission_candidate`) 산출물 | **SAS** — 재현성이 가장 높고 `.log`가 규제 증적이 됨 |
| 원본이 `.sas7bdat` / `.xpt` | SAS |
| 탐색적 분석, 데이터 프로파일링 | Python |
| 생존분석 그림, 고급 통계 그래픽 | R |
| 기존 프로그램과 같은 언어여야 하는 경우 | 기존 언어 유지 |

**제출 경로에서는 SAS를 기본으로 하십시오.** Python/R은 패키지 변동으로 재현성이 낮습니다.

## 절차

### 1단계: SAP에서 명세 추출

`docs/sap.md`를 읽고 $1에 해당하는 부분을 찾습니다. 다음을 정확히 파악하십시오.

- **분석군(Analysis Set)**: Safety Set / FAS / PPS 중 무엇인가, flag 변수와 값
- **분모(denominator)**: 무엇을 분모로 쓰는가
- **카운트 단위**: subject인가 event인가 — **이 한 줄이 표를 바꿉니다**
- **TEAE window**: 시작/종료 기준, partial date 처리 규칙
- **baseline 정의**: 마지막 non-missing pre-dose인가, 특정 방문인가
- **결측 처리**: 분모에서 제외하는가, 별도 범주로 표시하는가
- **표시 반올림**: 소수 자릿수
- **table shell**: 행/열 구조, 라벨

SAP에 명시되지 않은 항목이 있으면 **임의로 정하지 말고 사용자에게 확인**하십시오.
그대로 진행하면 "기계적으로 완전하지만 임상적으로 틀린 분석"이 나옵니다.

### 2단계: assertion 명세 도출

SAP 내용을 검증 가능한 규칙으로 바꿉니다. 최소한 다음을 포함하십시오.

```yaml
- rowcount: 입력 데이터셋이 비어 있지 않은가
- analysis_set: 분석군 flag가 SAP 정의와 일치하는가, 피험자 수가 SAP 명시와 맞는가
- unique: key 조합이 유일한가 (USUBJID 또는 USUBJID+PARAMCD+AVISITN)
- rowcount_delta: 필터/병합 후 행 수 변화가 의도한 것인가
- join_loss: 병합에서 피험자가 유실되지 않았는가
- denominator: 표의 분모가 분석군 unique subject 수와 일치하는가
- reconciliation: AE subject count <= 분모, arm별 합 = 전체 합
- coding_version: MedDRA/WHODrug 버전이 DMP 명시와 일치하는가 (AE 관련 표)
```

### 3단계: Data Dictionary 확인

`docs/data_dictionary.md`에서 사용할 변수의 실제 이름·타입·값 도메인을 확인합니다.
없으면 먼저 `/build-dictionary`를 실행하십시오.

### 4단계: 프로그램 생성

`mcp__local-coder__write_program`을 호출합니다.

```
language:        (선택한 언어)
program_name:    {$1}.{sas|py|R}
purpose:         (SAP의 표 제목)
sap_ref:         docs/sap.md#{$1}
table_shell:     (1단계에서 추출한 table shell 전문)
data_dictionary: (3단계에서 확인한 관련 변수 부분만)
assertions_spec: (2단계 YAML)
inputs:          (사용할 데이터셋 상대경로 목록)
outputs:         (생성할 산출물 상대경로)
analysis_set:    (예: Safety Set (SAFFL='Y'))
instructions:    (SAP에서 특별히 주의할 점)
```

### 5단계: 코드 검토 — 저장 전 필수

생성된 코드를 **반드시 읽고 확인**하십시오. 자동으로 저장하지 마십시오.

확인 항목:

| 항목 | 확인 내용 |
|---|---|
| GXPLLM-META | `inputs` / `outputs`가 실제와 일치하는가 |
| 분석군 | SAP의 flag 변수와 값을 정확히 썼는가 |
| 카운트 단위 | subject / event를 혼동하지 않았는가 |
| 병합 | 관계를 명시했는가 (SAS는 행 수 검증, pandas는 `validate=`, dplyr는 `relationship=`) |
| assertion | 2단계에서 도출한 규칙이 모두 들어갔는가 |
| 하드코딩 | 실제 피험자 ID, 실제 수치, 자유기술 텍스트가 리터럴로 박혀 있지 않은가 |
| 출력 경로 | `output/tables/` 또는 `output/figures/`로 가는가 (Listing이면 `output/listings/`) |

**하드코딩된 실제 값을 발견하면 저장하지 말고 재생성하십시오.** 코드는 Opus가 읽을 수 있는
영역이므로, 코드에 박힌 데이터 값은 경계를 우회하는 유출 경로가 됩니다.

문제가 없으면 `programs/{language}/{$1}.{확장자}`에 저장합니다.

### 6단계: 실행 안내

사용자에게 실행 명령을 안내합니다.

```bash
python scripts/run_sas.py --program programs/sas/{$1}.sas --purpose exploratory
```

제출 후보라면 `--purpose submission_candidate`를 씁니다. 이 경우:
- WARNING과 CRITICAL NOTE도 실패로 처리됩니다
- Python/R은 환경 잠금(`uv.lock` / `renv.lock`)이 필수입니다
- Independent Programming(`/qc-program`)이 별도로 필요합니다

## 주의

- 원본 데이터를 읽어 확인하려 하지 마십시오. hook이 차단합니다.
- 데이터 구조가 궁금하면 `docs/data_dictionary.md`를 보거나 `/build-dictionary`를 실행하십시오.
- 실행 후 `stdout.txt`나 `execution.log`를 읽을 수 없습니다. `assertions.json`과 manifest를 보십시오.
