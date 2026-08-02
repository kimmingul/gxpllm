<div align="center">

# gxpllm

### 임상시험 데이터 분석. 데이터는 PC를 벗어나지 않고, 모든 실행에 증적이 남습니다.

로컬 LLM이 SAS/Python/R 코드를 쓰고, runner가 실행하며,
**오케스트레이터 모델은 환자 데이터를 볼 수 없습니다** — 프롬프트가 아니라 hook이 강제합니다.

**v0.1.0** · MIT · Claude Code plugin

`SAS 9.4` · `Python` · `R` · `CDISC SDTM/ADaM` · `local LLM`

</div>

> ### ⚠️ 아직 밸리데이션되지 않았습니다
>
> 이름에 GxP가 들어간다고 GxP 준수를 자동 달성하지 않습니다.
> 이 도구는 **증적을 자동 생산할 뿐**이며, 밸리데이션은 별도 작업입니다.
>
> 규제 제출 경로(CSR, safety signal, IND/NDA)에 쓰기 전에
> [환경 검증](docs/development.md)과 IQ/OQ/PQ를 완료하십시오.

---

## 왜 gxpllm인가

상용 LLM은 문제해결 능력이 뛰어나지만 임상 데이터를 밖으로 내보냅니다.
로컬 LLM은 데이터를 지키지만 능력이 부족합니다.

gxpllm은 **역할을 나눕니다.**

| 주체 | 담당 | 임상 데이터 |
|---|---|---|
| **Claude** (Opus) | Protocol / SAP / DMP 초안, Table·Figure 검토, CSR 문구 | **접근 불가** |
| **로컬 LLM** (Qwen3.6 on DGX Spark) | SAS / Python / R 코드 작성, 비정형 텍스트 정형화 | 접근 가능 |
| **SAS / Python / R** | 모든 계산 | 접근 가능 |
| **사람** | SAP 승인, 코드 검토, Independent Programming, 사인오프 | 접근 가능 |

**불변 원칙: LLM은 숫자를 만들지 않습니다.** 계산은 SAS/Python/R만 합니다.

이것은 새로운 시스템이 아니라 **도구 교체**입니다 —
통계 프로그래머가 SAS를 쓰던 자리에 LLM이 코드 작성을 돕고,
Medical Writer가 문서를 쓰던 자리에 Claude가 초안을 냅니다.

---

## 데이터 경계

**study 루트 안은 기본 거부입니다.** 허용 목록에 있는 것만 읽을 수 있습니다.

| 경로 | 읽기 | 쓰기 |
|---|---|---|
| `docs/` `programs/` `macros/` `spec/` `validation/` | 가능 | 가능 |
| `output/tables/` `output/figures/` | 가능 | 차단 |
| `logs/runs/*/manifest.json` `assertions.json` | 가능 | 차단 |
| `audit/` `.gxpllm/` | 가능 | 차단 |
| **`data/`** | **차단** | **차단** |
| **`output/listings/`** | **차단** | **차단** |
| **`logs/runs/*/stdout.txt` `execution.log`** | **차단** | **차단** |

**Table과 Figure는 되고 Listing은 안 됩니다.** TLF 중 L만 성질이 다릅니다 — 피험자 단위입니다.

경계는 **PreToolUse hook**이 강제합니다. 차단 시도는 감사 로그에 남습니다.

---

## 빠른 시작

```bash
# 설치
/plugin marketplace add https://github.com/kimmingul/gxpllm
/plugin install gxpllm

# 로컬 LLM 서버 지정 (아래 "설정" 참조)

# 검증 (SAS·R·DGX Spark 없이도 전부 통과해야 합니다)
python tests/run_all.py

# study 생성
python scripts/init_study.py --root D:\clinical\ABC-301 --study-id ABC-301
cd D:\clinical\ABC-301\programs
claude
```

전체 흐름은 [시작하기](docs/getting-started.md) 와
[최소 예제](examples/minimal-study.md) 를 보십시오.

---

## 설정

**`.mcp.json` 을 고치지 마십시오.** 환경변수로 설정합니다.

`.mcp.json` 은 `${VAR:-기본값}` 형태로 되어 있어서 환경변수를 설정하면 그 값이
이깁니다. 저장소 파일을 직접 고치면 `git pull` 마다 충돌하고, 사내 서버 주소를
실수로 커밋하게 됩니다.

| 환경변수 | 기본값 | 설명 |
|---|---|---|
| `GXPLLM_ENDPOINT` | `http://dgx-spark.internal:8001/v1` | OpenAI 호환 endpoint |
| `GXPLLM_MODEL` | `Qwen3.6-35B-A3B` | 서빙 중인 모델 이름 |
| `GXPLLM_API_KEY` | (없음) | 서버가 인증을 요구할 때만 |
| `GXPLLM_MAX_TOKENS` | `32768` | 응답 토큰 상한 |
| `GXPLLM_ENCODING` | `utf-8` | MCP 서버 입출력 인코딩 |

### 방법 1 — Claude Code 설정 파일 (권장)

