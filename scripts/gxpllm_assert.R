# ============================================================================
# gxpllm assertion 라이브러리 (R)
#
# 분석 프로그램이 데이터 무결성과 임상 정합성을 검증하고 결과를 기록한다.
# 결과는 assertions.jsonl 에 한 줄씩 append 되며, runner 가 이를 모아 판정한다.
#
# SAS(gxpllm_assert.sas) / Python(gxpllm_assert.py) 과 동일한 출력 형식을 유지한다.
#
# 사용:
#   source(file.path(Sys.getenv("GXPLLM_PLUGIN_ROOT"), "scripts", "gxpllm_assert.R"))
#
#   gxpllm_assert_rowcount(adsl, label = "ADSL_LOADED", expected_min = 1)
#   gxpllm_assert_unique(adsl, keys = "USUBJID", label = "ADSL_UNIQUE_SUBJ")
#
# 주의: assertion 은 실패해도 프로그램을 중단하지 않는다.
#       즉시 중단하려면 strict = TRUE 를 지정한다.
#       외부 패키지(jsonlite 등)에 의존하지 않는다.
# ============================================================================

# ----------------------------------------------------------------------------
# 설정 영역 - 여기서 수정하세요
# ----------------------------------------------------------------------------

.GXPLLM_ASSERTIONS_FILENAME <- "assertions.jsonl"
.GXPLLM_RESULT_PASS <- "PASS"
.GXPLLM_RESULT_FAIL <- "FAIL"


# ----------------------------------------------------------------------------
# 내부 유틸
# ----------------------------------------------------------------------------

.gxpllm_run_dir <- function() {
  #' assertion 기록 디렉터리를 돌려준다
  #'
  #' runner 가 GXPLLM_RUN_DIR 환경변수로 전달한다.
  #' runner 밖에서 실행되면 현재 디렉터리에 기록한다.
  #'
  #' @return 디렉터리 경로 문자열
  dir <- Sys.getenv("GXPLLM_RUN_DIR")
  if (nzchar(dir)) dir else getwd()
}


.gxpllm_json_escape <- function(x) {
  #' JSON 문자열 값으로 쓸 수 있게 이스케이프한다
  #'
  #' @param x 대상 문자열
  #' @return 이스케이프된 문자열
  x <- as.character(x)
  if (length(x) == 0 || is.na(x)) return("")
  x <- gsub("\\\\", "\\\\\\\\", x)
  x <- gsub('"', '\\\\"', x)
  x <- gsub("\n", "\\\\n", x)
  x <- gsub("\r", "\\\\r", x)
  x <- gsub("\t", "\\\\t", x)
  x
}


.gxpllm_emit <- function(label, rule, result, message, observed = "", expected = "",
                        strict = FALSE) {
  #' assertion 결과를 assertions.jsonl 에 append 한다
  #'
  #' @param label assertion 식별자
  #' @param rule 규칙 종류
  #' @param result PASS 또는 FAIL
  #' @param message 사람이 읽을 설명
  #' @param observed 관측값
  #' @param expected 기대값
  #' @param strict TRUE 이면 실패 시 stop()
  #'
  #' @return result == PASS 이면 TRUE
  line <- paste0(
    '{"label":"',    .gxpllm_json_escape(label),    '"',
    ',"rule":"',     .gxpllm_json_escape(rule),     '"',
    ',"result":"',   .gxpllm_json_escape(result),   '"',
    ',"language":"r"',
    ',"message":"',  .gxpllm_json_escape(message),  '"',
    ',"observed":"', .gxpllm_json_escape(observed), '"',
    ',"expected":"', .gxpllm_json_escape(expected), '"',
    ',"ts":"',       format(Sys.time(), "%Y-%m-%dT%H:%M:%S%z"), '"',
    '}'
  )

  path <- file.path(.gxpllm_run_dir(), .GXPLLM_ASSERTIONS_FILENAME)
  tryCatch(
    {
      con <- file(path, open = "a", encoding = "UTF-8")
      on.exit(close(con), add = TRUE)
      writeLines(line, con, useBytes = TRUE)
    },
    error = function(e) invisible(NULL)
  )

  cat(sprintf("  [%s] %s: %s\n", result, label, message))

  if (identical(result, .GXPLLM_RESULT_FAIL) && isTRUE(strict)) {
    stop(sprintf("[gxpllm] assertion 실패: %s: %s", label, message), call. = FALSE)
  }

  identical(result, .GXPLLM_RESULT_PASS)
}


