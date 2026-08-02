---
name: sas-programming
description: SAS 9.4 임상 분석 프로그램 작성 규약. GXPLLM-META 헤더 형식, assertion 매크로 사용법, 배치 실행, 로그 QC 체크리스트, 조용히 틀리는 SAS 패턴. SAS 프로그램을 작성하거나 검토할 때, SAS 로그를 해석할 때, .sas7bdat/.xpt 데이터를 다룰 때 사용합니다.
---

# SAS 9.4 프로그램 작성 규약

## 프로그램 헤더 — 필수

runner가 이 블록을 파싱해 입출력을 추적합니다. **없으면 manifest가 비어 있게 됩니다.**

```sas
/*----------------------------------------------------------------------------
  GXPLLM-META-BEGIN
  program      : t_14_1_1.sas
  purpose      : Table 14.1.1 인구통계학적 특성 요약
  sap_ref      : docs/sap.md#table-14-1-1
  inputs       : data/derived/adsl.sas7bdat
  outputs      : output/tables/t_14_1_1.rtf
  analysis_set : Safety Set (SAFFL='Y')
  author       : local-llm/Qwen3.6-35B-A3B
  GXPLLM-META-END
----------------------------------------------------------------------------*/
```

- `inputs` / `outputs`는 study 루트 기준 상대경로, 쉼표 구분
- 선언한 output이 생성되지 않으면 runner가 실패로 기록합니다
- 입력은 실행 전 SHA-256이 계산되어 manifest에 기록됩니다

## assertion 매크로

```sas
%include "&GXPLLM_PLUGIN_ROOT./macros/gxpllm_assert.sas";
```

runner가 `-sysparm`으로 `run_dir`을 전달하므로 별도 설정이 필요 없습니다.

| 매크로 | 용도 |
|---|---|
| `%gxpllm_assert_rowcount(ds, label=, expected_min=, expected_max=, expected_n=)` | 행 수 |
| `%gxpllm_assert_rowcount_delta(ds_before, ds_after, label=, max_loss_rate=, allow_increase=N)` | 변환 전후 행 수 |
| `%gxpllm_assert_unique(ds, keys=, label=)` | key 유일성 |
| `%gxpllm_assert_domain(ds, column=, allowed=%str(M F), label=)` | 값 도메인 |
| `%gxpllm_assert_missingness(ds, column=, label=, max_rate=)` | 결측률 |
| `%gxpllm_assert_date_order(ds, earlier=, later=, label=)` | 날짜 순서 |
| `%gxpllm_assert_analysis_set(ds, flag_column=SAFFL, flag_value=Y, label=, expected_n=)` | 분석군 |
| `%gxpllm_assert_denominator(ds, subject_column=USUBJID, denominator=, label=)` | 분모 |
| `%gxpllm_assert_le(actual, limit, label=, expr=)` | 정합성 관계식 |

`strict=Y`를 주면 실패 시 즉시 중단합니다. 기본은 계속 진행하며 전체 결과를 모읍니다.

## 표준 구조

```sas
/* GXPLLM-META 블록 */

%include "&GXPLLM_PLUGIN_ROOT./macros/gxpllm_assert.sas";

/* --- 1. 라이브러리 (반드시 읽기 전용) --------------------------------- */
libname indata "&GXPLLM_STUDY_ROOT./data/derived" access=readonly;

/* --- 2. 입력 확인 ------------------------------------------------------ */
%gxpllm_assert_rowcount(indata.adsl, label=ADSL_LOADED, expected_min=1);
%gxpllm_assert_unique(indata.adsl, keys=USUBJID, label=ADSL_UNIQUE_SUBJ);

/* --- 3. 분석군 필터 ---------------------------------------------------- */
data saf;
    set indata.adsl;
    where SAFFL = 'Y';
run;

%gxpllm_assert_rowcount_delta(indata.adsl, saf, label=SAFETY_FILTER, max_loss_rate=0.1);
%gxpllm_assert_analysis_set(saf, flag_column=SAFFL, flag_value=Y, label=SAFETY_SET, expected_n=241);

/* --- 4. 분석 ----------------------------------------------------------- */
/* ... */

/* --- 5. 정합성 --------------------------------------------------------- */
%gxpllm_assert_denominator(saf, denominator=241, label=DENOM_CHECK);

/* --- 6. 출력 ----------------------------------------------------------- */
ods rtf file="&GXPLLM_STUDY_ROOT./output/tables/t_14_1_1.rtf";
/* ... */
ods rtf close;
```

## 배치 실행 — runner가 담당

**직접 실행하지 마십시오.** hook이 차단합니다.

```bash
python scripts/run_sas.py --program programs/sas/t_14_1_1.sas --purpose exploratory
```

runner가 붙이는 옵션:

