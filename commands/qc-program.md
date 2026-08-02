---
description: Independent Programming(double programming)용 QC 프로그램을 작성합니다
argument-hint: <table/figure ID> [sas|python|r]
allowed-tools: mcp__local-coder__write_program, Read, Write, Glob, Bash
---

# Independent Programming (QC)

제출 경로 산출물은 두 사람이 독립적으로 프로그래밍한 결과를 대조합니다.
이 명령은 QC 쪽 프로그램을 작성합니다.

## 대상

$1 (예: `t_14_1_1`)
언어: $2

## 독립성 규칙 — 가장 중요합니다

**원 프로그램 소스를 절대 읽지 마십시오.**

Independent Programming의 목적은 같은 명세를 서로 다르게 구현했을 때
같은 숫자가 나오는지 확인하는 것입니다. 원 코드를 보면 같은 오해를 반복하게 되어
double programming이 무의미해집니다.

이 명령을 실행하는 동안:
- `programs/{language}/{$1}.{확장자}` (원 프로그램)를 읽지 않습니다
- 원 프로그램의 manifest나 assertion 결과를 참조하지 않습니다
- 입력은 **SAP의 table shell과 Data Dictionary뿐**입니다

### 제출 경로에서의 한계 — 사용자에게 반드시 알릴 것

같은 로컬 LLM이 primary와 QC를 모두 작성하면, 원 코드를 안 보더라도
**모델이 가진 동일한 편향과 오해가 양쪽에 반영됩니다.**

따라서 제출 경로에서는:
- **원칙: QC는 사람이 직접 작성합니다.** LLM QC는 보조 수단입니다.
- LLM QC를 쓴다면 manifest의 `generator`에 기록되며, 이것만으로는
  Independent Programming 요건을 충족하지 못합니다.
- 이 사실을 사용자에게 명시적으로 알리고 진행 여부를 확인하십시오.

## 절차

### 1단계: 독립성 확인

먼저 사용자에게 확인합니다.

```
이 QC 프로그램은 로컬 LLM이 작성합니다.
원 프로그램은 참조하지 않지만, primary도 같은 모델이 작성했다면
Independent Programming 요건을 충족하지 못합니다.

- primary를 누가 작성했습니까? (사람 / LLM)
- 이 QC를 정식 double programming으로 쓸 것입니까, 사전 점검용입니까?
```

### 2단계: SAP에서 명세 추출

`docs/sap.md`에서 $1 부분만 읽습니다. `/write-program`의 1단계와 동일합니다.

### 3단계: 다른 접근으로 구현하도록 지시

같은 명세라도 구현 방식을 달리하면 오류가 상쇄될 확률이 높아집니다.

`mcp__local-coder__write_program` 호출 시 `instructions`에 다음을 넣습니다.

```
이것은 Independent Programming(QC) 프로그램입니다.
다음 원칙으로 작성하십시오.

1. 가능하면 primary와 다른 접근을 쓰십시오.
   - SAS: PROC MEANS/FREQ 대신 PROC SQL 집계, 또는 그 반대
   - Python: pandas groupby 대신 명시적 반복, 또는 그 반대
   - R: dplyr 대신 base R, 또는 그 반대
2. 중간 단계를 더 많이 나누고 각 단계마다 assertion을 넣으십시오.
3. 산출물은 output/tables/qc_{$1}.{확장자}로 저장하십시오.
4. 최종 숫자를 기계 판독 가능한 형식(JSON 또는 CSV)으로도 출력하십시오.
   대조 스크립트가 읽습니다.
```

`program_name`은 `qc_{$1}.{확장자}`, `outputs`는 `output/tables/qc_{$1}.*`로 지정합니다.

### 4단계: 검토 후 저장

`/write-program`의 5단계 검토를 동일하게 수행합니다.
`programs/qc/{$1}.{확장자}`에 저장합니다.

### 5단계: 실행

```bash
python scripts/run_{language}.py --program programs/qc/{$1}.{확장자} --purpose qc
```

`--purpose qc`는 WARNING도 실패로 처리합니다.

### 6단계: 대조

**대조는 LLM이 아니라 결정론적 스크립트가 합니다.**

primary와 QC의 수치 출력을 비교합니다.

```bash
python scripts/compare_outputs.py \
    --primary output/tables/{$1}.json \
    --qc      output/tables/qc_{$1}.json \
    --tolerance 0
```

불일치가 있으면 **어느 쪽이 맞는지 LLM이 판단하지 마십시오.**
불일치 항목을 사용자에게 보고하고 사람이 판정하게 합니다.

## 보고

사용자에게 보고할 것:
- primary와 QC의 작성 주체 (사람 / LLM, 같은 모델인지)
- 대조 결과: 일치 항목 수, 불일치 항목 수
- 불일치 항목 목록 (값과 위치)
- **Independent Programming 요건 충족 여부에 대한 명시적 판단**

## 주의

- 원 프로그램을 읽으려는 시도는 이 명령의 목적을 훼손합니다.
- 대조에서 불일치가 나오면 그것이 이 절차의 성과입니다. 숨기거나 맞추려 하지 마십시오.