.gxpllm_nrow <- function(df) {
  #' 데이터프레임의 행 수를 돌려준다
  #'
  #' @param df 데이터프레임 또는 벡터
  #' @return 행 수 정수
  if (is.data.frame(df)) nrow(df) else length(df)
}


# ----------------------------------------------------------------------------
# 데이터 무결성 assertion
# ----------------------------------------------------------------------------

gxpllm_assert_rowcount <- function(df, label, expected_min = NULL, expected_max = NULL,
                                  expected_n = NULL, strict = FALSE) {
  #' 데이터셋 행 수가 기대 범위 안인지 검증한다
  #'
  #' @param df 검사할 데이터프레임
  #' @param label assertion 식별자
  #' @param expected_min 최소 행 수
  #' @param expected_max 최대 행 수
  #' @param expected_n 정확히 일치해야 할 행 수
  #' @param strict TRUE 이면 실패 시 중단
  #'
  #' @return 통과하면 TRUE
  n <- .gxpllm_nrow(df)
  problems <- character(0)
  expected <- character(0)

  if (!is.null(expected_n)) {
    expected <- c(expected, paste0("exact=", expected_n))
    if (n != expected_n) {
      problems <- c(problems, sprintf("기대 %s행이 아닌 %s행",
                                      format(expected_n, big.mark = ","),
                                      format(n, big.mark = ",")))
    }
  }
  if (!is.null(expected_min)) {
    expected <- c(expected, paste0("min=", expected_min))
    if (n < expected_min) {
      problems <- c(problems, sprintf("%s행 < 최소 %s행",
                                      format(n, big.mark = ","),
                                      format(expected_min, big.mark = ",")))
    }
  }
  if (!is.null(expected_max)) {
    expected <- c(expected, paste0("max=", expected_max))
    if (n > expected_max) {
      problems <- c(problems, sprintf("%s행 > 최대 %s행",
                                      format(n, big.mark = ","),
                                      format(expected_max, big.mark = ",")))
    }
  }

  result <- if (length(problems)) .GXPLLM_RESULT_FAIL else .GXPLLM_RESULT_PASS
  message <- if (length(problems)) paste(problems, collapse = "; ") else
    sprintf("%s행", format(n, big.mark = ","))

  .gxpllm_emit(label, "rowcount", result, message, n,
              paste(expected, collapse = ";"), strict)
}


gxpllm_assert_rowcount_delta <- function(before, after, label, max_loss_rate = NULL,
                                        allow_increase = FALSE, strict = FALSE) {
  #' 변환 전후 행 수 변화가 의도한 것인지 검증한다
  #'
  #' @param before 변환 전 데이터프레임 또는 행 수
  #' @param after 변환 후 데이터프레임 또는 행 수
  #' @param label assertion 식별자
  #' @param max_loss_rate 허용 손실률 (0 ~ 1)
  #' @param allow_increase TRUE 이면 행 증가 허용
  #' @param strict TRUE 이면 실패 시 중단
  #'
  #' @return 통과하면 TRUE
  n1 <- if (is.numeric(before) && length(before) == 1) before else .gxpllm_nrow(before)
  n2 <- if (is.numeric(after) && length(after) == 1) after else .gxpllm_nrow(after)
  delta <- n2 - n1
  loss <- if (n1 > 0) (n1 - n2) / n1 else 0

  problems <- character(0)

  if (!isTRUE(allow_increase) && delta > 0) {
    problems <- c(problems, sprintf(
      "행이 %s건 증가했습니다 (%s -> %s) - 다대다 병합을 의심하십시오",
      format(delta, big.mark = ","), format(n1, big.mark = ","), format(n2, big.mark = ",")))
  }
  if (!is.null(max_loss_rate) && loss > max_loss_rate) {
    problems <- c(problems, sprintf("손실률 %.2f%% > 허용 %.2f%% (%s -> %s)",
                                    loss * 100, max_loss_rate * 100,
                                    format(n1, big.mark = ","), format(n2, big.mark = ",")))
  }

  result <- if (length(problems)) .GXPLLM_RESULT_FAIL else .GXPLLM_RESULT_PASS
  message <- if (length(problems)) paste(problems, collapse = "; ") else
    sprintf("%s -> %s (%+d)", format(n1, big.mark = ","), format(n2, big.mark = ","), delta)

  .gxpllm_emit(label, "rowcount_delta", result, message,
              sprintf("before=%d;after=%d", n1, n2),
              sprintf("max_loss_rate=%s", ifelse(is.null(max_loss_rate), "", max_loss_rate)),
              strict)
}