`~/.claude/settings.json` 의 `env` 에 넣습니다. 모든 프로젝트에 적용되므로
study 를 새로 만들 때마다 다시 설정할 필요가 없습니다.

```json
{
  "env": {
    "GXPLLM_ENDPOINT": "http://dgx-spark.internal:8001/v1",
    "GXPLLM_MODEL": "Qwen3.6-35B-A3B"
  }
}
```

기존 설정이 있으면 **`env` 키만 추가**하십시오. 파일 전체를 덮어쓰면
hook, statusLine, plugin 설정이 사라집니다.

프로젝트마다 다른 서버를 쓴다면 `.claude/settings.local.json` 에 같은 형태로
넣습니다. 이 파일은 Claude Code 가 git 에서 자동으로 제외하므로, 사내 주소가
커밋될 걱정이 없습니다.

### 방법 2 — OS 환경변수

여러 도구가 같은 서버를 공유할 때 씁니다.

```powershell
# Windows
setx GXPLLM_ENDPOINT "http://dgx-spark.internal:8001/v1"
setx GXPLLM_MODEL    "Qwen3.6-35B-A3B"
```

```bash
# Linux / macOS — ~/.bashrc 또는 ~/.zshrc
export GXPLLM_ENDPOINT="http://dgx-spark.internal:8001/v1"
export GXPLLM_MODEL="Qwen3.6-35B-A3B"
```

### 적용 확인

어느 방법이든 **Claude Code 를 재시작해야** 적용됩니다.
`.mcp.json` 의 MCP 서버는 처음 한 번 승인이 필요합니다.

```bash
claude mcp list        # local-coder 가 ✔ Connected 인지 확인
```

### 주의

- **API key 를 저장소에 두지 마십시오.** `.mcp.json` 의 `GXPLLM_API_KEY` 에는
  기본값이 없습니다. 값이 필요하면 위 방법으로만 주십시오.
- **`GXPLLM_MAX_TOKENS` 를 낮추지 마십시오.** 추론 모델은 추론과 본문이 같은
  토큰 예산을 나눠 씁니다. 실측에서 인구통계 요약표 하나에 추론이 8,000 토큰을
  넘게 썼습니다. 부족하면 응답이 잘리고, 서버는 이를 오류로 거부합니다.

---

## 작업 흐름

```
/build-dictionary          데이터 프로파일링 → Data Dictionary 초안
      ↓ (사람 검토)
/draft-sap                 Protocol + Dictionary → SAP 초안
      ↓ (통계책임자 승인)
/derive-assertions         SAP → 기계 판독 가능 검증 규칙
      ↓
/write-program t_14_1_1    SAP table shell → SAS/Python/R 코드
      ↓ (코드 검토)
/run-program               runner 실행 + 감사 증적
      ↓
/qc-program                Independent Programming (원 코드 미참조)
      ↓
/review-output             Table 검토 → CSR 문구 초안 (placeholder)
```

| Command | 용도 |
|---|---|
| `/build-dictionary` | Data Dictionary가 없는 의뢰 건에서 프로파일링 → 초안 |
| `/draft-protocol` `/draft-sap` `/draft-dmp` | 계획 문서 초안 |
| `/derive-assertions` | SAP → assertion 명세 |
| `/write-program` | table shell → 분석 프로그램 |
| `/run-program` | runner 실행 + 실패 시 수정 |
| `/qc-program` | Independent Programming |
| `/review-output` | Table/Figure 검토 → CSR 문구 |
| `/verify-audit` | 감사 로그 해시 체인 검증 |

---

## 감사 증적

runner가 자동으로 남깁니다. 사용자가 의식할 필요가 없습니다.

```
logs/runs/{run_id}/
  manifest.json      입출력 SHA-256, 로그 스캔, assertion 요약, 판정
  assertions.json    검증 결과
  execution.log      SAS .log 또는 runner 생성 로그
  execution.lst      SAS 출력

audit/audit.jsonl    HMAC-SHA256 해시 체인 (append-only)
```

### 백업이 필수입니다

해시 체인은 변조를 **탐지**할 뿐 **방지하지 못합니다.**
특히 **후미 절단은 탐지되지 않습니다** — 백업본과 대조해야만 알 수 있습니다.

- `audit/`와 `logs/`를 사내 공유 드라이브에 **일 1회 동기화**
- `/verify-audit`를 **월 1회** 실행하고 결과를 날짜별 보관

---

## 언어 선택

| 상황 | 선택 |
|---|---|
| **제출 경로** | **SAS** — 재현성이 가장 높고 `.log`가 규제 증적 |
| 원본이 `.sas7bdat` / `.xpt` | SAS |
| 탐색적 분석, 프로파일링 | Python |
| 생존분석 그림, 고급 그래픽 | R |

| | SAS 9.4 | Python | R |
|---|---|---|---|
| 로그 자동 생성 | **예** | 아니오 (runner 생성) | 아니오 (runner 생성) |
| 반환코드 신뢰도 | **낮음** — 로그 스캔 필수 | 높음 | 중간 |
| 환경 잠금 | 설치 버전 | `uv.lock` | `renv.lock` |
| 재현성 위험 | 낮음 | **높음** | **높음** |

