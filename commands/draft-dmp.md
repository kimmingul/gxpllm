---
description: Protocol과 Data Dictionary로 DMP(자료관리계획서) 초안을 작성합니다
argument-hint: [특정 섹션 또는 비워두면 전체]
allowed-tools: Read, Write, Glob
---

# DMP 초안 작성

**이 작업에는 피험자 데이터가 필요하지 않습니다.** Opus가 직접 수행합니다.

## 범위

$1 (비어 있으면 전체)

## 입력

- `docs/protocol.md` — 임상시험계획서
- `docs/data_dictionary.md` — 실제 데이터 구조 (없으면 `/build-dictionary` 먼저)
- CRF (있으면)

## 표준 목차

```
1.  개요 및 범위
2.  역할과 책임
3.  자료 흐름
4.  CRF 설계 및 데이터베이스 구조
5.  데이터 입력 및 검증 (edit check)
6.  Query 관리
7.  의학용어 코딩
8.  외부 데이터 통합
9.  SAE 조정 (reconciliation)
10. Database Lock
11. 자료 보관
12. 품질관리
```

## 통계 분석과 직결되는 절

### 4. 데이터베이스 구조

`docs/data_dictionary.md`의 내용을 반영합니다.

- 데이터셋 목록과 각각의 단위 (피험자 단위 / 방문 단위 / 이벤트 단위)
- key 구조
- 데이터셋 간 관계
- SDTM/ADaM 표준 준수 여부 (준수하지 않으면 사내 표준 명시)

**Data Dictionary의 `추정 의미`가 아직 검토(⬜)되지 않았다면 명시하십시오.**

### 5. Edit check

분석 단계의 assertion과 대응합니다. 여기서 잡히지 않으면 분석에서 잡아야 합니다.

| 유형 | 예 |
|---|---|
| 필수 항목 | 필수 필드 결측 |
| 범위 | 생리적으로 불가능한 값 |
| 논리 | 날짜 순서, 방문 순서 |
| 일관성 | 성별과 임신 여부 |
| 중복 | key 중복 |

### 7. 의학용어 코딩 — **버전을 정확히 명시하십시오**

```
MedDRA  : 27.0
WHODrug : GLOBALB3Mar26
```

**이 버전은 `/derive-assertions`가 assertion으로 변환하고, 분석 프로그램이 검증합니다.**
데이터에 기록된 버전과 DMP 명시가 다르면 실패로 처리됩니다.

코딩 시점에 버전이 바뀌면 DMP를 개정하고 재코딩 범위를 명시해야 합니다.

### 9. SAE reconciliation

안전성 데이터베이스와 임상 데이터베이스의 SAE 대조 절차.
불일치 해소 규칙이 분석 결과에 직접 영향을 줍니다.

### 10. Database Lock

- Lock 전 확인 목록
- **눈가림 해제 시점** — `.gxpllm/config.json`의 `blinded`를 언제 `false`로 바꿀지
- Lock 후 데이터 수정 절차 (unlock 조건)

## 이 시스템에 특화된 절 — 추가 권장

기존 DMP 템플릿에 없더라도 다음을 넣는 것을 권합니다.
QA와 감사 대응에 필요합니다.

```markdown
## 13. 분석 환경 및 LLM 활용

### 13.1 역할 분담
- 계획 문서 작성 및 결과 검토: Claude Opus (상용 LLM)
- 분석 코드 작성: Qwen3.6-35B-A3B (사내 DGX Spark, 로컬)
- 계산: SAS 9.4 / Python / R
- 검증 및 승인: 통계 프로그래머, 생물통계가

### 13.2 데이터 경계
상용 LLM은 임상 데이터에 접근하지 않는다. PreToolUse hook으로 강제한다.
- 접근 가능: Protocol, SAP, DMP, Data Dictionary, 코드, Table, Figure
- 접근 불가: 원본/파생 데이터셋, Listing, 실행 로그 원문

### 13.3 감사 증적
모든 분석 실행은 runner를 경유하며 다음이 자동 기록된다.
- 입력 데이터셋 SHA-256
- 실행 로그
- assertion 결과
- manifest.json
- HMAC-SHA256 해시 체인 (audit/audit.jsonl)

### 13.4 검증 범위
검증 대상은 생성된 코드이며 LLM 자체가 아니다.
LLM이 작성한 코드는 사람 프로그래머가 작성한 코드와 동일한 검토·테스트·
Independent Programming 절차를 거친다.

### 13.5 알려진 한계
docs/development.md §12 참조. CSV 문서에 함께 기재한다.
```

## 출력

`docs/dmp.md`

```markdown
# 자료관리계획서 — {STUDY_ID}

버전: 0.1 (초안)
작성: Claude Opus (초안) — **데이터 매니저 검토 필수**
근거 문서: docs/protocol.md, docs/data_dictionary.md
작성일: {날짜}

> 이 문서는 LLM이 작성한 초안입니다. 승인 전까지 운영에 사용하지 마십시오.

## 확인이 필요한 항목

- [ ] (판단할 수 없었던 항목)
```

## 주의

- 코딩 사전 버전은 **반드시 확인 후** 기재하십시오. 추측하면 분석이 실패합니다.
- Protocol과 불일치하는 부분을 발견하면 명시하고 확인을 요청하십시오.
