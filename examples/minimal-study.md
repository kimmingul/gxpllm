# 최소 예제 — 인구통계 요약표 하나

Data Dictionary 없이 받은 의뢰 건에서 Table 14.1.1 하나를 내는 전체 흐름입니다.
**합성 데이터로 먼저 연습하십시오.**

---

## 0. study 준비

```bash
python scripts/init_study.py --root D:\clinical\DEMO-001 --study-id DEMO-001
```

원본 데이터를 `D:\clinical\DEMO-001\data\raw\` 에 두고,
Protocol 을 `docs\protocol.md` 에 둡니다.

```bash
cd D:\clinical\DEMO-001\programs
claude
```

---

## 1. 데이터가 뭔지 파악한다

```
/build-dictionary
```

일어나는 일:

1. Claude 가 `Glob` 으로 `data/` 의 **파일 목록만** 확인 (내용은 hook 이 차단)
2. 로컬 LLM 이 프로파일링 프로그램 생성
3. runner 가 실행 → `output/tables/profile.json`
4. 로컬 LLM 이 프로파일을 읽고 `docs/data_dictionary.md` 초안 작성

산출물:

```markdown
| 변수 | 타입 | 값 도메인 | 결측률 | 추정 의미 | 검토 |
|---|---|---|---|---|---|
| USUBJID | char(20) | 고유 248개 | 0.0% | 피험자 식별자 (CDISC 표준) | ⬜ |
| SAFFL | char(1) | Y(241), N(7) | 0.0% | Safety Set 포함 여부 | ⬜ |
| AGE | num | 22–78 | 0.0% | 연령 (세) | ⬜ |
| SEX | char(1) | M(130), F(118) | 0.0% | 성별 | ⬜ |
```

**⬜ 를 ✅ 로 바꾸는 것은 사람의 일입니다.**
`추정 의미` 는 LLM 추정이므로 검토 전에는 SAP 에 인용하지 마십시오.

---

## 2. SAP 초안

```
/draft-sap
```

Claude 가 Protocol + Dictionary 로 SAP 를 씁니다.
**피험자 데이터가 필요 없는 작업이므로 Claude 가 직접 합니다.**

중요한 것은 table shell 에 다음이 **명시**되는 것입니다.

```yaml
분석군    : Safety Set (SAFFL='Y')
분모      : Safety Set 의 unique USUBJID
카운트 단위: subject          # event 가 아님 — 이 한 줄이 표를 바꿉니다
결측 처리 : 별도 범주로 표시
반올림    : 소수 1자리
```

여기서 모호하면 그대로 잘못된 분석이 됩니다.
SAP 에 없는 항목은 `## 확인이 필요한 항목` 에 남고, 통계책임자가 채웁니다.

---

## 3. 검증 규칙 도출

```
/derive-assertions t_14_1_1
```

SAP 를 기계 판독 가능한 형태로 옮깁니다.

```yaml
analysis_sets:
  safety:
    flag: SAFFL
    value: "Y"
    expected_n: 241

tables:
  t_14_1_1:
    analysis_set: safety
    count_unit: subject
    denominator: safety.unique_subjects
    reconciliation:
      - "sum(arm_counts) == overall_count"
      - "each_cell_n <= denominator"
```

---

## 4. 코드 생성

```
/write-program t_14_1_1 sas
```

로컬 LLM 이 SAS 프로그램을 씁니다.

```sas
/*----------------------------------------------------------------------------
  GXPLLM-META-BEGIN
  program      : t_14_1_1.sas
  purpose      : Table 14.1.1 인구통계학적 특성 요약
  sap_ref      : docs/sap.md#table-14-1-1
  inputs       : data/derived/adsl.sas7bdat
  outputs      : output/tables/t_14_1_1.rtf
  analysis_set : Safety Set (SAFFL='Y')
  GXPLLM-META-END
----------------------------------------------------------------------------*/

%include "&GXPLLM_PLUGIN_ROOT./macros/gxpllm_assert.sas";

libname indata "&GXPLLM_STUDY_ROOT./data/derived" access=readonly;

%gxpllm_assert_rowcount(indata.adsl, label=ADSL_LOADED, expected_min=1);
%gxpllm_assert_unique(indata.adsl, keys=USUBJID, label=ADSL_UNIQUE_SUBJ);

data saf;
    set indata.adsl;
    where SAFFL = 'Y';
run;

%gxpllm_assert_rowcount_delta(indata.adsl, saf, label=SAFETY_FILTER, max_loss_rate=0.1);
%gxpllm_assert_analysis_set(saf, flag_column=SAFFL, flag_value=Y,
                            label=SAFETY_SET, expected_n=241);
%gxpllm_assert_denominator(saf, denominator=241, label=DENOM_CHECK);

ods rtf file="&GXPLLM_STUDY_ROOT./output/tables/t_14_1_1.rtf";
proc freq data=saf;
    tables SEX * TRT01A / nocol nopercent;
run;
ods rtf close;
```