---

## 검증

```bash
python tests/run_all.py
```

**배포 전 반드시 통과해야 합니다.** hook 스크립트에 구문 오류가 있으면
exit 1이 되어 경계가 열립니다.

| 스위트 | 내용 |
|---|---|
| `test_hooks.py` | 경계 차단 327건 (우회 시도 + 정상 작업 허용) |
| `test_false_positives.py` | 일상 명령 오탐 점검 — **오탐은 보안 이슈** |
| `test_audit.py` | 감사 체인 변조 탐지 9건 |
| `test_assert_api.py` | 3개 언어 assertion API 일치 + 문서 정합성 |
| `test_runners.py` | runner, assertion emitter, 미선언 산출물 탐지 |
| `test_mcp.py` | MCP 프로토콜, 위험 도구 미노출 |
| `test_llm_path.py` | 모의 vLLM 서버로 MCP → HTTP → 파싱 end-to-end |
| `test_benchmark.py` | 코드 생성 품질 실측 도구 동작 검증 |

SAS, R, DGX Spark 없이도 전부 통과합니다.

`test_false_positives.py`가 있는 이유: **정당한 작업이 막히면 사용자가 plugin을 끕니다.**
그게 가장 확실한 경계 붕괴입니다.

### 실제 PC에서

```bash
python scripts/verify_environment.py --study D:\clinical\DEMO-001
```

SAS 배치 실행, CP949 로그 인코딩, 로그 스캔 규칙, R runner, vLLM 연동을
**의도적 실패 케이스를 포함해** 확인합니다.
결과는 `validation/`에 저장되며 **CSV 문서(IQ/OQ)에 첨부해야 합니다.**

### 코드 생성 품질 실측 — 프로젝트 성립 여부를 좌우

```bash
python scripts/benchmark_codegen.py --init-cases benchmark/cases.yaml
# 실제 업무 프로그램 10개로 채우고 human_minutes 기록
python scripts/benchmark_codegen.py --cases benchmark/cases.yaml --study <경로>
```

| 결과 | 조치 |
|---|---|
| SAS 최종 성공률 ≥ 70% | 계획대로 3개 언어 모두 지원 |
| SAS만 Python 대비 30%p 이상 낮음 | **SAS 코드 작성은 Claude에게** (코드에는 피험자 데이터가 없음) |
| 전반적으로 낮음 | 범위를 `/build-dictionary`와 탐색적 분석으로 축소 |

**가장 중요한 지표는 "LLM 시간 vs 사람 직접 작성 시간"입니다.**
여기에 코드 검토 시간을 더했을 때 마이너스면 프로젝트가 성립하지 않습니다.

---

## 알려진 한계

[개발문서 §12](docs/development.md)를 참조하십시오.
**CSV 문서와 SOP에 그대로 옮겨야 합니다.**

- hook이 크래시하면 경계가 열립니다 (exit 2만 차단으로 처리됨) → 배포 전 테스트 필수
- 해시 체인의 후미 절단은 탐지되지 않습니다 → 백업본 대조 필요
- `programs/` → runner → `output/tables/` 덤프는 **탐지만 가능**하고 방지는 불가
- 사람이 화면을 보고 붙여넣는 것은 기술로 막을 수 없습니다
- Part 11 준수를 자동 달성하지 않습니다 — 증적을 자동 생산할 뿐입니다
- 제출 경로의 Independent Programming은 사람이 작성하는 것이 원칙입니다

**위협 모델**: 목적은 **실수 방지와 감사 증적 확보**입니다.
적대적 내부자나 프롬프트 인젝션된 모델을 완전히 막지는 못합니다.

적대적 검토 6회에서 확인된 우회 경로는 모두 막았지만, 각 라운드가
**앞 라운드 수정이 만든 새 구멍**을 찾았습니다.
경계 코드를 수정하실 때 `tests/run_all.py`를 반드시 통과시키십시오 —
되돌리면 이미 확인된 우회가 다시 열립니다.

---

## 문서

| 문서 | 내용 |
|---|---|
| [시작하기](docs/getting-started.md) | 설치, 첫 study, 작업 흐름 |
| [아키텍처](docs/architecture.md) | 왜 이렇게 설계했는가, 위협 모델 |
| [개발문서](docs/development.md) | hook·로깅·runner 상세 구현, 알려진 한계 |
| [최소 예제](examples/minimal-study.md) | 요약표 하나를 내는 전체 흐름 |
| [AGENTS.md](AGENTS.md) | 이 저장소에서 작업할 때의 규칙 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 기여 방법, 경계 변경 절차 |

---

## 관련

[**xllm**](https://github.com/kimmingul/xllm) — 크로스-벤더 LLM 교차검토.
gxpllm의 경계 설계는 xllm으로 grok-4.5와 6라운드 적대적 검토를 거쳐 만들어졌습니다.