gxpllm_assert_unique <- function(df, keys, label, strict = FALSE) {
  #' key 조합이 유일한지 검증한다
  #'
  #' @param df 검사할 데이터프레임
  #' @param keys 키 컬럼명 (문자 벡터)
  #' @param label assertion 식별자
  #' @param strict TRUE 이면 실패 시 중단
  #'
  #' @return 통과하면 TRUE
  missing_cols <- setdiff(keys, names(df))
  if (length(missing_cols)) {
    return(.gxpllm_emit(label, "unique", .GXPLLM_RESULT_FAIL,
                       sprintf("컬럼 없음: %s", paste(missing_cols, collapse = ", ")),
                       "", paste(keys, collapse = "+"), strict))
  }

  subset_df <- df[, keys, drop = FALSE]
  n_total <- nrow(subset_df)
  n_unique <- nrow(unique(subset_df))
  duplicates <- n_total - n_unique

  result <- if (duplicates > 0) .GXPLLM_RESULT_FAIL else .GXPLLM_RESULT_PASS
  message <- if (duplicates > 0) {
    sprintf("중복 %s건 (%s)", format(duplicates, big.mark = ","), paste(keys, collapse = "+"))
  } else {
    sprintf("유일 (%s, %s건)", paste(keys, collapse = "+"), format(n_unique, big.mark = ","))
  }

  .gxpllm_emit(label, "unique", result, message,
              sprintf("total=%d;unique=%d;duplicates=%d", n_total, n_unique, duplicates),
              "duplicates=0", strict)
}


gxpllm_assert_domain <- function(df, column, allowed, label, allow_missing = TRUE,
                                strict = FALSE) {
  #' 값이 허용 도메인 안인지 검증한다
  #'
  #' @param df 검사할 데이터프레임
  #' @param column 검사할 컬럼명
  #' @param allowed 허용 값 벡터
  #' @param label assertion 식별자
  #' @param allow_missing TRUE 이면 결측 허용
  #' @param strict TRUE 이면 실패 시 중단
  #'
  #' @return 통과하면 TRUE
  if (!(column %in% names(df))) {
    return(.gxpllm_emit(label, "domain", .GXPLLM_RESULT_FAIL,
                       sprintf("컬럼 없음: %s", column), "", "", strict))
  }

  values <- df[[column]]
  n_missing <- sum(is.na(values))
  observed_values <- unique(values[!is.na(values)])
  unexpected <- setdiff(observed_values, allowed)

  problems <- character(0)
  if (length(unexpected)) {
    preview <- paste(head(as.character(unexpected), 5), collapse = ", ")
    problems <- c(problems, sprintf("허용되지 않은 값 %d종: %s", length(unexpected), preview))
  }
  if (n_missing > 0 && !isTRUE(allow_missing)) {
    problems <- c(problems, sprintf("결측 %s건", format(n_missing, big.mark = ",")))
  }

  result <- if (length(problems)) .GXPLLM_RESULT_FAIL else .GXPLLM_RESULT_PASS
  message <- if (length(problems)) paste(problems, collapse = "; ") else
    sprintf("%s 모두 허용 도메인 안 (%d종)", column, length(observed_values))

  .gxpllm_emit(label, "domain", result, message,
              sprintf("unexpected=%d;missing=%d", length(unexpected), n_missing),
              sprintf("%s in (%s)", column, paste(allowed, collapse = " ")), strict)
}


