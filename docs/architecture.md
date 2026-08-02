# 아키텍처

## 문제

임상시험 데이터를 LLM 으로 분석하고 싶은데, 상용 LLM API 를 쓰면 데이터가
외부로 나갑니다. 그렇다고 로컬 LLM 만 쓰면 문제해결 능력이 부족합니다.

## 해결

**역할을 나눕니다.**

```
┌─────────────────── 직원 PC ───────────────────┐
│                                                │
│  Claude Code (Opus)                            │
│    Protocol / SAP / DMP 작성                   │
│    Table·Figure 검토, CSR 문구 초안            │
│    ★ 임상 데이터 접근 불가 (hook 차단)         │
│         │                                      │
│         │ MCP                                  │
│         ▼                                      │
│  로컬 LLM ──► SAS / Python / R 코드            │
│         │                                      │
│         ▼                                      │
│  runner ──► 실행 + 감사 증적                   │
│         │                                      │
│         ▼                                      │
│  임상 데이터 (PC 를 벗어나지 않음)             │
└────────────────────────┬───────────────────────┘
                         │ 사내망
                ┌────────▼────────┐
                │  DGX Spark      │
                │  vLLM (Qwen3.6) │
                └─────────────────┘
```

**불변 원칙: LLM 은 숫자를 만들지 않습니다.** 계산은 SAS/Python/R 만 합니다.
LLM 은 코드를 쓰고 문서를 씁니다.

---

## 왜 이 구조인가

### 기존 방식과의 대응

이것은 새로운 시스템이 아니라 **도구 교체**입니다.

| 기존 | gxpllm |
|---|---|
| Medical Writer 가 Protocol·SAP·DMP 작성 | Claude 가 초안, 사람이 검토·승인 |
| 통계 프로그래머가 table shell 보고 SAS 코드 작성 | 로컬 LLM 이 코드 작성 |
| 직원 PC 에서 SAS 실행, `.log` 생성 | runner 가 실행, 로그·manifest 생성 |
| 통계책임자가 TLF 검토 | Claude 가 검토 보조, 사람이 판정 |
| Independent programmer 가 double programming | 그대로 유지 (사람이 원칙) |

DGX Spark 는 과거 SAS 서버가 있던 자리와 같은 위치의 **사내 LLM 서버**입니다.

### 규제상 프레이밍

**검증 대상은 LLM 이 아니라 분석 파이프라인입니다.**

Opus 나 Qwen3.6 을 밸리데이션할 방법은 없습니다. 시도하면 실패합니다.
하지만 그럴 필요가 없습니다 — 컴파일러나 IDE 를 밸리데이션하지 않듯,
**코드 작성을 돕는 도구는 GxP 시스템이 아닙니다.**

검증 대상은 산출된 R/SAS 스크립트이고, 그건 사람 프로그래머가 짠 코드와
**동일한 리뷰·테스트·double programming 절차**를 거칩니다.

이 선을 넘는 순간 — LLM 출력이 사람 검토 없이 숫자가 되는 순간 —
밸리데이션 부담이 감당 불가능해집니다. **선은 여기 하나만 그으면 됩니다.**

---

## 경계 설계

### 기본 거부 (default-deny)

study 루트 안은 **허용 목록에 있는 것만** 읽을 수 있습니다.

차단 목록(blocklist) 방식은 실패했습니다. 적대적 검토에서 다음이 확인됐습니다.

```
1. Claude 가 programs/ 에 데이터를 읽는 프로그램 작성   ← programs/ 는 허용
2. runner 로 실행                                        ← runner 는 허용
3. logs/runs/{run_id}/stdout.txt 읽기                    ← 차단 목록에 없었음
```

차단 규칙을 우회할 필요조차 없었습니다.
산출물 경로가 늘어날 때마다 목록에 추가하는 방식은 반드시 누락됩니다.

### 정책은 코드에만

`allowed_dirs`, `allowed_log_files`, `blocked_extensions` 는
`gxpllm/core.py` 에만 존재하며 `.gxpllm/config.json` 의 같은 키를 무시합니다.

`.gxpllm/` 이 Claude 의 읽기 허용 영역인 이상, 설정을 신뢰하면
`allowed_dirs` 에 `data` 를 추가해 정책 전체를 무력화할 수 있습니다.

### 읽기와 쓰기를 분리

