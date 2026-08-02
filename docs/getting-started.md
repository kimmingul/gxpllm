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

**`.mcp.json` 을 고치지 마십시오. 환경변수로 설정합니다.**

`.mcp.json` 은 `${VAR:-기본값}` 형태로 되어 있어서, 환경변수를 설정하면
그 값이 이깁니다. 저장소 파일을 고치면 `git pull` 마다 충돌하고,
실수로 사내 주소를 커밋하게 됩니다.

| 환경변수 | 기본값 | 설명 |
|---|---|---|
| `GXPLLM_ENDPOINT` | `http://dgx-spark.internal:8001/v1` | OpenAI 호환 endpoint |
| `GXPLLM_MODEL` | `Qwen3.6-35B-A3B` | 서빙 중인 모델 이름 |
| `GXPLLM_API_KEY` | (없음) | 서버가 인증을 요구할 때만 |
| `GXPLLM_MAX_TOKENS` | `32768` | 응답 토큰 상한 |
| `GXPLLM_TIMEOUT_SEC` | `300` | 응답 대기 상한 (초) |
| `GXPLLM_ENCODING` | `utf-8` | MCP 서버 입출력 인코딩 |

설정 방법은 두 가지입니다. 자세한 설명과 주의사항은
[README 의 설정 절](../README.md#설정) 을 보십시오.

**방법 1 — Claude Code 설정 파일 (권장).** `~/.claude/settings.json` 의 `env` 에
넣습니다. 모든 프로젝트에 적용되므로 study 를 새로 만들 때마다 다시 설정할
필요가 없습니다. 기존 설정이 있으면 `env` 키만 추가하십시오.

```json
{
  "env": {
    "GXPLLM_ENDPOINT": "http://dgx-spark.internal:8001/v1",
    "GXPLLM_MODEL": "Qwen3.6-35B-A3B"
  }
}
```

프로젝트마다 다른 서버를 쓴다면 `.claude/settings.local.json` 에 같은 형태로
넣습니다. Claude Code 가 git 에서 자동으로 제외합니다.

**방법 2 — OS 환경변수.** 여러 도구가 같은 서버를 공유할 때 씁니다.

```powershell
setx GXPLLM_ENDPOINT "http://dgx-spark.internal:8001/v1"   # Windows
```

```bash
export GXPLLM_ENDPOINT="http://dgx-spark.internal:8001/v1"  # Linux / macOS
```

어느 방법이든 설정 후 Claude Code 를 다시 시작해야 적용됩니다.
`.mcp.json` 의 MCP 서버는 처음 한 번 승인이 필요하며,
`claude mcp list` 에서 `✔ Connected` 를 확인하십시오.

**`GXPLLM_MAX_TOKENS` 를 낮추지 마십시오.** 추론 모델은 추론과 본문이
같은 예산을 나눠 씁니다. 실측에서 인구통계 요약표 하나에 추론이 8,000 토큰을
넘게 썼습니다. 부족하면 응답이 잘리고, 서버는 이를 오류로 거부합니다.

**응답이 timeout 으로 실패하면 `GXPLLM_TIMEOUT_SEC` 를 올리십시오.** 기본 300초입니다.
추론 모델은 요청에 따라 이 시간을 넘길 수 있습니다. 실측에서 `structure_text` 가
300초를 넘겨 실패했습니다. 다만 **상한 자체를 없애지는 마십시오** — 서버가
응답하지 않을 때 무한 대기하면 멈춘 것인지 기다리는 것인지 구분할 수 없습니다.

**API key 는 저장소에 두지 마십시오.** `.mcp.json` 의 `GXPLLM_API_KEY` 에는
기본값이 없습니다. 값이 필요하면 환경변수로만 주십시오.

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