| 옵션 | 이유 |
|---|---|
| `-sysin` | 실행할 프로그램 |
| `-log` / `-print` | run 디렉터리에 로그와 출력 |
| `-nosplash` | 스플래시 비표시 |
| `-noterminal` | **필수** — 없으면 오류 시 배치가 멈춤 |
| `-sysparm` | `run_id`, `study_id`, `run_dir` 전달 |
| `-work` | run별 WORK 분리 (잔여물 충돌 방지) |

## 로그 QC 체크리스트 — 가장 중요

**SAS 종료 코드 0을 믿지 마십시오.** 논리적으로 치명적인 상황에서도 0을 반환합니다.

runner가 자동으로 스캔하지만, 코드를 작성할 때 이것들이 나오지 않게 해야 합니다.

### CRITICAL — 조용히 잘못된 결과를 만듦

| 로그 메시지 | 원인 | 조치 |
|---|---|---|
| `MERGE statement has more than one data set with repeats of BY values` | **다대다 MERGE** | BY 변수 확인. 임상 데이터에서 행이 조용히 늘어나는 최대 원인 |
| `Numeric values have been converted to character` | 타입 혼동 | 명시적 `put()` / `input()` |
| `Character values have been converted to numeric` | 타입 혼동 | 명시적 변환 |
| `Invalid numeric data` | 숫자 변환 실패 | 원본 값 형식 확인 |
| `Missing values were generated as a result of` | 의도치 않은 결측 | 계산식 확인 |
| `Division by zero detected` | 0 나눗셈 | 분모 검사 추가 |
| `Variable ... is uninitialized` | **변수명 오타** | Data Dictionary와 대조 |
| `W.D format was too small` | **표 값 절삭** | 포맷 폭 확대 |
| `query requires remerging summary statistics` | PROC SQL 의도치 않은 remerge | `GROUP BY` 확인 |
| `Cartesian product` | 조인 조건 누락 | ON 절 확인 |
| `Multiple lengths were specified for the variable` | 길이 불일치 | `LENGTH` 문 통일 |

### INFO — 확인 필요

| 로그 메시지 | 의미 |
|---|---|
| `There were 0 observations read` | 빈 입력 — 필터 오류 의심 |
| `has 0 observations` | 빈 출력 |
| `The SAS System stopped processing this step` | 스텝 중단 |

**`--purpose submission_candidate`에서는 CRITICAL이 하나라도 있으면 실패 처리됩니다.**

## 조용히 틀리는 SAS 패턴

### 다대다 MERGE

```sas
/* 위험 — adae에 USUBJID 중복이 있으면 행이 늘어남 */
data merged;
    merge adsl adae;
    by USUBJID;
run;

/* 안전 — 관계를 명시하고 검증 */
proc sql;
    create table merged as
    select a.*, b.AETERM, b.AEDECOD
    from adsl as a
    left join adae as b
    on a.USUBJID = b.USUBJID;
quit;

%gxpllm_assert_rowcount_delta(adae, merged, label=AE_JOIN, allow_increase=N);
```

### PROC SQL remerge

```sas
/* 위험 — 그룹 평균이 각 행에 remerge됨 */
proc sql;
    select USUBJID, AVAL, mean(AVAL) as avg from adlb;
quit;

/* 안전 */
proc sql;
    select USUBJID, mean(AVAL) as avg from adlb group by USUBJID;
quit;
```

### where vs if

```sas
/* where : SET 이전에 적용, 인덱스 사용 가능 */
data saf; set adsl; where SAFFL='Y'; run;

/* if : SET 이후 적용. 계산 변수에는 if를 써야 함 */
data saf;
    set adsl;
    agegr = ifc(AGE>=65, '>=65', '<65');
    if agegr = '>=65';
run;
```

### 결측값 비교

```sas
/* 위험 — SAS에서 결측은 음의 무한대보다 작음 */
if AVAL > 100 then flag = 'Y';   /* 결측 자동 제외 (의도했는가?) */
if AVAL < 100 then flag = 'Y';   /* 결측이 포함됨! */

/* 안전 */
if not missing(AVAL) and AVAL < 100 then flag = 'Y';
```

### 문자 길이 절삭

```sas
/* 위험 — 첫 레코드 길이로 고정되어 이후가 잘림 */
data x; set y; newvar = catx(' ', a, b, c); run;

/* 안전 */
data x; length newvar $ 200; set y; newvar = catx(' ', a, b, c); run;
```

## 인코딩

한국어 Windows 환경의 SAS 9.4는 로그를 **CP949**로 기록합니다.
runner가 자동 감지하지만, `.gxpllm/config.json`의 `sas_log_encoding`으로 지정할 수 있습니다.

## 하지 말 것

- 실제 피험자 ID, 실제 수치, 자유기술 텍스트를 코드에 하드코딩
  (코드는 Opus가 읽을 수 있어 경계 우회 경로가 됩니다)
- `libname`을 읽기 전용 없이 열기
- `output/` 밖에 산출물 쓰기
- assertion 없이 데이터 변환