gxpllm_assert_missingness <- function(df, column, label, max_rate = NULL, strict = FALSE) {
  #' 결측률이 기대 범위 안인지 검증한다
  #'
  #' @param df 검사할 데이터프레임
  #' @param column 검사할 컬럼명
  #' @param label assertion 식별자
  #' @param max_rate 허용 최대 결측률 (0 ~ 1)
  #' @param strict TRUE 이면 실패 시 중단
  #'
  #' @return 통과하면 TRUE
  if (!(column %in% names(df))) {
    return(.gxpllm_emit(label, "missingness", .GXPLLM_RESULT_FAIL,
                       sprintf("컬럼 없음: %s", column), "", "", strict))
  }

  n_total <- nrow(df)
  n_missing <- sum(is.na(df[[column]]))
  rate <- if (n_total > 0) n_missing / n_total else 0

  problems <- character(0)
  if (!is.null(max_rate) && rate > max_rate) {
    problems <- c(problems, sprintf("결측률 %.2f%% > 허용 %.2f%%", rate * 100, max_rate * 100))
  }

  result <- if (length(problems)) .GXPLLM_RESULT_FAIL else .GXPLLM_RESULT_PASS
  message <- if (length(problems)) paste(problems, collapse = "; ") else
    sprintf("%s 결측 %s건 (%.2f%%)", column, format(n_missing, big.mark = ","), rate * 100)

  .gxpllm_emit(label, "missingness", result, message,
              sprintf("missing=%d;total=%d;rate=%.6f", n_missing, n_total, rate),
              sprintf("max_rate=%s", ifelse(is.null(max_rate), "", max_rate)), strict)
}


gxpllm_assert_date_order <- function(df, earlier, later, label, allow_equal = TRUE,
                                    strict = FALSE) {
  #' 날짜 순서가 논리적인지 검증한다 (TRTSDT <= TRTEDT 등)
  #'
  #' @param df 검사할 데이터프레임
  #' @param earlier 먼저여야 할 날짜 컬럼명
  #' @param later 나중이어야 할 날짜 컬럼명
  #' @param label assertion 식별자
  #' @param allow_equal TRUE 이면 같은 날짜 허용
  #' @param strict TRUE 이면 실패 시 중단
  #'
  #' @return 통과하면 TRUE
  missing_cols <- setdiff(c(earlier, later), names(df))
  if (length(missing_cols)) {
    return(.gxpllm_emit(label, "date_order", .GXPLLM_RESULT_FAIL,
                       sprintf("컬럼 없음: %s", paste(missing_cols, collapse = ", ")),
                       "", "", strict))
  }

  valid <- !is.na(df[[earlier]]) & !is.na(df[[later]])
  violations <- if (isTRUE(allow_equal)) {
    sum(df[[earlier]][valid] > df[[later]][valid])
  } else {
    sum(df[[earlier]][valid] >= df[[later]][valid])
  }

  operator <- if (isTRUE(allow_equal)) "<=" else "<"
  result <- if (violations > 0) .GXPLLM_RESULT_FAIL else .GXPLLM_RESULT_PASS
  message <- if (violations > 0) {
    sprintf("%s %s %s 위반 %s건", earlier, operator, later, format(violations, big.mark = ","))
  } else {
    sprintf("%s %s %s 정상 (%s건 검사)", earlier, operator, later,
            format(sum(valid), big.mark = ","))
  }

  .gxpllm_emit(label, "date_order", result, message,
              sprintf("violations=%d;checked=%d", violations, sum(valid)),
              "violations=0", strict)
}


# ----------------------------------------------------------------------------
# 임상 정합성 assertion
# ----------------------------------------------------------------------------

