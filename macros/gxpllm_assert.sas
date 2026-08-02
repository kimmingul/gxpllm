/*----------------------------------------------------------------------------
  gxpllm assertion 매크로 라이브러리 (SAS 9.4)

  분석 프로그램이 데이터 무결성과 임상 정합성을 검증하고 결과를 기록한다.
  결과는 assertions.jsonl 에 한 줄씩 append 되며, runner 가 이를 모아 판정한다.

  Python(gxpllm_assert.py) / R(gxpllm_assert.R) 과 동일한 출력 형식을 유지한다.

  사용:
    %include "&GXPLLM_PLUGIN_ROOT./macros/gxpllm_assert.sas";

    %gxpllm_assert_rowcount(indata.adsl, label=ADSL_LOADED, expected_min=1);
    %gxpllm_assert_unique(saf, keys=USUBJID, label=SAF_UNIQUE_SUBJ);
    %gxpllm_assert_domain(saf, column=SEX, allowed=%str(M F), label=SEX_DOMAIN);

  주의: assertion 은 실패해도 프로그램을 중단하지 않는다.
        전체 검증 결과를 모으기 위함이며, 판정은 runner 가 한다.
        즉시 중단하려면 strict=Y 를 지정한다.
----------------------------------------------------------------------------*/

/* run 디렉터리: runner 가 -sysparm 과 환경변수로 전달한다 */
%global GXPLLM_RUN_DIR GXPLLM_RUN_ID;

%macro gxpllm_init;
    /* SYSPARM 에서 run_dir 을 추출한다 (형식: run_id=X;study_id=Y;run_dir=Z) */
    %local i part;
    %if %length(&GXPLLM_RUN_DIR) = 0 %then %do;
        %do i = 1 %to %sysfunc(countw(&SYSPARM, %str(;)));
            %let part = %scan(&SYSPARM, &i, %str(;));
            %if %index(&part, run_dir=) = 1 %then
                %let GXPLLM_RUN_DIR = %substr(&part, 9);
            %if %index(&part, run_id=) = 1 %then
                %let GXPLLM_RUN_ID = %substr(&part, 8);
        %end;
    %end;

    /* 환경변수 폴백 */
    %if %length(&GXPLLM_RUN_DIR) = 0 %then
        %let GXPLLM_RUN_DIR = %sysget(GXPLLM_RUN_DIR);

    %if %length(&GXPLLM_RUN_DIR) = 0 %then %do;
        %put WARNING: [gxpllm] GXPLLM_RUN_DIR 을 확인할 수 없습니다. assertion 이 기록되지 않습니다.;
    %end;
    %else %do;
        %put NOTE: [gxpllm] assertion 기록 위치: &GXPLLM_RUN_DIR.;
    %end;
%mend gxpllm_init;

%gxpllm_init;