| 경로 | 읽기 | 쓰기 | 쓰기를 막는 이유 |
|---|---|---|---|
| `docs/` `programs/` `macros/` `spec/` `validation/` | 가능 | 가능 | — |
| `.gxpllm/` | 가능 | 차단 | 정책 자가 확장 방지 |
| `audit/` | 가능 | 차단 | 차단 기록 말소 방지 |
| `logs/` | 일부 | 차단 | manifest 위조 방지 |
| `output/tables/` `output/figures/` | 가능 | 차단 | runner 만 산출물을 만든다 |

### 목록이 아니라 구조로

적대적 검토를 거치며 세 곳을 목록 기반에서 구조 기반으로 바꿨습니다.

| 판정 | 목록 방식 (실패) | 구조 방식 (현재) |
|---|---|---|
| 인터프리터 | 접두 래퍼 목록 (`env`, `timeout`, …) | 모든 토큰 검사 + **경로 구분자로 실행파일 판별** |
| 읽기/쓰기 도구 | 도구명 부분 문자열 | **정확히 일치** + 쓰기 표지 우선 |
| git 하위명령 | 정규식 `git\s+show` | **전역 옵션 건너뛴 뒤 토큰 대조** |

목록은 끝없이 늘어나고 반드시 빠뜨립니다. 구조는 그렇지 않습니다.

---

## 감사 증적

### 자동 기록

runner 가 남깁니다. 사용자가 의식할 필요가 없습니다.

```
logs/runs/{run_id}/
  manifest.json      입출력 SHA-256, 로그 스캔, assertion 요약, 판정
  assertions.json    검증 결과 전체
  execution.log      SAS .log 또는 runner 생성 로그
  execution.lst      SAS 출력
  stdout.txt / stderr.txt

audit/audit.jsonl    HMAC-SHA256 해시 체인 (append-only)
```

### HMAC 키를 밖에 두는 이유

키가 study 안에 있으면 `audit.jsonl` 을 통째로 재작성한 뒤 같은 키로
다시 서명해 검증을 통과시킬 수 있습니다.
키를 `~/.gxpllm/audit.key` 에 두면 study 디렉터리만 조작해서는 위조가 성립하지 않습니다.

또한 키가 있으면 **모든 항목이 HMAC 이어야** 합니다.
항목이 자기 `hash_alg` 를 고르게 두면 전체를 `sha256` 으로 다운그레이드해
키 없이 재서명할 수 있습니다.

### 남는 한계

- **후미 절단은 탐지되지 않습니다.** 체인이 선형이라 마지막 N 개를 지우면
  남은 부분은 여전히 유효합니다. 백업본과 대조해야만 알 수 있습니다.
- 같은 PC 의 같은 사용자는 키 파일에도 접근할 수 있습니다.

---

## 조용히 틀리는 것을 잡는 법

기계적 무결성 검증(행 수, 결측률)만으로는
**"기계적으로 완전하지만 임상적으로 틀린 분석"** 을 막지 못합니다.

모집단 정의, 분모, subject vs event 카운트, TEAE window, baseline 규칙이
틀려도 assertion 은 전부 통과합니다.

그래서 **SAP 를 코드의 명세로** 씁니다.

```
docs/sap.md (사람 승인)
    ↓  /derive-assertions
docs/assertions_spec.yaml (기계 판독 가능)
    ↓  로컬 LLM 이 코드 생성 시 참조
programs/*.{sas,py,R}  ← assertion 호출 포함
    ↓  실행
logs/runs/{run_id}/assertions.json
```

Claude 는 **결과가 아니라 assertion 목록을 검토**합니다.
데이터 없이도 "cohort flow 검사가 빠졌네요"는 정확히 지적할 수 있습니다.

---

## 위협 모델

이 시스템의 목적은 **실수 방지와 감사 증적 확보**입니다.

막는 것
- 실수로 데이터를 읽는 것
- runner 를 거치지 않아 증적이 없는 실행
- 감사 기록의 우발적 손상과 미숙한 편집
- 조용히 잘못된 분석 (assertion 이 잡는 범위 안에서)

막지 못하는 것
- 적대적 내부자
- 프롬프트 인젝션된 모델이 정당한 도구를 조합하는 것
  (`programs/` → runner → `output/tables/` 덤프는 **탐지만** 가능)
- 사람이 화면을 보고 직접 붙여넣는 것
- 감사 로그 후미 절단

전체 목록은 [개발문서 §12](development.md) 에 있습니다.
**CSV 문서와 SOP 에 그대로 옮겨야 합니다.**