gxpllm_assert_analysis_set <- function(df, flag_column, flag_value = "Y", label,
                                      expected_n = NULL, strict = FALSE) {
  #' 분석군 flag 가 SAP 정의와 일치하는지 검증한다
  #'
  #' @param df 분석군 필터를 적용한 데이터프레임
  #' @param flag_column 분석군 flag 컬럼명 (SAFFL, FASFL, PPROTFL)
  #' @param flag_value 포함 조건 값
  #' @param label assertion 식별자
  #' @param expected_n SAP 에 명시된 기대 피험자 수
  #' @param strict TRUE 이면 실패 시 중단
  #'
  #' @return 통과하면 TRUE
  n <- nrow(df)
  wrong_flag <- if (flag_column %in% names(df)) sum(df[[flag_column]] != flag_value, na.rm = TRUE) else 0

  problems <- character(0)
  if (wrong_flag > 0) {
    problems <- c(problems, sprintf("%s != '%s' 인 행 %s건 포함",
                                    flag_column, flag_value, format(wrong_flag, big.mark = ",")))
  }
  if (!is.null(expected_n) && n != expected_n) {
    problems <- c(problems, sprintf("SAP 명시 %s명과 불일치 (실제 %s명)",
                                    format(expected_n, big.mark = ","), format(n, big.mark = ",")))
  }

  result <- if (length(problems)) .GXPLLM_RESULT_FAIL else .GXPLLM_RESULT_PASS
  message <- if (length(problems)) paste(problems, collapse = "; ") else
    sprintf("%s='%s' %s명", flag_column, flag_value, format(n, big.mark = ","))

  .gxpllm_emit(label, "analysis_set", result, message,
              sprintf("n=%d;wrong_flag=%d", n, wrong_flag),
              sprintf("%s=%s", flag_column, flag_value), strict)
}


gxpllm_assert_denominator <- function(df, subject_column = "USUBJID", denominator,
                                     label, strict = FALSE) {
  #' 분모가 해당 분석군의 unique subject 수와 일치하는지 검증한다
  #'
  #' @param df 분석군 데이터프레임
  #' @param subject_column 피험자 식별자 컬럼명
  #' @param denominator 표에 사용한 분모 값
  #' @param label assertion 식별자
  #' @param strict TRUE 이면 실패 시 중단
  #'
  #' @return 통과하면 TRUE
  if (!(subject_column %in% names(df))) {
    return(.gxpllm_emit(label, "denominator", .GXPLLM_RESULT_FAIL,
                       sprintf("컬럼 없음: %s", subject_column), "", "", strict))
  }

  actual <- length(unique(df[[subject_column]]))
  result <- if (actual == denominator) .GXPLLM_RESULT_PASS else .GXPLLM_RESULT_FAIL
  message <- if (actual == denominator) {
    sprintf("분모 %s = unique %s %s", format(denominator, big.mark = ","),
            subject_column, format(actual, big.mark = ","))
  } else {
    sprintf("분모 %s != unique %s %s", format(denominator, big.mark = ","),
            subject_column, format(actual, big.mark = ","))
  }

  .gxpllm_emit(label, "denominator", result, message, actual,
              sprintf("denominator=%d", denominator), strict)
}


gxpllm_assert_le <- function(actual, limit, label, expression = NULL, strict = FALSE) {
  #' actual <= limit 관계를 검증한다
  #'
  #' 가장 흔한 정합성 규칙인 'AE subject count <= denominator' 용
  #'
  #' @param actual 관측값
  #' @param limit 상한
  #' @param label assertion 식별자
  #' @param expression 관계식 설명
  #' @param strict TRUE 이면 실패 시 중단
  #'
  #' @return 통과하면 TRUE
  expr <- if (is.null(expression)) sprintf("%s <= %s", label, format(limit, big.mark = ",")) else expression
  passed <- actual <= limit

  result <- if (passed) .GXPLLM_RESULT_PASS else .GXPLLM_RESULT_FAIL
  message <- if (passed) {
    sprintf("%s 만족 (%s <= %s)", expr, format(actual, big.mark = ","), format(limit, big.mark = ","))
  } else {
    sprintf("%s 위반 (%s > %s)", expr, format(actual, big.mark = ","), format(limit, big.mark = ","))
  }

  .gxpllm_emit(label, "reconciliation", result, message, actual,
              sprintf("max=%s", limit), strict)
}


