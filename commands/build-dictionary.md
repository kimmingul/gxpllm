---
description: Data Dictionary가 없는 의뢰 건에서 데이터를 프로파일링해 Data Dictionary 초안을 작성합니다
argument-hint: [데이터셋 경로 또는 비워두면 data/raw 전체]
allowed-tools: mcp__local-coder__profile_data, mcp__local-coder__write_program, Bash, Read, Write, Glob
---

# Data Dictionary 생성

분석 의뢰 건마다 데이터 구조가 다르고 Data Dictionary가 없는 경우가 많습니다.
이 명령은 데이터를 프로파일링해 Data Dictionary 초안을 만듭니다.

**완성된 Data Dictionary는 피험자 데이터를 담지 않으므로, 이후 SAP 작성 시 참조할 수 있습니다.**

## 대상

$1 (비어 있으면 `data/raw` 전체)

## 절차

### 1단계: 대상 데이터셋 확인

`Glob`으로 `data/` 아래 파일 **목록만** 확인합니다.
파일 내용은 읽을 수 없습니다 (hook이 차단합니다). 파일명과 확장자로 형식만 파악하십시오.

확장자로 언어를 정합니다.
- `.sas7bdat`, `.xpt` → SAS
- `.csv`, `.parquet`, `.xlsx` → Python
- `.rds`, `.rdata` → R

### 2단계: 프로파일링 프로그램 생성

`mcp__local-coder__profile_data`를 호출합니다.

```
language: (1단계에서 정한 언어)
datasets: (대상 데이터셋 상대경로 목록)
```

생성된 코드를 `programs/{language}/profile_data.{확장자}`에 저장합니다.

**저장 전 반드시 코드를 검토하십시오.** 확인할 것:
- GXPLLM-META 블록의 `inputs` / `outputs`가 정확한가
- 피험자 식별 값(원본 ID, 자유기술 원문)을 출력에 포함하지 않는가
- 출력이 `output/tables/profile.json`으로 가는가

### 3단계: 실행

```bash
python scripts/run_python.py --program programs/python/profile_data.py --purpose exploratory
```

(SAS면 `run_sas.py`, R이면 `run_r.py`)

실행 결과는 `logs/runs/{run_id}/assertions.json`과 `output/tables/profile.json`에서 확인합니다.
**`stdout.txt`나 `execution.log`는 읽을 수 없습니다** (프로그램 출력에 데이터 값이 섞일 수 있음).

실패하면 `assertions.json`의 실패 항목과 manifest의 `log_scan.findings`를 근거로
`mcp__local-coder__revise_program`을 호출해 수정합니다.

### 4단계: Data Dictionary 초안 작성

`output/tables/profile.json`을 읽고 `docs/data_dictionary.md`를 작성합니다.

형식:

```markdown
# Data Dictionary — {STUDY_ID}

생성일: {날짜}
생성 방법: 자동 프로파일링 + 사람 검토
프로파일 run_id: {run_id}

## 데이터셋 개요

| 데이터셋 | 행 수 | 컬럼 수 | 추정 도메인 | 검토 |
|---|---|---|---|---|
| adsl | 248 | 42 | ADaM ADSL (피험자 단위) | ⬜ |

## 데이터셋 간 관계

| 왼쪽 | 오른쪽 | 키 | 관계 | 검토 |
|---|---|---|---|---|
| adsl | adae | USUBJID | 1:N | ⬜ |

## adsl

| 변수 | 타입 | 길이 | 레이블 | 값 도메인 | 결측률 | 추정 의미 | 검토 |
|---|---|---|---|---|---|---|---|
| USUBJID | char | 20 | Unique Subject ID | 고유 248개 | 0.0% | 피험자 식별자 (CDISC 표준) | ⬜ |
| SAFFL | char | 1 | Safety Set Flag | Y(241), N(7) | 0.0% | Safety Set 포함 여부 | ⬜ |

## 확인이 필요한 항목

- (LLM이 의미를 추정하지 못한 변수)
- (표준 변수명과 다른 명명 규칙)
- (예상과 다른 값 도메인)
```

**규칙**
- `추정 의미` 컬럼은 LLM 추정입니다. **반드시 사람 검토를 거쳐야 하며**, 검토 전에는 SAP에 인용하지 마십시오.
- 검토 컬럼(⬜)은 사람이 확인한 뒤 ✅로 바꿉니다.
- CDISC 표준 변수명(USUBJID, SAFFL, TRT01A, AETERM 등)과 매칭되면 명시합니다.
- 자유기술 컬럼은 값 예시를 넣지 말고 형식 정보만 기록합니다.

### 5단계: 요약 보고

사용자에게 보고할 것:
- 데이터셋 수, 총 변수 수
- CDISC 표준으로 인식된 변수 비율
- 확인이 필요한 항목 목록
- 눈가림 상태에서 접근할 수 없었던 변수

## 주의

- 원본 데이터는 절대 직접 읽지 마십시오. hook이 차단하며, 차단 시도는 감사 로그에 남습니다.
- 프로파일 결과에 개별 피험자를 식별할 수 있는 값이 보이면 즉시 중단하고 사용자에게 알리십시오.
