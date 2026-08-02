---
description: Table/Figure를 검토하고 CSR 문구 초안을 작성합니다
argument-hint: <산출물 경로 또는 table/figure ID>
allowed-tools: Read, Write, Glob
---

# 산출물 검토 및 CSR 문구 작성

생성된 Table/Figure를 검토하고 CSR(임상시험결과보고서) 문구 초안을 작성합니다.

## 대상

$1

## 읽을 수 있는 것 / 없는 것

| 구분 | 가능 여부 | 이유 |
|---|---|---|
| `output/tables/` | **가능** | 집계값. CSR에 실리는 것과 동일 수준 |
| `output/figures/` | **가능** | 집계 그래픽 |
| `output/listings/` | **불가** | **피험자 단위 데이터** |
| `logs/runs/*/manifest.json` | 가능 | 구조화 메타데이터 |
| `logs/runs/*/assertions.json` | 가능 | 검증 결과 |
| `logs/runs/*/stdout.txt`, `execution.log` | **불가** | 프로그램 출력에 데이터 값 포함 가능 |
| `data/` | **불가** | 원본 데이터 |

Listing 검토가 필요하면 사용자에게 요청하십시오. 직접 읽을 수 없습니다.

## 절차

### 1단계: 검증 상태 확인 — 검토보다 먼저

`logs/runs/{run_id}/manifest.json`과 `assertions.json`을 읽습니다.

**assertion이 실패했거나 로그에 CRITICAL이 있으면 산출물 검토를 진행하지 마십시오.**
숫자를 해석하기 전에 그 숫자가 신뢰할 수 있는지부터 확인해야 합니다.

확인할 것:
- `result`가 `PASSED`인가
- `assertions.failed`가 0인가
- `log_scan.counts`에 ERROR/CRITICAL이 없는가
- `missing_outputs`가 비어 있는가
- `purpose`가 무엇인가 (exploratory 결과를 규제 결론에 쓰지 마십시오)

### 2단계: SAP 대조

`docs/sap.md`의 해당 table shell과 실제 산출물을 대조합니다.

| 확인 | 내용 |
|---|---|
| 분석군 | 표 머리의 분석군 표기가 SAP와 일치하는가 |
| 분모 | N 값이 SAP 정의와 일치하는가 |
| 행/열 구조 | table shell과 같은가 |
| 라벨 | SAP 표기와 일치하는가 |
| 반올림 | 지정된 소수 자릿수인가 |

### 3단계: 정합성 육안 확인

assertion이 잡지 못하는 것을 봅니다.

- 치료군별 합이 전체와 맞는가
- 백분율 합이 100%인가 (반올림 오차 범위 안인가)
- N이 분모보다 큰 셀이 없는가
- 빈 셀이 의도한 것인가 (0인지 결측인지 구분되는가)
- 극단값이 있는가 (단위 오류 의심)
- 소규모 셀(n=1, 2)이 있는가 — **재식별 위험 검토 필요**

### 4단계: CSR 문구 초안 — placeholder 방식

**숫자를 직접 써넣지 마십시오.** placeholder를 쓰고 값은 산출물에서 주입합니다.

```markdown
안전성 분석군 {{n_safety}}명 중 {{n_teae_subj}}명({{pct_teae_subj}}%)에서
치료 중 발생 이상반응이 보고되었다.

가장 흔한 이상반응은 {{top_pt_1}}({{n_top_pt_1}}명, {{pct_top_pt_1}}%)이었으며,
{{top_pt_2}}({{n_top_pt_2}}명, {{pct_top_pt_2}}%)가 뒤를 이었다.

중대한 이상반응은 {{n_sae_subj}}명({{pct_sae_subj}}%)에서 발생하였다.
```

이어서 placeholder 매핑표를 함께 출력합니다.

```yaml
source_run_id: 20260802T143012-a3f9c1
source_table: output/tables/t_14_3_1.rtf
placeholders:
  n_safety:      { value: 241, cell: "Table 14.3.1 열 머리 N" }
  n_teae_subj:   { value: 187, cell: "Table 14.3.1 'Any TEAE' 행, Total 열" }
  pct_teae_subj: { value: 77.6, cell: "동일 셀 백분율" }
```

**이렇게 하는 이유**: 문구에 나타나는 모든 숫자가 산출물의 어느 셀에서 왔는지 추적됩니다.
검토자가 대조할 수 있고, 산출물이 갱신되면 어떤 문구를 고쳐야 하는지 알 수 있습니다.

### 5단계: 서술 원칙

- 표에 있는 것만 서술합니다. 표에 없는 비교나 추론을 넣지 마십시오.
- 인과관계를 단정하지 마십시오 ("~로 인해" 대신 "~에서 보고되었다").
- 통계적 유의성을 SAP에 없는 방식으로 언급하지 마십시오.
- 탐색적 분석 결과는 **탐색적임을 명시**하십시오.
- 눈가림 상태라면 치료군 간 비교를 서술하지 마십시오.

## 출력

`docs/csr_draft/{$1}.md`

문서 머리에 다음을 넣습니다.

```markdown
> 이 문구는 LLM이 작성한 초안입니다.
> 모든 숫자는 placeholder이며 산출물에서 주입되어야 합니다.
> 메디컬 라이터와 생물통계가 검토 전까지 사용하지 마십시오.
>
> 근거 run_id: {run_id}
> 근거 산출물: {산출물 경로}
> purpose: {exploratory | qc | submission_candidate}
```

## 보고

사용자에게 보고할 것:
- 검증 상태 (assertion, 로그 스캔)
- SAP 대조 결과 (불일치 항목)
- 정합성 육안 확인에서 발견한 것
- **소규모 셀 유무** (재식별 위험)
- 작성한 문구와 placeholder 매핑