gxpllm_assert_sum_equals <- function(parts, total, label, tolerance = 0, strict = FALSE) {
  #' 부분의 합이 전체와 일치하는지 검증한다
  #'
  #' @param parts 부분값 벡터
  #' @param total 전체값
  #' @param label assertion 식별자
  #' @param tolerance 허용 오차
  #' @param strict TRUE 이면 실패 시 중단
  #'
  #' @return 통과하면 TRUE
  part_sum <- sum(parts)
  diff <- abs(part_sum - total)
  passed <- diff <= tolerance

  result <- if (passed) .GXPLLM_RESULT_PASS else .GXPLLM_RESULT_FAIL
  message <- if (passed) {
    sprintf("부분 합 %s = 전체 %s", format(part_sum, big.mark = ","), format(total, big.mark = ","))
  } else {
    sprintf("부분 합 %s != 전체 %s (차이 %s)", format(part_sum, big.mark = ","),
            format(total, big.mark = ","), format(diff, big.mark = ","))
  }

  .gxpllm_emit(label, "reconciliation", result, message,
              sprintf("part_sum=%s;total=%s;diff=%s", part_sum, total, diff),
              sprintf("tolerance=%s", tolerance), strict)
}


gxpllm_assert_coding_version <- function(actual_version, expected_version, dictionary,
                                        label, strict = FALSE) {
  #' MedDRA / WHODrug 코딩 사전 버전이 DMP 명시와 일치하는지 검증한다
  #'
  #' @param actual_version 데이터에 기록된 버전
  #' @param expected_version DMP 에 명시된 버전
  #' @param dictionary 사전 이름
  #' @param label assertion 식별자
  #' @param strict TRUE 이면 실패 시 중단
  #'
  #' @return 통과하면 TRUE
  passed <- trimws(as.character(actual_version)) == trimws(as.character(expected_version))

  result <- if (passed) .GXPLLM_RESULT_PASS else .GXPLLM_RESULT_FAIL
  message <- if (passed) {
    sprintf("%s %s (DMP 일치)", dictionary, actual_version)
  } else {
    sprintf("%s 버전 불일치: 데이터 %s vs DMP %s", dictionary, actual_version, expected_version)
  }

  .gxpllm_emit(label, "coding_version", result, message, actual_version,
              sprintf("%s=%s", dictionary, expected_version), strict)
}


# ----------------------------------------------------------------------------
# 요약
# ----------------------------------------------------------------------------

gxpllm_assert_summary <- function() {
  #' 현재 run 의 assertion 결과를 요약해 출력한다
  #'
  #' 프로그램 끝에서 호출하면 로그에 요약이 남는다
  #'
  #' @return 리스트(total, passed, failed)
  path <- file.path(.gxpllm_run_dir(), .GXPLLM_ASSERTIONS_FILENAME)
  if (!file.exists(path)) {
    cat("\n  assertion 기록 없음\n")
    return(list(total = 0, passed = 0, failed = 0))
  }

  lines <- readLines(path, warn = FALSE, encoding = "UTF-8")
  lines <- lines[nzchar(trimws(lines))]

  passed <- sum(grepl('"result":"PASS"', lines, fixed = TRUE))
  failed <- sum(grepl('"result":"FAIL"', lines, fixed = TRUE))

  cat(sprintf("\n  assertion 요약: %s건 중 %s건 통과 / %s건 실패\n",
              format(length(lines), big.mark = ","),
              format(passed, big.mark = ","),
              format(failed, big.mark = ",")))

  for (line in lines[grepl('"result":"FAIL"', lines, fixed = TRUE)]) {
    label <- sub('.*"label":"([^"]*)".*', "\\1", line)
    message <- sub('.*"message":"([^"]*)".*', "\\1", line)
    cat(sprintf("    FAIL %s: %s\n", label, message))
  }

  invisible(list(total = length(lines), passed = passed, failed = failed))
}