**저장 전 반드시 검토하십시오.** 특히 확인할 것:

- 실제 피험자 ID, 실제 수치가 리터럴로 박혀 있지 않은가
  (코드는 Claude 가 읽을 수 있어 경계 우회 경로가 됩니다)
- 분석군 flag 와 값이 SAP 와 같은가
- 카운트 단위가 SAP 명시와 같은가

---

## 5. 실행

```
/run-program programs/sas/t_14_1_1.sas
```

또는 직접:

```bash
python scripts/run_sas.py --program programs/sas/t_14_1_1.sas --purpose exploratory
```

runner 가 남기는 것:

```
logs/runs/20260802T143012-a3f9c1/
  manifest.json      입력 SHA-256, 로그 스캔, assertion 요약, 판정
  assertions.json    검증 결과
  execution.log      SAS .log (CP949 자동 감지)
  execution.lst      SAS 출력
```

**결과 확인은 `manifest.json` 과 `assertions.json` 으로 합니다.**
`stdout.txt` 와 `execution.log` 는 hook 이 차단합니다 — 데이터 값이 섞일 수 있습니다.

### 실패했다면

manifest 의 `log_scan.findings` 를 봅니다. SAS 는 종료 코드 0 이어도 틀릴 수 있습니다.

```json
{
  "severity": "CRITICAL",
  "rule": "MERGE_REPEAT_BY",
  "line": 142,
  "text": "NOTE: MERGE statement has more than one data set with repeats of BY values."
}
```

다대다 병합입니다. `/run-program` 이 자동으로 수정을 시도하되 **3회까지만** 합니다.

---

## 6. Independent Programming

```
/qc-program t_14_1_1
```

**원 프로그램을 참조하지 않고** 같은 SAP 명세로 다시 짭니다.

```bash
python scripts/compare_outputs.py \
    --primary output/tables/t_14_1_1.json \
    --qc      output/tables/qc_t_14_1_1.json
```

대조는 LLM 이 아니라 스크립트가 합니다.
불일치가 나오면 **어느 쪽이 맞는지는 사람이 판정**합니다.

> 제출 경로에서는 QC 를 사람이 직접 작성하는 것이 원칙입니다.
> 같은 모델이 양쪽을 쓰면 같은 오해를 두 번 하므로 double programming 이 무의미해집니다.

---

## 7. 검토와 CSR 문구

```
/review-output t_14_1_1
```

Claude 가 Table 을 읽고 CSR 문구 초안을 씁니다. **숫자는 placeholder 로만** 씁니다.

```markdown
안전성 분석군 {{n_safety}}명 중 {{n_male}}명({{pct_male}}%)이 남성이었다.
```

```yaml
source_run_id: 20260802T143012-a3f9c1
placeholders:
  n_safety: { value: 241, cell: "Table 14.1.1 열 머리 N" }
  n_male:   { value: 126, cell: "Table 14.1.1 '남' 행, Total 열" }
```

문구의 모든 숫자가 어느 셀에서 왔는지 추적됩니다.

---

## 8. 감사 증적 확인

```
/verify-audit
```

월 1회 실행하고 결과를 날짜별로 보관하십시오.

```bash
python scripts/verify_audit.py --study D:\clinical\DEMO-001
```

**`audit\` 와 `logs\` 를 사내 공유 드라이브에 일 1회 동기화하십시오.**
해시 체인의 후미 절단은 백업본과 대조해야만 탐지됩니다.

---

## 요약

| 단계 | 누가 | 데이터 접근 |
|---|---|---|
| Data Dictionary | 로컬 LLM + 사람 검토 | 로컬 LLM 만 |
| SAP | Claude 초안 + 통계책임자 승인 | 없음 |
| assertion 명세 | Claude | 없음 |
| 분석 코드 | 로컬 LLM + 사람 검토 | 로컬 LLM 만 |
| 실행 | runner (SAS/Python/R) | 있음 |
| QC | 사람 (원칙) | 있음 |
| 검토·CSR 문구 | Claude + 사람 승인 | Table/Figure 만 |