/*----------------------------------------------------------------------------
  내부: assertion 결과 한 줄을 assertions.jsonl 에 append
----------------------------------------------------------------------------*/
%macro gxpllm_emit(label=, rule=, result=, message=, observed=, expected=, strict=N);
    %if %length(&GXPLLM_RUN_DIR) = 0 %then %do;
        %put NOTE: [gxpllm] &result. &label.: &message.;
        %return;
    %end;

    %local esc_message esc_expected;
    /* JSON 문자열에 들어갈 큰따옴표와 역슬래시를 이스케이프 */
    %let esc_message  = %sysfunc(tranwrd(%superq(message), %str(%"), %str(')));
    %let esc_expected = %sysfunc(tranwrd(%superq(expected), %str(%"), %str(')));

    data _null_;
        length line $ 4000;
        file "&GXPLLM_RUN_DIR./assertions.jsonl" mod encoding='utf-8' lrecl=4000;
        line = cats(
            '{"label":"',    "&label",              '"',
            ',"rule":"',     "&rule",               '"',
            ',"result":"',   "&result",             '"',
            ',"language":"sas"',
            ',"message":"',  "&esc_message",        '"',
            ',"observed":"', "&observed",           '"',
            ',"expected":"', "&esc_expected",       '"',
            ',"ts":"',       put(datetime(), is8601dt.), '"',
            '}'
        );
        put line;
    run;

    %put NOTE: [gxpllm] &result. &label.: &message.;

    %if %upcase(&strict) = Y and %upcase(&result) = FAIL %then %do;
        %put ERROR: [gxpllm] assertion 실패로 중단합니다: &label.;
        %abort cancel;
    %end;
%mend gxpllm_emit;


/*----------------------------------------------------------------------------
  행 수 검증

  Args:
    ds           : 검사할 데이터셋
    label        : assertion 식별자
    expected_min : 최소 행 수
    expected_max : 최대 행 수
    expected_n   : 정확히 일치해야 할 행 수
    strict       : Y 이면 실패 시 중단
----------------------------------------------------------------------------*/
%macro gxpllm_assert_rowcount(ds, label=, expected_min=, expected_max=, expected_n=, strict=N);
    %local nobs result message expected dsid;

    %let dsid = %sysfunc(open(&ds));
    %if &dsid = 0 %then %do;
        %gxpllm_assert_emit_open_fail(&ds, &label, rowcount, &strict);
        %return;
    %end;
    %let nobs = %sysfunc(attrn(&dsid, NLOBS));
    %let dsid = %sysfunc(close(&dsid));

    %let result = PASS;
    %let message = &nobs 행;
    %let expected = ;

    %if %length(&expected_n) %then %do;
        %let expected = exact=&expected_n;
        %if &nobs ne &expected_n %then %do;
            %let result = FAIL;
            %let message = 기대 &expected_n 행이 아닌 &nobs 행;
        %end;
    %end;
    %if %length(&expected_min) %then %do;
        %let expected = &expected min=&expected_min;
        %if &nobs < &expected_min %then %do;
            %let result = FAIL;
            %let message = &nobs 행 < 최소 &expected_min 행;
        %end;
    %end;
    %if %length(&expected_max) %then %do;
        %let expected = &expected max=&expected_max;
        %if &nobs > &expected_max %then %do;
            %let result = FAIL;
            %let message = &nobs 행 > 최대 &expected_max 행;
        %end;
    %end;

    %gxpllm_emit(label=&label, rule=rowcount, result=&result,
                message=&message, observed=&nobs, expected=&expected, strict=&strict);
%mend gxpllm_assert_rowcount;


/*----------------------------------------------------------------------------
  행 수 변화 검증 (필터/병합 전후)

  다대다 MERGE 로 행이 늘어나는 것을 잡는다. SAS 에서 가장 흔한 조용한 오류다.

  Args:
    ds_before      : 변환 전 데이터셋
    ds_after       : 변환 후 데이터셋
    label          : assertion 식별자
    max_loss_rate  : 허용 손실률 (0 ~ 1)
    allow_increase : Y 이면 행 증가 허용
    strict         : Y 이면 실패 시 중단
----------------------------------------------------------------------------*/
%macro gxpllm_assert_rowcount_delta(ds_before, ds_after, label=,
                                   max_loss_rate=, allow_increase=N, strict=N);
    %local n1 n2 delta loss result message dsid;

    %let dsid = %sysfunc(open(&ds_before));
    %if &dsid = 0 %then %do;
        %gxpllm_assert_emit_open_fail(&ds_before, &label, rowcount_delta, &strict);
        %return;
    %end;
    %let n1 = %sysfunc(attrn(&dsid, NLOBS));
    %let dsid = %sysfunc(close(&dsid));

    %let dsid = %sysfunc(open(&ds_after));
    %if &dsid = 0 %then %do;
        %gxpllm_assert_emit_open_fail(&ds_after, &label, rowcount_delta, &strict);
        %return;
    %end;
    %let n2 = %sysfunc(attrn(&dsid, NLOBS));
    %let dsid = %sysfunc(close(&dsid));

    %let delta = %eval(&n2 - &n1);
    %if &n1 > 0 %then %let loss = %sysevalf((&n1 - &n2) / &n1);
    %else %let loss = 0;

    %let result = PASS;
    %let message = &n1 -> &n2 (&delta);

    %if %upcase(&allow_increase) ne Y and &delta > 0 %then %do;
        %let result = FAIL;
        %let message = 행이 &delta 건 증가했습니다 (&n1 -> &n2) - 다대다 병합을 의심하십시오;
    %end;
    %else %if %length(&max_loss_rate) %then %do;
        %if %sysevalf(&loss > &max_loss_rate) %then %do;
            %let result = FAIL;
            %let message = 손실률 %sysfunc(putn(&loss, percent8.2)) > 허용 %sysfunc(putn(&max_loss_rate, percent8.2)) (&n1 -> &n2);
        %end;
    %end;

    %gxpllm_emit(label=&label, rule=rowcount_delta, result=&result,
                message=&message, observed=before=&n1;after=&n2,
                expected=max_loss_rate=&max_loss_rate, strict=&strict);
%mend gxpllm_assert_rowcount_delta;


/*----------------------------------------------------------------------------
  key 유일성 검증

  Args:
    ds     : 검사할 데이터셋
    keys   : 키 변수 (공백 구분, 예: USUBJID PARAMCD AVISITN)
    label  : assertion 식별자
    strict : Y 이면 실패 시 중단
----------------------------------------------------------------------------*/
%macro gxpllm_assert_unique(ds, keys=, label=, strict=N);
    %local ntotal nunique ndup result message;

    proc sql noprint;
        select count(*) into :ntotal trimmed from &ds;
        select count(*) into :nunique trimmed
            from (select distinct %sysfunc(tranwrd(&keys, %str( ), %str(,))) from &ds);
    quit;

    %let ndup = %eval(&ntotal - &nunique);

    %if &ndup > 0 %then %do;
        %let result = FAIL;
        %let message = 중복 &ndup 건 (&keys);
    %end;
    %else %do;
        %let result = PASS;
        %let message = 유일 (&keys, &nunique 건);
    %end;

    %gxpllm_emit(label=&label, rule=unique, result=&result,
                message=&message, observed=total=&ntotal;unique=&nunique,
                expected=duplicates=0, strict=&strict);
%mend gxpllm_assert_unique;


/*----------------------------------------------------------------------------
  값 도메인 검증

  Args:
    ds            : 검사할 데이터셋
    var           : 검사할 변수
    allowed       : 허용 값 (공백 구분, 예: %str(M F))
    label         : assertion 식별자
    allow_missing : Y 이면 결측 허용
    strict        : Y 이면 실패 시 중단
----------------------------------------------------------------------------*/
%macro gxpllm_assert_domain(ds, column=, allowed=, label=, allow_missing=Y, strict=N);
    %local quoted nbad nmiss result message;

    %let quoted = %sysfunc(tranwrd(%sysfunc(compbl(&allowed)), %str( ), %str(","")));
    %let quoted = "&quoted";

    proc sql noprint;
        select count(*) into :nbad trimmed
            from &ds where &column is not missing and &column not in (&quoted);
        select count(*) into :nmiss trimmed
            from &ds where &column is missing;
    quit;

    %let result = PASS;
    %let message = &column 모두 허용 도메인 안;

    %if &nbad > 0 %then %do;
        %let result = FAIL;
        %let message = 허용되지 않은 값 &nbad 건 (&column);
    %end;
    %else %if %upcase(&allow_missing) ne Y and &nmiss > 0 %then %do;
        %let result = FAIL;
        %let message = 결측 &nmiss 건 (&column);
    %end;

    %gxpllm_emit(label=&label, rule=domain, result=&result,
                message=&message, observed=unexpected=&nbad;missing=&nmiss,
                expected=&column in (&allowed), strict=&strict);
%mend gxpllm_assert_domain;


/*----------------------------------------------------------------------------
  결측률 검증

  Args:
    ds       : 검사할 데이터셋
    var      : 검사할 변수
    label    : assertion 식별자
    max_rate : 허용 최대 결측률 (0 ~ 1)
    strict   : Y 이면 실패 시 중단
----------------------------------------------------------------------------*/
%macro gxpllm_assert_missingness(ds, column=, label=, max_rate=, strict=N);
    %local ntotal nmiss rate result message;

    proc sql noprint;
        select count(*) into :ntotal trimmed from &ds;
        select count(*) into :nmiss  trimmed from &ds where &column is missing;
    quit;

    %if &ntotal > 0 %then %let rate = %sysevalf(&nmiss / &ntotal);
    %else %let rate = 0;

    %let result = PASS;
    %let message = &column 결측 &nmiss 건 (%sysfunc(putn(&rate, percent8.2)));

    %if %length(&max_rate) %then %do;
        %if %sysevalf(&rate > &max_rate) %then %do;
            %let result = FAIL;
            %let message = 결측률 %sysfunc(putn(&rate, percent8.2)) > 허용 %sysfunc(putn(&max_rate, percent8.2)) (&column);
        %end;
    %end;

    %gxpllm_emit(label=&label, rule=missingness, result=&result,
                message=&message, observed=missing=&nmiss;total=&ntotal,
                expected=max_rate=&max_rate, strict=&strict);
%mend gxpllm_assert_missingness;


/*----------------------------------------------------------------------------
  날짜 순서 검증 (TRTSDT <= TRTEDT 등)

  Args:
    ds      : 검사할 데이터셋
    earlier : 먼저여야 할 날짜 변수
    later   : 나중이어야 할 날짜 변수
    label   : assertion 식별자
    strict  : Y 이면 실패 시 중단
----------------------------------------------------------------------------*/
%macro gxpllm_assert_date_order(ds, earlier=, later=, label=, strict=N);
    %local nbad result message;

    proc sql noprint;
        select count(*) into :nbad trimmed
            from &ds
            where &earlier is not missing and &later is not missing
              and &earlier > &later;
    quit;

    %if &nbad > 0 %then %do;
        %let result = FAIL;
        %let message = &earlier <= &later 위반 &nbad 건;
    %end;
    %else %do;
        %let result = PASS;
        %let message = &earlier <= &later 정상;
    %end;

    %gxpllm_emit(label=&label, rule=date_order, result=&result,
                message=&message, observed=violations=&nbad,
                expected=violations=0, strict=&strict);
%mend gxpllm_assert_date_order;


/*----------------------------------------------------------------------------
  분석군 검증

  Args:
    ds         : 분석군 필터를 적용한 데이터셋
    flag       : 분석군 flag 변수 (SAFFL, FASFL, PPROTFL)
    value      : 포함 조건 값 (보통 Y)
    label      : assertion 식별자
    expected_n : SAP 에 명시된 기대 피험자 수
    strict     : Y 이면 실패 시 중단
----------------------------------------------------------------------------*/
%macro gxpllm_assert_analysis_set(ds, flag_column=, flag_value=Y, label=, expected_n=, strict=N);
    %local n nbad result message;

    proc sql noprint;
        select count(*) into :n    trimmed from &ds;
        select count(*) into :nbad trimmed from &ds where &flag_column ne "&flag_value";
    quit;

    %let result = PASS;
    %let message = &flag_column='&flag_value' &n 명;

    %if &nbad > 0 %then %do;
        %let result = FAIL;
        %let message = &flag_column ne '&flag_value' 인 행 &nbad 건 포함;
    %end;
    %else %if %length(&expected_n) %then %do;
        %if &n ne &expected_n %then %do;
            %let result = FAIL;
            %let message = SAP 명시 &expected_n 명과 불일치 (실제 &n 명);
        %end;
    %end;

    %gxpllm_emit(label=&label, rule=analysis_set, result=&result,
                message=&message, observed=n=&n, expected=&flag_column=&flag_value, strict=&strict);
%mend gxpllm_assert_analysis_set;


/*----------------------------------------------------------------------------
  분모 검증

  Args:
    ds          : 분석군 데이터셋
    subject_column : 피험자 식별자 변수 (보통 USUBJID)
    denominator : 표에 사용한 분모 값
    label       : assertion 식별자
    strict      : Y 이면 실패 시 중단
----------------------------------------------------------------------------*/
%macro gxpllm_assert_denominator(ds, subject_column=USUBJID, denominator=, label=, strict=N);
    %local nactual result message;

    proc sql noprint;
        select count(distinct &subject_column) into :nactual trimmed from &ds;
    quit;

    %if &nactual = &denominator %then %do;
        %let result = PASS;
        %let message = 분모 &denominator = unique &subject_column &nactual;
    %end;
    %else %do;
        %let result = FAIL;
        %let message = 분모 &denominator ne unique &subject_column &nactual;
    %end;

    %gxpllm_emit(label=&label, rule=denominator, result=&result,
                message=&message, observed=&nactual,
                expected=denominator=&denominator, strict=&strict);
%mend gxpllm_assert_denominator;


/*----------------------------------------------------------------------------
  정합성 관계식 검증 (actual <= limit)

  가장 흔한 규칙: AE subject count <= 분모

  Args:
    actual : 관측값
    limit  : 상한
    label  : assertion 식별자
    expr   : 관계식 설명
    strict : Y 이면 실패 시 중단
----------------------------------------------------------------------------*/
%macro gxpllm_assert_le(actual, limit, label=, expr=, strict=N);
    %local result message description;

    %if %length(&expr) %then %let description = &expr;
    %else %let description = &label <= &limit;

    %if %sysevalf(&actual <= &limit) %then %do;
        %let result = PASS;
        %let message = &description 만족 (&actual <= &limit);
    %end;
    %else %do;
        %let result = FAIL;
        %let message = &description 위반 (&actual > &limit);
    %end;

    %gxpllm_emit(label=&label, rule=reconciliation, result=&result,
                message=&message, observed=&actual, expected=max=&limit, strict=&strict);
%mend gxpllm_assert_le;


/*----------------------------------------------------------------------------
  내부: 데이터셋 열기 실패 처리
----------------------------------------------------------------------------*/
%macro gxpllm_assert_emit_open_fail(ds, label, rule, strict);
    %gxpllm_emit(label=&label, rule=&rule, result=FAIL,
                message=데이터셋을 열 수 없습니다: &ds, observed=, expected=,
                strict=&strict);
%mend gxpllm_assert_emit_open_fail;
