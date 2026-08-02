# 시작하기

## 0. 먼저 읽을 것

**gxpllm 은 아직 밸리데이션되지 않았습니다.**
규제 제출 경로(CSR, safety signal, IND/NDA)에 쓰기 전에
[§12.4 환경 검증](development.md)과 IQ/OQ/PQ 를 완료하십시오.

이름에 GxP 가 들어간다고 GxP 준수를 자동 달성하지 않습니다.
이 도구는 **증적을 자동 생산할 뿐**이며, 밸리데이션은 별도 작업입니다.

---

## 1. 요구 사항

| 항목 | 필요성 |
|---|---|
| Claude Code | 필수 |
| Python 3.10 이상 | 필수 (hook·runner. 표준 라이브러리만 사용) |
| SAS 9.4 | 제출 경로 분석용 (**정품 라이선스 필수**) |
| R + renv | 선택 |
| uv | 제출 경로에서는 필수 (Python 환경 잠금) |
| DGX Spark vLLM endpoint | 로컬 LLM |
| chardet | 선택 (SAS 로그 인코딩 자동 감지 정확도 향상) |
| PyYAML | 선택 (`benchmark_codegen.py` 용) |

---

## 2. 설치

```bash
/plugin marketplace add https://github.com/kimmingul/gxpllm
/plugin install gxpllm
```

### LLM endpoint 설정

`.mcp.json` 의 `GXPLLM_ENDPOINT` 를 사내 DGX Spark 주소로 바꿉니다.

```json
{
  "mcpServers": {
    "local-coder": {
      "env": {
        "GXPLLM_ENDPOINT": "http://dgx-spark.internal:8001/v1",
        "GXPLLM_MODEL": "Qwen3.6-35B-A3B-NVFP4"
      }
    }
  }
}
```

### DGX Spark 쪽 vLLM 기동

```bash
vllm serve /models/Qwen3.6-35B-A3B-NVFP4 \
  --served-model-name Qwen3.6-35B-A3B-NVFP4 \
  --port 8001 --max-num-seqs 4 --max-model-len 32768 \
  --disable-log-requests
```

**`--disable-log-requests` 는 필수입니다.** 코드 작성 프롬프트에 데이터 구조가
들어가는데, 기본 설정에서는 이것이 서버 로그에 평문으로 쌓입니다.

---

## 3. 검증

```bash
python tests/run_all.py
```

**배포 전 반드시 통과해야 합니다.** hook 스크립트에 구문 오류가 있으면
exit 1 이 되어 경계가 열립니다.

SAS, R, DGX Spark 없이도 전부 통과합니다
(`test_llm_path.py` 가 모의 vLLM 서버로 HTTP 왕복까지 확인합니다).

---

## 4. 첫 study

```bash
python scripts/init_study.py --root D:\clinical\ABC-301 --study-id ABC-301
```

만들어지는 구조:

```
D:\clinical\ABC-301\
├── .gxpllm\config.json      study 설정 (Claude 는 읽기만 가능)
├── data\raw\                원본 — Claude 접근 차단
├── data\derived\            파생 — Claude 접근 차단
├── docs\                    Protocol, SAP, DMP, Data Dictionary
├── programs\sas|python|r\   분석 코드
├── programs\qc\             Independent Programming
├── output\tables\           집계 표 — Claude 읽기 가능
├── output\figures\          그림 — Claude 읽기 가능
├── output\listings\         피험자 단위 — Claude 접근 차단
├── logs\runs\{run_id}\      실행 기록
└── audit\audit.jsonl        해시 체인
```

### 원본 데이터 배치

```
D:\clinical\ABC-301\data\raw\  에 의뢰자 제공 데이터를 둡니다
D:\clinical\ABC-301\docs\protocol.md  에 Protocol 을 둡니다
```

### Claude Code 실행

```bash
cd D:\clinical\ABC-301\programs
claude
```

**`programs` 에서 실행하는 것이 중요합니다.** 이 위치가 작업 공간입니다.

---

## 5. 작업 흐름

```
/build-dictionary          데이터 프로파일링 → Data Dictionary 초안
      ↓ (사람 검토 — 추정 의미 확인)
/draft-sap                 Protocol + Dictionary → SAP 초안
      ↓ (통계책임자 승인)
/derive-assertions         SAP → 기계 판독 가능 검증 규칙
      ↓
/write-program t_14_1_1    SAP table shell → SAS/Python/R 코드
      ↓ (코드 검토 — 하드코딩된 값이 없는지)
/run-program programs/sas/t_14_1_1.sas
      ↓
/qc-program t_14_1_1       Independent Programming (원 코드 미참조)
      ↓
/review-output t_14_1_1    Table 검토 → CSR 문구 초안 (placeholder)
```

---

## 6. 데이터 경계

**study 루트 안은 기본 거부입니다.**

| 경로 | Claude |
|---|---|
| `docs/` `programs/` `macros/` `spec/` `validation/` | 읽기·쓰기 |
| `output/tables/` `output/figures/` | 읽기만 |
| `logs/runs/*/manifest.json` `assertions.json` | 읽기만 |
| `audit/` `.gxpllm/` | 읽기만 |
| **`data/`** | **차단** |
| **`output/listings/`** | **차단** (피험자 단위) |
| **`logs/runs/*/stdout.txt` `execution.log`** | **차단** |

**Table 과 Figure 는 되고 Listing 은 안 됩니다.** TLF 중 L 만 성질이 다릅니다.

차단 시도는 감사 로그에 `access_blocked` 로 남습니다.

---

## 7. 백업 — 필수

해시 체인은 변조를 **탐지**할 뿐 **방지하지 못합니다.**
특히 **후미 절단은 탐지되지 않습니다** — 백업본과 대조해야만 알 수 있습니다.

- `audit\` 와 `logs\` 를 사내 공유 드라이브에 **일 1회 동기화**
- `/verify-audit` 를 **월 1회** 실행하고 결과를 날짜별 보관

---

## 다음

- [개발문서](development.md) — 구현 상세, 알려진 한계
- [아키텍처](architecture.md) — 왜 이렇게 설계했는가
