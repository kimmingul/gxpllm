---
description: 분석 프로그램을 runner로 실행하고 결과를 검토합니다
argument-hint: <프로그램 경로> [exploratory|qc|submission_candidate]
allowed-tools: Bash, Read, Glob, mcp__local-coder__revise_program, Write
---

# 분석 프로그램 실행

## 대상

프로그램: $1
purpose: $2 (미지정이면 `exploratory`)

## purpose 선택

| purpose | 실패 조건 | 용도 |
|---|---|---|
| `exploratory` | ERROR만 | 탐색적 분석, 데이터 확인 |
| `qc` | ERROR + WARNING | Independent Programming 대조 |
| `submission_candidate` | ERROR + WARNING + CRITICAL NOTE | 제출 경로. 환경 잠금 필수 |

**모든 purpose 에서 assertion 0건은 실패입니다.**

assertion 이 0건이면 "검증이 통과했다" 가 아니라 "검증이 없었다" 입니다.
프로그램이 중간에서 잘렸거나 assertion 호출이 빠지면 남은 assertion 이 없으므로
실패할 것도 없어집니다. 이 상태를 PASSED 로 두면 안전장치가 사라진 것을
아무도 모릅니다.

assertion 이 필요 없는 작업(프로파일링 등)은 `--allow-no-assertions` 를 붙입니다.
`qc` 와 `submission_candidate` 에서는 이 플래그가 통하지 않습니다.
사용하면 manifest 와 감사 로그에 기록됩니다.

```bash
python scripts/run_python.py --program programs/python/profile.py --allow-no-assertions
```

붙이기 전에 **프로그램이 잘리지 않았는지 먼저 확인하십시오.** 대부분의 경우
assertion 0건은 의도가 아니라 사고입니다.

## 절차

### 1단계: 실행

```bash
python scripts/run_sas.py    --program $1 --purpose {purpose}
python scripts/run_python.py --program $1 --purpose {purpose}
python scripts/run_r.py      --program $1 --purpose {purpose}
```

프로그램 확장자로 runner를 고릅니다 (`.sas` → run_sas, `.py` → run_python, `.R` → run_r).

runner가 자동으로 남기는 것:
- 입력 데이터셋 SHA-256
- 실행 로그 (SAS는 `.log`/`.lst`, Python/R은 runner가 생성)
- assertion 결과
- `manifest.json`
- 감사 로그 해시 체인 항목

### 2단계: 결과 확인

**읽을 수 있는 것**
- `logs/runs/{run_id}/manifest.json` — 실행 메타데이터, 로그 스캔 요약, 판정
- `logs/runs/{run_id}/assertions.json` — assertion 결과 전체
- `output/tables/`, `output/figures/` — 산출물

**읽을 수 없는 것 (hook이 차단)**
- `stdout.txt`, `stderr.txt`, `execution.log`, `execution.lst`
- 프로그램 출력에 피험자 데이터가 섞일 수 있기 때문입니다

manifest에서 확인할 항목:

```
result                             PASSED / FAILED
exit_code                          SAS는 0이어도 로그 스캔을 함께 봐야 함
log_scan.counts                    ERROR / WARNING / CRITICAL / INFO 건수
log_scan.findings                  규칙 ID와 발생 위치 (내용은 절단됨)
assertions.total                   기록된 assertion 총 건수 — 0이면 검증이 없었던 것
assertions.passed / .failed        통과/실패 수
assertions.no_assertions_allowed   --allow-no-assertions 사용 여부
missing_outputs                    선언했는데 생성되지 않은 산출물
failure_reasons                    실패 사유
```

`assertions.total` 을 먼저 보십시오. 기대한 assertion 수보다 적으면
프로그램이 잘렸을 수 있습니다.

### 3단계: 실패 시 수정

**SAS에서 특히 주의할 CRITICAL NOTE**

| 규칙 ID | 의미 | 조치 |
|---|---|---|
| `MERGE_REPEAT_BY` | 다대다 MERGE — 행이 조용히 늘어남 | BY 변수 확인, PROC SQL join으로 전환 검토 |
| `NUM_TO_CHAR` / `CHAR_TO_NUM` | 타입 혼동 | 명시적 변환 추가 |
| `INVALID_NUMERIC` | 숫자 변환 실패 | 원본 값 형식 확인 |
| `MISSING_GENERATED` | 의도치 않은 결측 발생 | 계산식 확인 |
| `UNINITIALIZED` | 변수명 오타 | Data Dictionary와 대조 |
| `SQL_REMERGE` | PROC SQL 의도치 않은 remerge | GROUP BY 확인 |
| `FORMAT_TOO_SMALL` | 표 값 절삭 | 포맷 폭 확대 |

**Python에서 주의할 CRITICAL**

| 규칙 ID | 의미 |
|---|---|
| `SETTING_WITH_COPY` | 뷰에 대입 — 원본이 안 바뀜 |
| `DTYPE_WARNING` | 컬럼 타입이 섞임 |
| `NUMPY_ALL_NAN` | 빈 슬라이스 평균, 자유도 0 |

**R에서 주의할 CRITICAL**

| 규칙 ID | 의미 |
|---|---|
| `NAS_INTRODUCED` | 강제 변환으로 NA 발생 |
| `MANY_TO_MANY` | dplyr 다대다 조인 |
| `LONGER_OBJECT` | 벡터 길이 불일치 재활용 |

수정이 필요하면 `mcp__local-coder__revise_program`을 호출합니다.

```
language:           (프로그램 언어)
source:             (현재 프로그램 전문)
assertion_failures: (assertions.json의 실패 항목)
log_findings:       (manifest의 log_scan.findings)
error_summary:      (manifest의 environment.sanitized_error)
```

**원본 traceback이나 데이터 값을 넘기지 마십시오.** manifest에 담긴 정제된 정보만 씁니다.

수정 후 5단계 검토(→ `/write-program` 참조)를 거쳐 저장하고 재실행합니다.

### 4단계: 반복 제한

**자동 수정은 3회까지만 시도하십시오.** 3회 안에 통과하지 못하면 중단하고 사용자에게 보고합니다.

반복할수록 더 많은 맥락을 요구하게 되고, 그 과정에서 데이터가 경계를 넘을 압력이 생깁니다.
막히면 사람이 로컬 화면에서 직접 확인하는 것이 맞습니다.

### 5단계: 보고

사용자에게 보고할 것:
- 판정 (PASSED / FAILED)과 run_id
- assertion 통과/실패 요약
- 로그 스캔에서 나온 CRITICAL 항목
- 생성된 산출물 경로
- 수정 횟수

## 주의

- runner를 거치지 않고 `sas.exe`나 `python`을 직접 실행할 수 없습니다. hook이 차단합니다.
- 차단 시도는 감사 로그에 `access_blocked`로 남습니다.
