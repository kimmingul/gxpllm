# 임상 데이터 분석용 Claude Code Plugin 개발문서

**대상 시스템**: SAS 9.4 (개인 PC 설치) + Python + R
**로컬 LLM**: Qwen3.6-35B-A3B (NVIDIA DGX Spark, vLLM)
**오케스트레이터**: Claude Code (Opus)
**문서 버전**: 0.9 (2026-08-03) — 적대적 검토 6회 + 실서버 실측 1회 + 실사용 설치 검토 1회 반영

> **개정 이력**
>
> **0.1 → 0.2**: 초안의 차단 목록(blocklist) 방식이 1차 적대적 검토에서 뚫렸다.
> Opus 가 `programs/` 에 프로그램을 쓰고 runner 로 실행한 뒤 `logs/runs/*/stdout.txt` 를
> 읽으면 차단 규칙을 우회할 필요조차 없었다.
> **허용 목록(allowlist) 기본 거부**로 재설계했다 (§4.2).
>
> **0.2 → 0.3**: 2차 적대적 검토에서 재설계가 연 새 구멍이 발견됐다.
> - `.gxpllm/config.json` 을 고쳐 `allowed_dirs` 에 `data` 추가 → **정책 루트 붕괴**
> - `cmd.exe /c "python -c ..."` → 따옴표 안은 별도 토큰이 아니라 통과
> - audit 항목이 자기 `hash_alg` 를 고르므로 전체를 `sha256` 으로 다운그레이드하면 위조 통과
> - `logs/` allowlist 파일에 **쓰기**가 가능해 manifest 위조 가능
> - 오탐 12건 (`ruff check programs/python` 등이 차단되어 사용자가 plugin 을 끌 동기 발생)
>
> 조치: 정책을 plugin 코드에 고정(설정으로 덮어쓰기 불가), 셸 래퍼 전면 차단,
> 인터프리터 판정을 첫 토큰 기준으로 변경(오탐 해소), HMAC 강제,
> 읽기/쓰기 분리, 미선언 산출물 탐지 추가.
>
> **0.3 → 0.4**: 3차 검토에서 0.3 의 완화 조치가 만든 새 구멍이 발견됐다.
> - `env python programs/payload` (확장자 없음) → first-token-only 판정을 빠져나감
> - `set PY=python && %PY% -c "..."` → 변수 확장 후에야 인터프리터가 드러남
> - `git log -p` → 커밋된 데이터가 패치로 노출
> - 미선언 산출물 감시가 `output/`, `docs/` 만 커버 → `macros/leak.csv` 로 우회
> - `os.utime` 으로 mtime 복원 시 변경 탐지 실패
> - 새 오탐: `echo update the data dictionary` 가 bare 토큰 `data` 로 차단됨
>
> 조치: 접두 래퍼(`env`/`call`/`timeout`/`nice`) 해체 후 재판정, 변수 확장 전면 차단,
> `git log -p` 차단, 감시 범위를 **쓰기 가능한 모든 디렉터리**로 확대,
> mtime → **내용 해시** 비교, bare 토큰 검사를 파일 읽기 명령으로 한정.
>
> **0.4 → 0.5**: 4차 검토에서 **목록 기반 방어의 한계**가 드러났다.
> - `is_write_tool` 의 부분 문자열 매칭 → `catalog_write`(cat), `ViewEdit`(view),
>   `ReadWrite`(read) 가 읽기 모드로 판정되어 `.gxpllm/` `audit/` 쓰기가 통과
> - 접두 래퍼 목록 → `env.exe`, `timeout /T 5`, `busybox`, `taskset`, `firejail`,
>   `systemd-run`, `pipenv run`, depth 6 중첩 등 끝없이 우회 가능
> - `git -C . show HEAD:data/raw/x.csv`, `git --no-pager log -p` → 전역 옵션이 끼면
>   정규식이 하위명령을 못 찾음
> - `for %A in (python) do %A payload`, `wmic`, `mshta`, `setx` → 간접 실행
> - 변수 확장 전면 차단이 만든 오탐: `echo Today is %DATE%`, `set PURPOSE=exploratory`
>
> 조치 — **목록에서 구조로 전환**:
> - 도구 판정: 부분 문자열 → **정확히 일치** + 쓰기 표지 우선
> - 인터프리터 판정: 래퍼 목록 → **모든 토큰 검사 + 경로 구분자로 실행파일 판별** (§4.4)
> - git: 정규식 → **전역 옵션 건너뛴 뒤 하위명령 토큰 대조**
> - 변수 확장: 전면 차단 → **명령 위치(구간 첫 토큰)일 때만** 차단
>
> **0.5 → 0.6**: 5차 검토에서 4차 구조 변경의 빈틈이 드러났다.
> - `command python -c print(1)` — POSIX `command` 는 인자를 **실제로 실행**하는데
>   비실행 목록에 넣어 검사를 통째로 건너뛰었다
> - `C:\Python310\python payload` — "Windows 인터프리터는 항상 `.exe`" 가정이 틀렸다
>   (py 런처, 포터블 배포). `..\python`, `C:\Windows\py -3 -c` 도 통과했다
> - `git -c alias.x=!python x payload` — alias 로 하위명령·외부 명령 우회
> - `env git show HEAD` — git 검사가 첫 토큰일 때만 동작했다
> - `call %PY% -c` — 변수 확장 검사가 구간 선두만 봤다
> - `busybox ash`, `git whatchanged -p`, `git diff HEAD~1 HEAD`
> - 새 오탐: `echo see logs/runs for results` 가 경로 패턴에 걸렸다
>
> 조치:
> - `command` 를 비실행 목록에서 제거, `-v`/`-V` 조회 형태만 예외
> - 인터프리터 경로 판정: `.exe` 유무 → **study 하위 경로인가** (§4.4)
> - git: 모든 위치에서 탐색, `alias.` 전면 차단, `whatchanged`/`--full-diff`/다중 ref diff 추가
> - 변수 확장: 구간 선두 → **모든 토큰** (비실행 명령은 예외)
> - `echo` 등 비실행 명령은 경로 검사에서 제외
>
> **0.6 → 0.7**: 6차 검토에서 5차 완화 조치의 부작용이 드러났다.
> - `programs/leak/python payload` — "study 하위면 인터프리터가 아니다" 예외가
>   **쓰기 가능 디렉터리**(`programs/`, `macros/`)와 겹쳐, 포터블 인터프리터를
>   심어 runner·감사 체인을 우회할 수 있었다
> - `git diff HEAD~1..HEAD` — 범위 표기는 토큰 1개라 개수 검사를 빠져나갔다
> - `call %PY:~0% -c` — Windows 변수 부분 확장 미탐
> - 새 오탐: `ruff check ./programs/python`, `git commit -m "update alias.docs"`,
>   `git diff HEAD -- programs/x.sas`
>
> 조치:
> - study 하위 예외를 **표준 언어 디렉터리 6개로만** 한정 (`KNOWN_LANGUAGE_DIRS`)
> - git 범위 표기(`..`, `...`, `^!`) 차단, `--` 뒤는 경로로 취급
> - `alias.` 판정을 `-c` / `--config` / `config` 다음 토큰으로 좁힘
> - 변수 확장 정규식에 부분·치환 확장 포함
> - `mksh`, `yash`, `xonsh`, `busybox` 등 셸 추가
>
> **0.7 → 0.8**: 처음으로 **실제 로컬 LLM 서버**(Qwen3.6-35B-A3B,
> lemonade-server)에 붙여 측정한 결과, 그때까지 모의 서버로는 볼 수 없던
> 조용한 실패 두 건이 드러났다. 경계 우회가 아니라 **검증 장치의 소실**이다.
>
> - `call_llm` 이 `finish_reason` 을 확인하지 않았다. 추론 모델은 추론과 본문이
>   **같은** `max_tokens` 예산을 나눠 쓴다. 실측: 인구통계 요약표 요청 하나에
>   reasoning 26,023자 / content 859자, `completion_tokens` 8,192 로 예산을
>   전부 소진하고 `finish_reason=length`. 잘린 소스가 정상 반환되었다.
>   모의 서버는 언제나 `finish_reason=stop` 을 냈으므로 잡히지 않았다.
> - `decide_result` 가 실패 수만 보고 총 건수를 보지 않아 **assertion 0건이
>   PASSED** 였다. 잘린 프로그램에는 assertion 호출이 남아 있지 않으므로
>   실패할 것도 없다. "검증이 통과했다" 와 "검증이 없었다" 를 구분하지 못했다.
>
> 두 결함은 하나의 시나리오로 이어진다 — 잘린 프로그램이 저장되고, 실행되고,
> assertion 없이 PASSED 로 보고되고, 감사 증적에는 정상 run 으로 남는다.
>
> 조치:
> - `finish_reason` 을 **allowlist(`stop`)** 로 판정. `length` 만 막으면
>   `content_filter`, 필드 누락, 프록시 고유 값이 통과한다
> - 본문이 `None` 이거나 공백뿐이면 거부 (`None` 만 우연히 걸리던 비대칭 제거)
> - assertion 0건은 실패. `qc`/`submission_candidate` 는 예외 없음,
>   `exploratory` 는 `--allow-no-assertions` 로만 열리고 증적에 남는다
> - `GXPLLM_MAX_TOKENS` 추가, 기본 8,192 → 32,768.
>   **상향은 완화책이지 안전장치가 아니다** — 추론이 예산을 다시 잠식할 수 있다.
>   자동 토큰 배증 재시도는 넣지 않는다 (지연·비용 폭탄)
> - `structure_text` 의 JSON 완결성 검사 (코드와 달리 JSON 은 검사가 저렴하다)
>
> 검토: grok-4.5 와 codex(GPT-5.6) 교차 검토. 두 검토 모두 1차 제안
> (`length` 거부 + 토큰 상향)만으로는 불충분하다고 판정했고,
> `decide_result` 의 0-assertion 경로를 독립적으로 지목했다.
>
> **오탐 주의**: 0-assertion 실패는 프로파일링처럼 assertion 이 필요 없는
> 정당한 작업을 막을 수 있다. 예외는 LLM 이 쓰는 META 가 아니라
> **runner CLI 플래그**(신뢰 경계 안)로만 연다.
>
> **검증**: `tests/run_all.py` — 경계 **327건**, 감사 체인 9건,
> assertion API 일치, runner, MCP, **LLM 경로(모의 서버)**, 실측 도구 전부 통과.
> 오탐 프로브 44건 통과.
> 실서버 검증은 `tests/test_live_llm.py` (서버가 있는 PC 에서 별도 실행).
>
> **0.8 → 0.9**: plugin 을 실제로 설치해 개발하는 과정에서 세 결함이 드러났다.
> 적대적 검토가 아니라 **평범한 개발 작업**에서 나왔다는 점이 공통점이다.
>
> - **`RUNNER_ALLOW_PATTERN` 이 앵커 없는 `search()` 였다.** 명령 어디에든
>   runner 경로 문자열이 있기만 하면 구간 전체가 인터프리터·변수확장·재귀탐색
>   검사에서 면제됐다. 주석 한 줄이면 충분했다.
>   ```
>   python -c "print(1)" # scripts/run_sas.py          <- 통과했다
>   python -c "print(1)" --note scripts/run_python.py  <- 통과했다
>   ```
>   조치: 구간 **선두 고정 + `match()`** 로 바꿨다 (§4.4).
>
> - **PowerShell 도구가 두 hook 을 모두 통과했다.** `guard_file_access` 는
>   `powershell` 을 guard_bash 가 담당한다고 보고 넘기는데, `hooks.json` 의
>   matcher 는 `"Bash"` 뿐이라 받는 쪽이 없었다. 위임과 배선이 어긋난 것이다.
>   조치: matcher 를 `"Bash|PowerShell"` 로 고치고, 위임 목록을
>   `SHELL_TOOL_NAMES` 상수로 뺀 뒤 `test_hooks.py` 의 `test_hook_wiring()` 이
>   둘의 일치를 검사하게 했다. 배선을 빼먹으면 테스트가 실패한다.
>
> - **오탐: 이 저장소 자신의 필수 명령이 막혔다.** `python tests/run_all.py` 와
>   `python scripts/verify_environment.py` 는 CLAUDE.md 와 CONTRIBUTING.md 가
>   지정한 명령인데 인터프리터 차단에 걸렸다. 개발자가 문서대로 못 하면
>   plugin 을 끄게 되고, 그것이 가장 확실한 경계 붕괴다.
>   조치: `RUNNER_ALLOW_PATTERN` 에 이름을 **추가하지 않고** 별도의
>   `DEV_COMMAND_PATTERN` 을 만들었다 (§4.4). 위 첫 번째 결함 때문이다 —
>   그쪽에 넣었다면 같은 우회 창을 넓히는 셈이 된다.
>
> 함께 고친 것:
> - hook 이 내보내는 모든 메시지를 **영어로** 바꿨다. Windows 한국어 콘솔은
>   cp949 라 한글 차단 사유가 깨져서 읽을 수 없었다. **차단 이유를 못 읽으면
>   오탐인지 정당한 차단인지 판단조차 못 한다** — 차단 자체보다 나쁘다.
>   `gxpllm/core.py` 의 차단 사유 문자열도 hook 이 그대로 출력하므로 함께 바꿨다.
> - `tests/run_all.py` 가 cp949 콘솔에서 `UnicodeEncodeError` 로 죽었다.
>   요약 문자열의 em dash 하나 때문이었다. 출력 스트림을 utf-8 로 고정했다.
>
> **검증**: `tests/run_all.py` — 경계 **329건**(327 + hook 배선 2건),
> 오탐 프로브 일상 53건 / 차단 29건, 나머지 전부 통과.
>
> **알려진 제약**: `DEV_COMMAND_PATTERN` 은 구간 선두부터 끝까지 정확히
> 일치해야 하고 장옵션(`--flag`)만 허용한다. 따라서 리디렉션이나 파이프를
> 붙인 형태(`python tests/run_all.py 2>&1 | tail`)는 여전히 막힌다.
> 앵커를 풀면 위 첫 번째 결함과 같은 구멍이 다시 열리므로 **의도한 제약**이다.
> 그냥 `python tests/run_all.py` 로 실행하십시오.

---

## 1. 목적과 범위

### 1.1 무엇을 만드는가

기존 통계 프로그래머의 SAS 기반 업무 흐름을 유지하면서, 코드 작성을 로컬 LLM에게 위임하고
Protocol / SAP / DMP 등 계획 문서 작성과 결과 검토를 Opus에게 위임하는 Claude Code plugin.

### 1.2 역할 분담

| 주체 | 담당 | 임상 데이터 접근 |
|---|---|---|
| **Opus** (Claude Code 본체) | Protocol, SAP, DMP 초안 작성 / Data Dictionary 검토 / Table·Figure 검토 / CSR 문구 초안 | **불가** (hook으로 차단) |
| **로컬 LLM** (Qwen3.6) | SAS / Python / R 코드 작성, 데이터 구조 파악, 이상치 탐지, 비정형 텍스트 정형화 | 가능 |
| **SAS / Python / R** | 모든 계산 | 가능 |
| **사람** | Protocol·SAP 승인, 코드 검토, Independent Programming(QC), 최종 사인오프 | 가능 |

**불변 원칙**: LLM은 숫자를 만들지 않는다. 숫자는 SAS / Python / R만 만든다.

### 1.3 데이터 경계

**study 루트 안은 기본 거부(default-deny)다.** 허용 목록에 있는 것만 Opus 가 읽을 수 있다.

| 경로 | Opus 접근 | 비고 |
|---|---|---|
| `docs/` | **가능** | Protocol, SAP, DMP, Data Dictionary, define.xml |
| `programs/`, `macros/`, `templates/`, `scripts/` | **가능** | 코드에는 피험자 데이터가 없다 |
| `output/tables/` | **가능** | 집계 표 |
| `output/figures/` | **가능** | 그림 (개별 피험자 궤적 plot 은 판단 필요) |
| `audit/`, `.gxpllm/` | **가능** | 구조화 메타데이터, 설정 |
| `logs/runs/*/manifest.json` | **가능** | 실행 메타데이터 |
| `logs/runs/*/assertions.json` | **가능** | 검증 결과 |
| `logs/env_snapshot.json` | **가능** | 환경 스냅샷 |
| **`data/`** | **차단** | 원본 및 파생 데이터셋 |
| **`output/listings/`** | **차단** | 피험자 단위 목록 |
| **`logs/runs/*/stdout.txt`, `stderr.txt`** | **차단** | 프로그램 출력에 데이터 값이 담긴다 |
| **`logs/runs/*/execution.log`, `execution.lst`** | **차단** | SAS `.log` / `.lst` 원문 |
| **그 밖의 모든 경로** | **차단** | 기본 거부 |

**Table 과 Figure 는 되고 Listing 은 안 된다** — TLF 중 L 만 성질이 다르다.

study 루트 **밖**은 기본 허용이다. 단 임상 데이터 고유 확장자
(`.sas7bdat`, `.sas7bndx`, `.xpt`, `.sav`, `.dta` 등)는 위치와 무관하게 항상 차단한다.

이 경계는 프롬프트가 아니라 **PreToolUse hook**으로 강제한다 (§4).

---

## 2. 전체 구조

```
┌─────────────────────────── 직원 PC (Windows) ───────────────────────────┐
│                                                                          │
│  Claude Code (Opus)                                                      │
│    ├── hooks/   ← 경계 강제 + 자동 감사 로그                             │
│    ├── commands/ ← /build-dictionary, /draft-sap, /write-program ...     │
│    └── MCP ──────────────────────┐                                       │
│                                   │                                      │
│  scripts/                         │                                      │
│    ├── run_sas.py  ──→ sas.exe   │                                       │
│    ├── run_python.py              │                                      │
│    └── run_r.py                   │                                      │
│                                   │                                      │
│  D:\clinical\{STUDY}\             │                                      │
│    data\ output\ logs\ audit\     │                                      │
└───────────────────────────────────┼──────────────────────────────────────┘
                                    │ 사내망 HTTP
                          ┌─────────▼─────────┐
                          │  DGX Spark        │
                          │  vLLM (Qwen3.6)   │
                          │  프롬프트 로깅 OFF │
                          └───────────────────┘
```

---

## 3. 디렉터리 표준

Study(또는 분석 의뢰 건)마다 아래 구조를 강제한다. hook과 runner가 이 구조를 전제한다.

```
D:\clinical\{STUDY_ID}\
├── .gxpllm\
│   ├── config.json              # STUDY_ID, 경로, 차단 규칙, SAS 경로
│   └── env\
│       ├── uv.lock              # Python 환경 잠금
│       └── renv.lock            # R 환경 잠금
│
├── data\                        # ★ Opus 접근 차단
│   ├── raw\                     #   원본 (읽기 전용, 변경 금지)
│   └── derived\                 #   파생 데이터셋
│
├── docs\                        # Opus 작업 영역
│   ├── protocol.md
│   ├── sap.md                   # table shell 포함
│   ├── dmp.md
│   └── data_dictionary.md       # /build-dictionary 산출물
│
├── programs\                    # Opus 접근 가능 (코드에는 데이터 없음)
│   ├── sas\
│   ├── python\
│   ├── r\
│   └── qc\                      # Independent programming
│
├── output\
│   ├── tables\                  # Opus 접근 가능
│   ├── figures\                 # Opus 접근 가능
│   └── listings\                # ★ Opus 접근 차단 (피험자 단위)
│
├── logs\
│   └── runs\{run_id}\
│       ├── manifest.json        # 실행 메타데이터 (§5.1)
│       ├── assertions.json      # 검증 결과 (§7)
│       ├── execution.log        # SAS .log / Python·R 로그
│       ├── execution.lst        # SAS 전용
│       ├── stdout.txt
│       └── stderr.txt
│
└── audit\
    └── audit.jsonl              # append-only 해시 체인 (§6)
```

### 3.1 `.gxpllm\config.json`

```json
{
  "study_id": "ABC-301",
  "root": "D:\\clinical\\ABC-301",
  "sas_exe": "C:\\Program Files\\SASHome\\SASFoundation\\9.4\\sas.exe",
  "sas_config": "C:\\Program Files\\SASHome\\SASFoundation\\9.4\\nls\\ko\\sasv9.cfg",
  "sas_log_encoding": "cp949",
  "blocked_paths": ["data", "output\\listings"],
  "blocked_extensions": [".sas7bdat", ".xpt", ".sas7bndx", ".csv", ".xlsx", ".rds", ".parquet"],
  "llm_endpoint": "http://dgx-spark.internal:8001/v1",
  "llm_model": "Qwen3.6-35B-A3B-NVFP4",
  "blinded": true
}
```

`blinded`가 `true`이면 hook이 치료군 변수(`TRT01A`, `TRTP`, `ARM`, `ACTARM` 등)를 포함한
Table 읽기도 차단한다. Database lock / 눈가림 해제 후 `false`로 변경한다.

---

## 4. Hook 구현

### 4.1 왜 hook인가

Claude Code의 hook은 **모델의 판단 밖에서 결정론적으로 실행**된다. 모델이 우회할 수 없다.
프롬프트로 "데이터를 읽지 마세요"라고 쓰는 것과 근본적으로 다르며, 이것이 규제 환경에서
경계를 주장할 수 있는 유일한 근거다.

| Hook | matcher | 용도 |
|---|---|---|
| `PreToolUse` | `*` | 임상 데이터 접근 차단 (모든 도구) |
| `PreToolUse` | `Bash` | runner 우회 실행 및 재귀 탐색 차단 |
| `PostToolUse` | `Bash` | 셸 명령 감사 기록 |
| `SessionStart` | — | 환경 스냅샷 (SAS/Python/R 버전, lock 해시) |

**matcher 가 `*` 인 이유**: 도구 이름을 나열하면 새 도구가 추가될 때 경계가 열린다.
초안(0.1)은 `Read|Edit|Write|NotebookEdit|Glob|Grep` 로 제한했는데,
검토에서 `MultiEdit` 이나 미지의 도구, 그리고 `file_path` 대신 `filePath` 같은
키 별칭으로 우회 가능함이 확인됐다.

### 4.2 경계 정책: 기본 거부 + 허용 목록

**초안(0.1)의 차단 목록 방식이 실패한 이유**

적대적 검토에서 다음 경로가 확인됐다. 차단 규칙을 우회할 필요조차 없었다.

```
1. Opus 가 Write 로 programs/python/_exfil.py 작성    ← programs/ 는 허용
2. Opus 가 Bash 로 runner 실행                          ← runner 는 허용
3. 프로그램이 데이터를 읽어 print()                     ← 프로그램은 데이터 접근 가능
4. Opus 가 logs/runs/{run_id}/stdout.txt 읽기          ← logs/ 가 차단 목록에 없었음
```

산출물 경로가 늘어날 때마다 차단 목록에 추가하는 방식은 반드시 누락된다.
따라서 **study 루트 안은 기본 거부**로 바꾸고, 명시적으로 허용한 것만 통과시킨다.

**구현 위치**: `gxpllm/core.py` 의 `classify_path()`

```
1. 임상 데이터 고유 확장자 → 위치 무관 항상 차단
2. study 루트 밖         → 기본 허용
3. logs/                 → 전체 차단, 3개 파일만 예외 허용
4. 허용 디렉터리 목록    → 없으면 차단 (기본 거부)
5. 눈가림 상태           → output/ 산출물의 치료군 관련 파일 차단
```

**설정으로 덮어쓸 수 있다** (`.gxpllm/config.json`)

```json
{
  "allowed_dirs": ["docs", "programs", "output/tables"],
  "allowed_log_files": ["manifest.json", "assertions.json"],
  "blocked_extensions": [".sas7bdat", ".xpt"]
}
```

경계를 넓히는 변경은 QA 검토를 거쳐야 한다.

### 4.3 검색 도구는 범위를 명시해야 한다

`Grep(pattern="USUBJID", path=<study_root>)` 는 study 트리 전체를 훑으며
`data/` 안의 피험자 단위 값을 매칭 결과로 반환한다. `path=data` 만 차단하면
한 단계 위 루트로 우회된다.

따라서 검색 도구(`Grep`, `Glob`)는 **허용 디렉터리를 명시한 경우에만** 통과한다.

| 호출 | 결과 |
|---|---|
| `Grep(pattern="X")` — 범위 미지정 | **차단** |
| `Grep(pattern="X", path=<study_root>)` | **차단** |
| `Grep(pattern="X", path=<study_root>/docs)` | 허용 |
| `Glob(pattern="**/*.sas")` — 범위 미지정 | **차단** |
| `Glob(pattern="**/*.sas", path=<study_root>/programs)` | 허용 |

구현: `gxpllm/core.py` 의 `classify_search_scope()`

### 4.3b 읽기와 쓰기를 분리한다

읽기 허용 목록과 쓰기 허용 목록이 다르다.

| 경로 | 읽기 | 쓰기 | 쓰기를 막는 이유 |
|---|---|---|---|
| `docs/` `programs/` `macros/` `templates/` `spec/` `validation/` | 가능 | **가능** | — |
| `.gxpllm/` | 가능 | **차단** | 설정을 고치면 경계 정책 자체를 자가 확장할 수 있다 |
| `audit/` | 가능 | **차단** | 차단 기록(`access_blocked`)을 말소할 수 있다 |
| `logs/runs/*/manifest.json` 등 | 가능 | **차단** | manifest 를 심어 감사 증적을 위조할 수 있다 |
| `output/tables/` `output/figures/` | 가능 | **차단** | 산출물은 runner 가 실행한 프로그램만 생성한다 |

**정책은 설정으로 덮어쓸 수 없다.** `allowed_dirs`, `allowed_log_files`,
`blocked_extensions` 는 `gxpllm/core.py` 에만 존재하며 `config.json` 의 같은 이름 키를
무시한다. `.gxpllm/` 이 Opus 의 읽기 허용 영역인 이상, 설정을 신뢰하면 안 된다.

**읽기/쓰기 판정은 정확히 일치로 한다.**

부분 문자열 매칭을 쓰면 다음이 전부 읽기 모드로 빠져나간다 (4차 검토 발견).

```
catalog_write  → 'cat' 포함    → 읽기 모드 → .gxpllm/config.json 쓰기 통과
ViewEdit       → 'view' 포함   → 읽기 모드 → audit/ 쓰기 통과
ReadWrite      → 'read' 포함   → 읽기 모드 → output/tables/ 쓰기 통과
list_write     → 'list' 포함   → 읽기 모드
search_replace → 'search' 포함 → 읽기 모드
```

현재 규칙 (`hooks/guard_file_access.py`)

1. 도구명을 정규화한다 (소문자, `-`/`_`/공백 제거, MCP 는 마지막 조각만)
2. 쓰기 표지(`write`, `edit`, `replace`, `patch`, `save`, `delete`, …)가 있으면 **쓰기**
3. 정규화한 읽기 목록과 **정확히 일치**할 때만 읽기
4. 그 밖에는 전부 **쓰기** (모르는 도구는 fail-closed)

### 4.4 인터프리터 전면 차단

`python -c "..."` 는 임의 코드 실행 창이다. 경로를 문자열 결합으로 만들면
명령 문자열에 `data` 가 나타나지 않아 어떤 정규식으로도 잡을 수 없다.

```bash
# 이런 형태는 정규식으로 막을 수 없다
python -c "from pathlib import Path;a='da';b='ta';print((Path(root)/(a+b)/'raw'/'x').read_text())"
```

따라서 **runner 경유가 아닌 모든 인터프리터 실행을 차단**한다.
`python`, `python3`, `py`, `sas`, `Rscript`, `Rterm`, `R`, `node`, `perl`, `ruby`,
`ipython`, `uv run`, `conda run`, `poetry run`, `jupyter`.

**명령 체이닝 대응**: 명령을 `&&`, `||`, `;`, `|`, `&` 로 나눠 각 구간을 독립 판정한다.
`python scripts/run_sas.py --program x && python -c "..."` 는 두 번째 구간에서 차단된다.

**허용 판정은 반드시 앵커를 건다.** runner 허용은 구간 **선두에서 `match()`** 로만
한다. 앵커 없는 `search()` 였을 때 다음이 전부 통과했다.

```bash
python -c "print(1)" # scripts/run_sas.py           # 주석에 경로만 끼워 넣으면 면제
python -c "print(1)" --note scripts/run_python.py   # 인자에 넣어도 면제
```

허용 목록은 "이 문자열이 어딘가 있으면 봐준다" 가 아니라
"이 명령이 정확히 그것이면 봐준다" 여야 한다.

**개발용 명령 예외** (`DEV_COMMAND_PATTERN`): `python tests/run_all.py`,
`python tests/test_*.py`, `python scripts/verify_environment.py` 는 이 plugin 자신을
시험하는 명령이라 인터프리터 차단만 면제한다. runner 허용과 **분리해서** 둔다.
`RUNNER_ALLOW_PATTERN` 에 이름을 얹으면 구간 전체가 모든 검사에서 면제되지만,
이쪽은 세 겹으로 좁혔다.

1. 구간 선두부터 끝까지 정확히 일치 (`^...$`)
2. 인자는 장옵션(`--flag[=value]`)만 — `-c` / `-m` 이 들어올 수 없다
3. `check_direct_exec` 안에서만 면제 — 난독화·변수확장·git·재귀탐색·
   **데이터 경로** 검사는 그대로 적용된다

`python tests/run_all.py --study data/raw` 가 여전히 차단되는 이유가 3번이다.

**셸 래퍼 전면 차단**: `cmd`, `powershell`, `pwsh`, `sh`, `bash`, `wsl`, `wscript` 등.
래퍼는 두 가지 우회 통로를 연다.

```bash
powershell -File programs/exfil.ps1     # 스크립트 본문은 검사 대상이 아님
cmd.exe /c "python -c print(1)"          # 따옴표 안이 한 토큰이라 판정을 빠져나감
```

#### 인터프리터 판정: 목록이 아니라 구조로

**접두 래퍼 목록 방식은 실패했다.** 4차 검토에서 다음이 전부 통과했다.

```bash
env.exe python payload          # .exe 접미사
timeout /T 5 python payload      # Windows 플래그
env -u HOME python payload       # 옵션이 인자를 받음
env env env env env env python payload   # depth 상한 초과
busybox / taskset / numactl / firejail / systemd-run / nsenter / flock
pipenv run / pdm run / hatch run / parallel
```

래퍼는 끝없이 나온다. 목록으로는 못 막는다.

**현재 방식**: 모든 토큰을 검사하되, **경로 구분자로 실행 파일과 데이터 경로를 구분**한다.

| 조건 | 실행 파일 후보 | 예 |
|---|---|---|
| 경로 구분자 없음 | **예** | `python`, `env`, `busybox` |
| 실행 파일 확장자 | **예** | `C:/Python310/python.exe` |
| POSIX 절대경로 | **예** | `/usr/bin/python` |
| 명시적 상대 실행 | **예** | `./python` |
| basename 이 인터프리터인데 **표준 언어 디렉터리가 아님** | **예** | `C:/Python310/python`, `../python`, `programs/leak/python` |
| 표준 언어 디렉터리 | 아니오 | `programs/python`, `programs/r`, `programs/qc/sas` |

마지막 두 행이 핵심이다.

**"Windows 인터프리터는 항상 `.exe`" 가정은 틀렸다** — py 런처, 포터블 배포,
심볼릭 링크가 있다. 5차 검토에서 `C:\Python310\python payload` 가 통과했다.

그렇다고 모든 경로의 basename 을 검사하면 `ruff check programs/python` 이 막힌다.

**"study 하위면 예외" 도 틀렸다.** 6차 검토에서 `programs/leak/python payload` 가
통과했다. `programs/` 와 `macros/` 는 Opus 가 **쓸 수 있는** 영역이므로,
포터블 인터프리터를 심어 runner·감사 체인을 우회할 수 있다.

따라서 예외는 **표준 언어 디렉터리 6개로만** 한정한다 (`KNOWN_LANGUAGE_DIRS`).

```
programs/python   programs/r   programs/sas
programs/qc/python   programs/qc/r   programs/qc/sas
```

이 목록 밖의 어떤 경로든 basename 이 인터프리터면 차단된다.

**추가 예외**: 인자를 실행하지 않는 명령(`where`, `which`, `echo`, `printf`)은
인터프리터 검사에서 제외한다. `echo use SAS output` 이 차단되면 안 된다.

**`command` 를 여기에 넣으면 안 된다.** POSIX `command` 는 인자를 실제로 실행한다.
`command python -c print(1)` 이 검사를 통째로 건너뛴다.
`command -v` / `command -V` 조회 형태만 따로 허용한다.

**오탐이 왜 중요한가**: 정당한 작업이 막히면 사용자는 plugin 을 끈다.
그것이 가장 현실적인 메타 우회다. **오탐 해소는 보안 조치의 일부다.**
`tests/test_hooks.py` 에 일상 명령 허용 케이스를 두어 오탐 회귀를 보안 회귀와 동일하게 다룬다.

#### 변수 확장: 명령 위치만 차단

hook 은 `%PY%` 가 무엇으로 확장될지 알 수 없다. 그렇다고 전면 차단하면
`echo Today is %DATE%`, `set PURPOSE=exploratory`, `git log --format=%H` 이 모두 막힌다.

**인자를 실행하지 않는 명령에서만 허용**하고, 나머지는 모든 토큰을 검사한다.
구간 선두만 보면 `call %PY% -c` 가 통과한다 (5차 검토 발견).

| 명령 | 판정 |
|---|---|
| `set PY=python && %PY% -c "..."` | **차단** (두 번째 구간의 `%PY%`) |
| `call %PY% -c print(1)` | **차단** (접두가 붙어도 실행된다) |
| `!PY! -c print(1)` | **차단** (지연 확장도 동일) |
| `echo Today is %DATE%` | 허용 (echo 는 실행하지 않는다) |
| `echo check $nobs in log` | 허용 |
| `set PURPOSE=exploratory` | 허용 (대입일 뿐 실행이 아니다) |

#### git: 전역 옵션을 건너뛰고 하위명령을 찾는다

`\bgit\s+show\b` 같은 정규식은 `git -C . show` 나 `git --no-pager log -p` 에 빗나간다.
**git 토큰을 어느 위치에서든 찾고**, 전역 옵션(`-C`, `-c`, `--git-dir` 등,
값을 받는 것은 두 칸)을 건너뛴 뒤 실제 하위명령을 대조한다.

첫 토큰만 보면 `env git show HEAD` 나 `timeout 5 git log -p` 로 우회된다 (5차 검토 발견).

차단 대상
- 하위명령: `grep`, `show`, `cat-file`, `archive`, `bundle`, `format-patch`, `whatchanged`
- 플래그: `log -p/--patch/-u/-U<n>/--full-diff`, `stash show -p`, `diff --all`
- 커밋 참조가 둘 이상인 `git diff` (`git diff HEAD~1 HEAD`)
- **커밋 범위 표기** `..` `...` `^!` — 토큰이 하나라 개수 검사를 빠져나간다
  (`git diff HEAD~1..HEAD`, 6차 검토 발견)
- **`-c` / `--config` / `config` 다음의 `alias.`** — alias 로 하위명령이나
  외부 명령(`!python`)을 심을 수 있다

`--` 뒤는 전부 경로로 취급한다. `git diff HEAD -- programs/x.sas` 는 허용해야 한다.
`alias.` 를 부분 문자열로 찾으면 `git commit -m "update alias.docs"` 가 막힌다.

### 4.5 재귀 탐색 명령 차단

경로 리터럴에 `data` 가 없어도 결과적으로 `data/` 를 읽는 명령이 있다.

```bash
findstr /s /i USUBJID D:\clinical\ABC-301\*.*
Get-ChildItem -Path D:\clinical\ABC-301 -Recurse | ForEach-Object { Get-Content $_.FullName }
```

명령 형태 자체로 차단한다: `findstr /s`, `Get-ChildItem -Recurse`, `dir /s`,
`Select-String`, `rg`/`ack`/`ag`, `grep -r`, `find -exec`, `robocopy`/`xcopy`,
`Join-Path`, `Compress-Archive`/`tar`/`zip`, `for /d`, `ForEach-Object`.

### 4.2 `hooks/hooks.json`

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Read|Edit|Write|NotebookEdit|Glob|Grep",
        "hooks": [
          {
            "type": "command",
            "command": "python \"${CLAUDE_PLUGIN_ROOT}/hooks/guard_file_access.py\""
          }
        ]
      },
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python \"${CLAUDE_PLUGIN_ROOT}/hooks/guard_bash.py\""
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python \"${CLAUDE_PLUGIN_ROOT}/hooks/audit_append.py\""
          }
        ]
      }
    ],
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python \"${CLAUDE_PLUGIN_ROOT}/hooks/snapshot_env.py\""
          }
        ]
      }
    ]
  }
}
```

### 4.3 Hook 입출력 규약

Hook은 **stdin으로 JSON**을 받는다.

```json
{
  "session_id": "abc123",
  "transcript_path": "C:\\Users\\...\\transcript.jsonl",
  "cwd": "D:\\clinical\\ABC-301\\programs",
  "hook_event_name": "PreToolUse",
  "tool_name": "Read",
  "tool_input": { "file_path": "D:\\clinical\\ABC-301\\data\\raw\\adsl.sas7bdat" }
}
```

`PostToolUse`에는 `tool_response`가 추가된다.

**차단 방법 (두 가지, 어느 쪽이든 가능)**

| 방법 | 동작 |
|---|---|
| `exit 2` + stderr에 사유 | 도구 호출이 차단되고 stderr 내용이 Claude에게 전달됨 |
| stdout에 JSON + `exit 0` | `permissionDecision`으로 `deny`/`allow`/`ask` 지정 |

본 문서는 **exit 2 방식**을 표준으로 한다. 단순하고 실패 시 안전한(fail-closed) 쪽이다.

JSON 방식 예시(참고):

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "임상 데이터 디렉터리 접근은 차단됩니다."
  }
}
```

> **주의**: hook 스크립트 자체가 예외로 죽으면 차단이 풀릴 수 있다.
> 반드시 최상위 `try/except`로 감싸고, **예외 발생 시에도 exit 2로 차단**한다 (fail-closed).

### 4.4 `hooks/guard_file_access.py`

```python
"""
임상 데이터 접근 차단 hook (PreToolUse)

Claude Code(Opus)가 임상 데이터에 직접 접근하는 것을 차단한다.
- data\\ 디렉터리 전체 차단
- output\\listings\\ 차단 (피험자 단위 데이터)
- 데이터 파일 확장자 차단 (.sas7bdat, .xpt, .csv 등)
- 눈가림 상태에서는 치료군 변수를 포함한 산출물도 차단

차단 시 exit code 2, 사유는 stderr로 출력한다.
예외 발생 시에도 차단한다 (fail-closed).
"""

import json
import sys
import os
from pathlib import Path

# ============================================================================
# 설정 영역 - 여기서 수정하세요
# ============================================================================

CONFIG_FILENAME = '.gxpllm/config.json'
FILE_PATH_KEYS = ('file_path', 'path', 'notebook_path', 'pattern')

# config.json을 찾지 못했을 때 사용할 기본 차단 규칙
DEFAULT_BLOCKED_DIRS = ('data', 'output/listings')
DEFAULT_BLOCKED_EXTS = ('.sas7bdat', '.xpt', '.sas7bndx', '.csv', '.xlsx',
                        '.xls', '.rds', '.rdata', '.parquet', '.sav')

BLIND_KEYWORDS = ('trt01a', 'trt01p', 'trtp', 'trta', 'arm', 'actarm', 'unblind')

EXIT_ALLOW = 0
EXIT_BLOCK = 2


# ============================================================================
# 메인 로직
# ============================================================================

def find_study_root(start_dir):
    """
    현재 디렉터리에서 위로 올라가며 .gxpllm/config.json 을 찾는다

    Args:
        start_dir: 탐색 시작 디렉터리

    Returns:
        (study_root: Path, config: dict) 또는 (None, {})
    """
    current = Path(start_dir).resolve()
    for candidate in [current] + list(current.parents):
        config_path = candidate / CONFIG_FILENAME
        if config_path.is_file():
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    return candidate, json.load(f)
            except Exception:
                return candidate, {}
    return None, {}


def extract_target_paths(tool_input):
    """
    도구 입력에서 파일 경로 후보를 모두 추출한다

    Args:
        tool_input: hook이 받은 tool_input 딕셔너리

    Returns:
        경로 문자열 리스트
    """
    paths = []
    for key in FILE_PATH_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            paths.append(value)
    return paths


def is_blocked(target_path, study_root, config):
    """
    해당 경로가 차단 대상인지 판정한다

    Args:
        target_path: 검사할 경로 문자열
        study_root: study 루트 Path (없으면 None)
        config: config.json 내용

    Returns:
        차단 사유 문자열, 차단 대상이 아니면 None
    """
    normalized = target_path.replace('\\', '/').lower()

    # 1. 확장자 검사 (study_root 무관하게 항상 적용)
    blocked_exts = tuple(
        e.lower() for e in config.get('blocked_extensions', DEFAULT_BLOCKED_EXTS)
    )
    if normalized.endswith(blocked_exts):
        return f"임상 데이터 파일 확장자입니다: {Path(target_path).suffix}"

    # 2. 차단 디렉터리 검사
    blocked_dirs = [
        d.replace('\\', '/').lower()
        for d in config.get('blocked_paths', DEFAULT_BLOCKED_DIRS)
    ]

    if study_root is not None:
        try:
            resolved = Path(target_path).resolve()
            relative = resolved.relative_to(study_root.resolve())
            rel_str = str(relative).replace('\\', '/').lower()
        except (ValueError, OSError):
            rel_str = normalized
    else:
        rel_str = normalized

    for blocked in blocked_dirs:
        if rel_str == blocked or rel_str.startswith(blocked + '/') or f'/{blocked}/' in f'/{rel_str}':
            return f"차단된 디렉터리입니다: {blocked}"

    # 3. 눈가림 상태에서 치료군 관련 파일 차단
    if config.get('blinded', False):
        filename = Path(normalized).name
        for keyword in BLIND_KEYWORDS:
            if keyword in filename:
                return f"눈가림(blinded) 상태에서 치료군 관련 파일 접근은 차단됩니다: {keyword}"

    return None


def main():
    """메인 함수"""
    try:
        payload = json.load(sys.stdin)
    except Exception as exc:
        print(f"[gxpllm-guard] hook 입력 파싱 실패로 차단합니다: {exc}", file=sys.stderr)
        sys.exit(EXIT_BLOCK)

    try:
        tool_input = payload.get('tool_input', {}) or {}
        cwd = payload.get('cwd') or os.getcwd()

        study_root, config = find_study_root(cwd)
        targets = extract_target_paths(tool_input)

        for target in targets:
            reason = is_blocked(target, study_root, config)
            if reason:
                print(
                    f"[gxpllm-guard] 접근 차단\n"
                    f"  경로: {target}\n"
                    f"  사유: {reason}\n"
                    f"\n"
                    f"  임상 데이터는 Opus가 직접 읽을 수 없습니다.\n"
                    f"  데이터가 필요한 작업은 로컬 LLM에게 코드를 작성시키고\n"
                    f"  scripts/run_sas.py, run_python.py, run_r.py 로 실행하십시오.\n"
                    f"  집계 결과는 output/tables, output/figures 에서 읽을 수 있습니다.",
                    file=sys.stderr,
                )
                sys.exit(EXIT_BLOCK)

        sys.exit(EXIT_ALLOW)

    except SystemExit:
        raise
    except Exception as exc:
        # fail-closed: 판정 불가 시 차단
        print(f"[gxpllm-guard] 내부 오류로 차단합니다: {exc}", file=sys.stderr)
        sys.exit(EXIT_BLOCK)


if __name__ == "__main__":
    main()
```

### 4.5 `hooks/guard_bash.py`

Bash로 `type data\adsl.csv`, `sas.exe`, `python analysis.py` 등을 직접 실행하면
파일 접근 hook을 우회할 수 있다. 이를 막는다.

**핵심 정책 두 가지**

1. **데이터 경로를 참조하는 셸 명령 차단** — `type`, `more`, `Get-Content`, `findstr` 등
2. **SAS / Python / R 직접 실행 차단** — 반드시 runner를 경유해야 로그·manifest가 남는다

```python
"""
Bash 명령 검사 hook (PreToolUse)

두 가지를 차단한다.
- 임상 데이터 경로를 참조하는 셸 명령 (hook 우회 방지)
- SAS / Python / R 직접 실행 (runner 우회 방지 → 로그·manifest 누락 방지)

차단 시 exit code 2, 사유는 stderr로 출력한다.
"""

import json
import re
import sys
import os
from pathlib import Path

# ============================================================================
# 설정 영역 - 여기서 수정하세요
# ============================================================================

CONFIG_FILENAME = '.gxpllm/config.json'

# 데이터 경로 참조를 나타내는 패턴
DATA_PATH_PATTERNS = [
    r'[\\/]data[\\/]',
    r'[\\/]listings[\\/]',
    r'\.sas7bdat\b',
    r'\.xpt\b',
    r'\.sas7bndx\b',
    r'\.parquet\b',
    r'\.rds\b',
]

# runner를 경유하지 않은 직접 실행 패턴
DIRECT_EXEC_PATTERNS = [
    (r'\bsas(\.exe)?\b',                 'SAS'),
    (r'\bsaspy\b',                       'SAS (saspy)'),
    (r'\bRscript(\.exe)?\b',             'R'),
    (r'\bR\s+CMD\s+BATCH\b',             'R'),
    (r'(?<!run_)\bpython[0-9.]*(\.exe)?\s+\S+\.py\b', 'Python'),
]

# runner 호출은 허용
RUNNER_ALLOW_PATTERN = r'scripts[\\/]run_(sas|python|r)\.py'

EXIT_ALLOW = 0
EXIT_BLOCK = 2


# ============================================================================
# 메인 로직
# ============================================================================

def load_config(cwd):
    """
    상위 디렉터리를 탐색해 .gxpllm/config.json 을 읽는다

    Args:
        cwd: 탐색 시작 디렉터리

    Returns:
        config 딕셔너리 (없으면 빈 딕셔너리)
    """
    current = Path(cwd).resolve()
    for candidate in [current] + list(current.parents):
        config_path = candidate / CONFIG_FILENAME
        if config_path.is_file():
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                return {}
    return {}


def check_command(command):
    """
    셸 명령을 검사해 차단 사유를 반환한다

    Args:
        command: 실행하려는 셸 명령 문자열

    Returns:
        차단 사유 문자열, 문제 없으면 None
    """
    lowered = command.lower()

    # runner 경유는 무조건 허용
    if re.search(RUNNER_ALLOW_PATTERN, lowered):
        return None

    for pattern in DATA_PATH_PATTERNS:
        if re.search(pattern, lowered):
            return f"임상 데이터 경로를 참조합니다 (패턴: {pattern})"

    for pattern, label in DIRECT_EXEC_PATTERNS:
        if re.search(pattern, lowered):
            return (
                f"{label} 직접 실행은 차단됩니다. "
                f"runner를 경유해야 로그와 manifest가 기록됩니다."
            )

    return None


def main():
    """메인 함수"""
    try:
        payload = json.load(sys.stdin)
    except Exception as exc:
        print(f"[gxpllm-guard] hook 입력 파싱 실패로 차단합니다: {exc}", file=sys.stderr)
        sys.exit(EXIT_BLOCK)

    try:
        command = (payload.get('tool_input', {}) or {}).get('command', '')
        if not command:
            sys.exit(EXIT_ALLOW)

        reason = check_command(command)
        if reason:
            print(
                f"[gxpllm-guard] 명령 차단\n"
                f"  명령: {command[:200]}\n"
                f"  사유: {reason}\n"
                f"\n"
                f"  올바른 실행 방법:\n"
                f"    python scripts/run_sas.py    --program programs/sas/t_dm.sas\n"
                f"    python scripts/run_python.py --program programs/python/t_ae.py\n"
                f"    python scripts/run_r.py      --program programs/r/f_km.R",
                file=sys.stderr,
            )
            sys.exit(EXIT_BLOCK)

        sys.exit(EXIT_ALLOW)

    except SystemExit:
        raise
    except Exception as exc:
        print(f"[gxpllm-guard] 내부 오류로 차단합니다: {exc}", file=sys.stderr)
        sys.exit(EXIT_BLOCK)


if __name__ == "__main__":
    main()
```

> **한계를 명시할 것**: 정규식 기반 차단은 완전하지 않다. Base64 인코딩, 변수 치환,
> 우회 경로 표기 등으로 뚫릴 수 있다. 이 hook의 목적은 **악의적 우회 방지가 아니라
> 실수 방지와 감사 증적 확보**다. 실제 방어는 "Opus가 데이터를 볼 필요가 없는 워크플로"
> 자체에 있다. 이 한계는 CSV 문서에 반드시 기재한다.

### 4.6 `hooks/snapshot_env.py` (SessionStart)

세션 시작 시 환경을 한 번 캡처해 `logs/env_snapshot.json`에 기록한다.
매 run마다 다시 계산하지 않아도 되게 한다.

캡처 항목:

| 항목 | 취득 방법 |
|---|---|
| SAS 버전 | `sas.exe -version` 또는 `PROC PRODUCT_STATUS` 1회 실행 결과 |
| SAS 설치 경로 | `config.json`의 `sas_exe` |
| Python 버전 | `sys.version` |
| Python 패키지 | `uv.lock` 파일의 SHA-256 (전체 목록은 lock 파일 자체 보관) |
| R 버전 | `Rscript -e "cat(R.version.string)"` |
| R 패키지 | `renv.lock`의 SHA-256 |
| OS | `platform.platform()` |
| 사용자 | `os.getlogin()` |
| 호스트명 | `socket.gethostname()` |

---

## 5. Runner 구현

### 5.1 설계 원칙

**hook이 아니라 runner가 로그를 만든다.**

- runner: 실행 + 로그 캡처 + manifest 생성 + 감사 로그 append (실제 작업)
- hook: runner를 우회한 실행을 차단 (강제)

이렇게 나누면 hook 로직이 단순해지고, runner는 Claude Code 밖에서(예: 배치 스크립트)
호출해도 동일하게 동작한다.

### 5.1b 미선언 산출물 탐지

**남는 구조적 우회 경로가 하나 있다.**

```
1. Opus 가 programs/ 에 데이터를 읽어 output/tables/ 에 쓰는 프로그램 작성  ← 허용
2. runner 로 실행                                                          ← 허용
3. output/tables/leak.csv 읽기                                             ← 허용
```

각 단계가 모두 정당한 작업이라 경로만으로는 막을 수 없다.
프로그램이 데이터를 읽고 집계 산출물을 만드는 것이 이 시스템의 목적이기 때문이다.

**대응**: runner 가 실행 전후 산출물 디렉터리를 스냅샷 비교해
GXPLLM-META 에 선언되지 않은 파일 생성을 탐지하고 **실패로 처리**한다.

```
감시 대상: output/tables/, output/figures/, output/listings/, docs/
판정      : 선언되지 않은 새 파일 또는 변경된 파일 → FAILED
기록      : manifest.undeclared_outputs, 감사 로그 undeclared_output_count
```

이것은 **탐지이지 방지가 아니다.** 프로그램이 산출물을 선언하면 통과한다.
실질적 통제는 **코드 검토**이며, 프로그램은 로컬 LLM(MCP)이 작성하는 것이 원칙이다.
Opus 가 직접 프로그램을 작성하는 것은 SOP 위반으로 다루어야 한다.

### 5.2 공통 manifest 스키마

세 언어 모두 동일한 `manifest.json`을 생성한다.

```json
{
  "manifest_version": "1.0",
  "run_id": "20260802T143012-a3f9c1",
  "study_id": "ABC-301",
  "language": "sas",
  "program": {
    "path": "programs/sas/t_dm_summary.sas",
    "sha256": "9f2c...",
    "lines": 184
  },
  "user": "kimmingul",
  "hostname": "STAT-PC-07",
  "started_at": "2026-08-02T14:30:12+09:00",
  "finished_at": "2026-08-02T14:31:48+09:00",
  "duration_sec": 96.3,
  "exit_code": 0,
  "inputs": [
    {
      "path": "data/derived/adsl.sas7bdat",
      "sha256": "1a4b...",
      "bytes": 2841600,
      "mtime": "2026-07-28T09:12:03+09:00",
      "rows": 248
    }
  ],
  "outputs": [
    {
      "path": "output/tables/t_14_1_1.rtf",
      "sha256": "c7e0...",
      "bytes": 48210,
      "classification": "table"
    }
  ],
  "logs": {
    "execution_log": "logs/runs/20260802T143012-a3f9c1/execution.log",
    "execution_lst": "logs/runs/20260802T143012-a3f9c1/execution.lst",
    "log_sha256": "5b31..."
  },
  "log_scan": {
    "error_count": 0,
    "warning_count": 0,
    "critical_note_count": 2,
    "findings": [
      {"severity": "NOTE", "rule": "MERGE_REPEAT_BY", "line": 142,
       "text": "NOTE: MERGE statement has more than one data set with repeats of BY values."}
    ]
  },
  "assertions": {
    "path": "logs/runs/20260802T143012-a3f9c1/assertions.json",
    "total": 7,
    "passed": 7,
    "failed": 0
  },
  "environment": {
    "sas_version": "9.4 (TS1M7)",
    "python_version": null,
    "r_version": null,
    "env_snapshot_sha256": "aa19...",
    "os": "Windows-11-10.0.26200"
  },
  "blinded": true,
  "purpose": "exploratory",
  "sap_reference": "docs/sap.md#table-14-1-1",
  "audit_entry_hash": "e91f..."
}
```

**`purpose`**: `exploratory` | `qc` | `submission_candidate`
탐색적 분석과 제출 후보를 구분한다. `submission_candidate`는 assertion 전체 통과와
Independent Programming 대조를 요구한다.

### 5.3 SAS runner (`scripts/run_sas.py`)

#### 5.3.1 SAS 9.4 배치 실행

```
"C:\Program Files\SASHome\SASFoundation\9.4\sas.exe" ^
    -sysin  "D:\clinical\ABC-301\programs\sas\t_dm_summary.sas" ^
    -log    "D:\clinical\ABC-301\logs\runs\{run_id}\execution.log" ^
    -print  "D:\clinical\ABC-301\logs\runs\{run_id}\execution.lst" ^
    -nosplash ^
    -noterminal ^
    -sysparm "run_id={run_id};study_id=ABC-301" ^
    -work   "D:\sastemp\{run_id}"
```

| 옵션 | 의미 |
|---|---|
| `-sysin` | 실행할 `.sas` 프로그램 |
| `-log` | 로그 파일 경로 (지정하지 않으면 프로그램과 같은 위치에 생성) |
| `-print` | 출력(`.lst`) 경로 |
| `-nosplash` | 스플래시 화면 비표시 |
| `-noterminal` | 대화형 프롬프트 비활성. **필수** — 없으면 오류 시 배치가 멈춘다 |
| `-sysparm` | 프로그램에 `&SYSPARM`으로 전달. run_id를 SAS 코드에서 참조 가능 |
| `-work` | WORK 라이브러리 위치. run별로 분리하면 잔여물 충돌이 없다 |

**SAS 반환 코드**

| 코드 | 의미 | runner 처리 |
|---|---|---|
| 0 | 정상 종료 | 계속 |
| 1 | WARNING 발생 | 로그 스캔 결과와 함께 보고 |
| 2 | ERROR 발생 | 실패로 기록 |
| 3 이상 | 비정상 종료 (ABORT, 내부 오류) | 실패로 기록 |

**반환 코드만 믿으면 안 된다.** SAS는 논리적으로 치명적인 상황에서도 0을 반환하는 경우가 있다.
반드시 로그를 스캔한다 (§5.3.2).

#### 5.3.2 SAS 로그 스캔 규칙

임상 프로그래밍에서 통용되는 로그 QC 체크리스트를 코드로 구현한다.
`.log`의 각 줄을 검사해 아래 패턴을 찾는다.

| 심각도 | 패턴 | 왜 중요한가 |
|---|---|---|
| **ERROR** | `^ERROR` | 실행 실패 |
| **ERROR** | `^ERROR:` / `_ERROR_=1` | 데이터 스텝 오류 |
| **WARNING** | `^WARNING` | 대부분 조치 필요 |
| **CRITICAL NOTE** | `NOTE: MERGE statement has more than one data set with repeats of BY values` | **다대다 병합** — 임상 데이터에서 행 수가 조용히 늘어나는 최대 원인 |
| **CRITICAL NOTE** | `NOTE: Numeric values have been converted to character` | 타입 혼동 |
| **CRITICAL NOTE** | `NOTE: Character values have been converted to numeric` | 타입 혼동 |
| **CRITICAL NOTE** | `NOTE: Invalid numeric data` | 결측 처리 오류 유발 |
| **CRITICAL NOTE** | `NOTE: Missing values were generated as a result of` | 의도치 않은 결측 |
| **CRITICAL NOTE** | `NOTE: Division by zero detected` | 계산 오류 |
| **CRITICAL NOTE** | `NOTE: Mathematical operations could not be performed` | 계산 오류 |
| **CRITICAL NOTE** | `NOTE: Variable .* is uninitialized` | 변수명 오타 |
| **CRITICAL NOTE** | `NOTE: .* W\.D format was too small` | 표 값 절삭 |
| **CRITICAL NOTE** | `NOTE: The query requires remerging summary statistics` | PROC SQL 의도치 않은 remerge |
| **INFO** | `NOTE: There were 0 observations read` | 빈 데이터셋 — 필터 오류 의심 |
| **INFO** | `NOTE: The data set .* has 0 observations` | 빈 출력 |

**`submission_candidate` 실행에서는 ERROR·WARNING·CRITICAL NOTE가 하나라도 있으면 실패 처리**한다.
`exploratory`에서는 경고만 하고 통과시킨다.

#### 5.3.3 인코딩 처리

한국어 Windows 환경의 SAS 9.4는 로그를 **CP949(EUC-KR)**로 기록하는 경우가 많다.
UTF-8로 읽으면 깨지거나 예외가 발생한다.

```python
def read_sas_log(log_path, configured_encoding=None):
    """
    SAS 로그 파일을 인코딩 자동 감지로 읽는다

    Args:
        log_path: .log 파일 경로
        configured_encoding: config.json에 지정된 인코딩 (우선 시도)

    Returns:
        (로그 텍스트, 사용한 인코딩)
    """
    import chardet

    with open(log_path, 'rb') as f:
        raw = f.read()

    candidates = []
    if configured_encoding:
        candidates.append(configured_encoding)

    detected = chardet.detect(raw)
    if detected.get('encoding'):
        candidates.append(detected['encoding'])

    candidates.extend(['cp949', 'utf-8', 'latin-1'])

    for encoding in candidates:
        try:
            return raw.decode(encoding), encoding
        except (UnicodeDecodeError, LookupError):
            continue

    # 최후: 손실 허용
    return raw.decode('utf-8', errors='replace'), 'utf-8(replace)'
```

**manifest에는 원본 `.log`의 SHA-256을 기록**한다. 디코딩된 텍스트가 아니라 원본 바이트 기준이다.

#### 5.3.4 SAS 프로그램 쪽 규약

runner가 입력·출력을 자동으로 파악할 수 있도록, 생성되는 SAS 프로그램은 아래 헤더를 갖는다.
로컬 LLM에게 이 형식을 강제한다 (skill로 제공).

```sas
/*----------------------------------------------------------------------------
  GXPLLM-META-BEGIN
  program      : t_dm_summary.sas
  purpose      : Table 14.1.1 인구통계학적 특성 요약
  sap_ref      : docs/sap.md#table-14-1-1
  inputs       : data/derived/adsl.sas7bdat
  outputs      : output/tables/t_14_1_1.rtf
  analysis_set : Safety Set (SAFFL='Y')
  author       : local-llm/Qwen3.6-35B-A3B
  GXPLLM-META-END
----------------------------------------------------------------------------*/

%let RUN_ID = %scan(&SYSPARM, 1, %str(;));

libname indata "D:\clinical\ABC-301\data\derived" access=readonly;

/* --- assertion: 입력 행 수 기록 --------------------------------------- */
%gxpllm_assert_rowcount(indata.adsl, expected_min=1, label=ADSL_LOADED);

/* --- 분석군 필터 ------------------------------------------------------- */
data saf;
    set indata.adsl;
    where SAFFL = 'Y';
run;

%gxpllm_assert_rowcount(saf, expected_min=1, label=SAFETY_SET);
%gxpllm_assert_unique(saf, keys=USUBJID, label=SAF_UNIQUE_SUBJ);

/* ... 이하 분석 ... */
```

runner는 `GXPLLM-META-BEGIN/END` 블록을 파싱해 `inputs`, `outputs`를 manifest에 채운다.
선언된 output이 실제로 생성되지 않으면 실패로 기록한다.

#### 5.3.5 runner 골격

```python
"""
SAS 9.4 배치 실행 runner

SAS 프로그램을 배치 모드로 실행하고 감사 증적을 남긴다.
- run_id 생성 및 run 디렉터리 준비
- 입력 데이터셋 SHA-256 계산
- sas.exe 배치 실행
- .log 스캔 (ERROR / WARNING / CRITICAL NOTE)
- manifest.json 생성
- audit.jsonl 에 해시 체인 append
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

# ============================================================================
# 설정 영역 - 여기서 수정하세요
# ============================================================================

CONFIG_FILENAME = '.gxpllm/config.json'
META_BEGIN = 'GXPLLM-META-BEGIN'
META_END = 'GXPLLM-META-END'
SAS_WORK_ROOT = r'D:\sastemp'
HASH_CHUNK_SIZE = 1024 * 1024

LOG_RULES = [
    ('ERROR',    'SAS_ERROR',          r'^ERROR'),
    ('WARNING',  'SAS_WARNING',        r'^WARNING'),
    ('CRITICAL', 'MERGE_REPEAT_BY',    r'MERGE statement has more than one data set with repeats of BY values'),
    ('CRITICAL', 'NUM_TO_CHAR',        r'Numeric values have been converted to character'),
    ('CRITICAL', 'CHAR_TO_NUM',        r'Character values have been converted to numeric'),
    ('CRITICAL', 'INVALID_NUMERIC',    r'Invalid numeric data'),
    ('CRITICAL', 'MISSING_GENERATED',  r'Missing values were generated as a result of'),
    ('CRITICAL', 'DIVIDE_BY_ZERO',     r'Division by zero detected'),
    ('CRITICAL', 'MATH_FAILED',        r'Mathematical operations could not be performed'),
    ('CRITICAL', 'UNINITIALIZED',      r'Variable .+ is uninitialized'),
    ('CRITICAL', 'FORMAT_TOO_SMALL',   r'W\.D format was too small'),
    ('CRITICAL', 'SQL_REMERGE',        r'query requires remerging summary statistics'),
    ('INFO',     'ZERO_OBS_READ',      r'There were 0 observations read'),
    ('INFO',     'ZERO_OBS_OUT',       r'has 0 observations'),
]

SEVERITY_FAIL_ON = {
    'exploratory':          ('ERROR',),
    'qc':                   ('ERROR', 'WARNING'),
    'submission_candidate': ('ERROR', 'WARNING', 'CRITICAL'),
}


# ============================================================================
# 메인 로직
# ============================================================================

def make_run_id():
    """
    run_id 를 생성한다 (시각 + 랜덤 6자리)

    Returns:
        run_id 문자열 (예: 20260802T143012-a3f9c1)
    """
    stamp = datetime.now().strftime('%Y%m%dT%H%M%S')
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


def sha256_file(path):
    """
    파일의 SHA-256 을 계산한다

    Args:
        path: 파일 경로

    Returns:
        16진수 해시 문자열
    """
    digest = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            chunk = f.read(HASH_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def parse_meta_block(program_path):
    """
    SAS 프로그램의 GXPLLM-META 블록을 파싱한다

    Args:
        program_path: .sas 파일 경로

    Returns:
        메타데이터 딕셔너리 (inputs, outputs 는 리스트)
    """
    text = Path(program_path).read_text(encoding='utf-8', errors='replace')
    if META_BEGIN not in text or META_END not in text:
        return {}

    block = text.split(META_BEGIN, 1)[1].split(META_END, 1)[0]
    meta = {}
    for line in block.splitlines():
        if ':' not in line:
            continue
        key, _, value = line.partition(':')
        key = key.strip().lstrip('*').strip()
        value = value.strip()
        if key in ('inputs', 'outputs'):
            meta[key] = [v.strip() for v in value.split(',') if v.strip()]
        elif key:
            meta[key] = value
    return meta


def scan_sas_log(log_text):
    """
    SAS 로그를 스캔해 문제 항목을 찾는다

    Args:
        log_text: 디코딩된 로그 전체 텍스트

    Returns:
        findings 리스트. 각 항목은 severity, rule, line, text 를 갖는다
    """
    findings = []
    for line_no, line in enumerate(log_text.splitlines(), start=1):
        for severity, rule, pattern in LOG_RULES:
            if re.search(pattern, line):
                findings.append({
                    'severity': severity,
                    'rule': rule,
                    'line': line_no,
                    'text': line.strip()[:300],
                })
                break
    return findings


def run_sas(config, program_path, run_dir, run_id):
    """
    sas.exe 를 배치 모드로 실행한다

    Args:
        config: config.json 내용
        program_path: 실행할 .sas 경로
        run_dir: run 산출물 디렉터리 Path
        run_id: run 식별자

    Returns:
        (exit_code, log_path, lst_path)
    """
    log_path = run_dir / 'execution.log'
    lst_path = run_dir / 'execution.lst'
    work_dir = Path(SAS_WORK_ROOT) / run_id
    work_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        config['sas_exe'],
        '-sysin', str(program_path),
        '-log', str(log_path),
        '-print', str(lst_path),
        '-nosplash',
        '-noterminal',
        '-sysparm', f"run_id={run_id};study_id={config['study_id']}",
        '-work', str(work_dir),
    ]

    completed = subprocess.run(
        cmd,
        capture_output=True,
        text=False,
        cwd=config['root'],
    )

    (run_dir / 'stdout.txt').write_bytes(completed.stdout or b'')
    (run_dir / 'stderr.txt').write_bytes(completed.stderr or b'')

    return completed.returncode, log_path, lst_path


def main():
    """메인 함수"""
    print("=" * 80)
    print("SAS 9.4 배치 실행 runner")
    print("=" * 80)

    parser = argparse.ArgumentParser()
    parser.add_argument('--program', required=True, help='실행할 .sas 파일 경로')
    parser.add_argument('--purpose', default='exploratory',
                        choices=['exploratory', 'qc', 'submission_candidate'])
    args = parser.parse_args()

    # 1단계: 준비
    print(f"\n[1/6] 실행 환경 준비...")
    config = load_config(Path(args.program).parent)
    root = Path(config['root'])
    run_id = make_run_id()
    run_dir = root / 'logs' / 'runs' / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"  run_id: {run_id}")
    print(f"  purpose: {args.purpose}")

    # 2단계: 메타 파싱 및 입력 해시
    print(f"\n[2/6] 프로그램 메타데이터 파싱 및 입력 해시 계산...")
    meta = parse_meta_block(args.program)
    inputs = []
    for rel in meta.get('inputs', []):
        p = root / rel
        if p.is_file():
            inputs.append({
                'path': rel,
                'sha256': sha256_file(p),
                'bytes': p.stat().st_size,
                'mtime': datetime.fromtimestamp(p.stat().st_mtime).isoformat(),
            })
    print(f"  입력 데이터셋 {len(inputs):,}건")

    # 3단계: SAS 실행
    print(f"\n[3/6] SAS 실행 중...")
    started_at = datetime.now()
    exit_code, log_path, lst_path = run_sas(config, args.program, run_dir, run_id)
    finished_at = datetime.now()
    print(f"  종료 코드: {exit_code}")
    print(f"  소요 시간: {(finished_at - started_at).total_seconds():,.1f}초")

    # 4단계: 로그 스캔
    print(f"\n[4/6] SAS 로그 스캔...")
    log_text, encoding = read_sas_log(log_path, config.get('sas_log_encoding'))
    findings = scan_sas_log(log_text)
    counts = {}
    for f in findings:
        counts[f['severity']] = counts.get(f['severity'], 0) + 1
    for severity in ('ERROR', 'WARNING', 'CRITICAL', 'INFO'):
        if counts.get(severity):
            print(f"  {severity}: {counts[severity]:,}건")
    if not findings:
        print(f"  문제 없음")

    # 5단계: 출력 검증 및 manifest 생성
    print(f"\n[5/6] 출력 검증 및 manifest 생성...")
    # (선언된 outputs 존재 확인, 해시 계산, manifest.json 작성)

    # 6단계: 감사 로그 append
    print(f"\n[6/6] 감사 로그 기록...")
    # (audit.jsonl 해시 체인 append)

    # 판정
    fail_on = SEVERITY_FAIL_ON[args.purpose]
    failed = exit_code >= 2 or any(f['severity'] in fail_on for f in findings)

    print(f"\n{'=' * 80}")
    print(f"{'실패' if failed else '완료'} — run_id: {run_id}")
    print("=" * 80)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
```

### 5.4 Python runner (`scripts/run_python.py`)

SAS와 달리 Python은 로그를 자동 생성하지 않는다. runner가 만들어야 한다.

#### 5.4.1 실행 방식

```
uv run --locked --project D:\clinical\ABC-301 python programs\python\t_ae_summary.py
```

`--locked`를 붙이면 `uv.lock`과 다른 환경에서는 실행이 거부된다.
**재현성 확보의 핵심**이며, SAS가 공짜로 주던 안정성을 Python에서 대신 얻는 방법이다.

#### 5.4.2 로그 생성

SAS `.log`에 대응하는 것을 runner가 만든다. 프로그램 쪽 코드 수정 없이 자동으로 남기려면
runner가 subprocess의 stdout/stderr를 캡처하고, 여기에 헤더·푸터를 붙인다.

```
================================================================================
GXPLLM EXECUTION LOG
run_id      : 20260802T151203-7b2e44
study_id    : ABC-301
language    : python
program     : programs/python/t_ae_summary.py
program_sha : 4f81c2...
user        : kimmingul
hostname    : STAT-PC-07
started_at  : 2026-08-02T15:12:03+09:00
python      : 3.12.8
uv_lock_sha : 9ac31f...
purpose     : exploratory
================================================================================

--- INPUTS ---------------------------------------------------------------------
data/derived/adae.parquet   sha256=1a4b...  bytes=8,214,336  rows=3,847
data/derived/adsl.parquet   sha256=c209...  bytes=2,841,600  rows=248

--- STDOUT ---------------------------------------------------------------------
[1/4] 데이터 로드...
  ADSL: 248행
  ADAE: 3,847행
[2/4] Safety Set 필터링...
  248행 → 241행
...

--- STDERR ---------------------------------------------------------------------
(없음)

--- ASSERTIONS -----------------------------------------------------------------
PASS  ADSL_LOADED          rows=248 >= 1
PASS  SAFETY_SET           rows=241 >= 1
PASS  SAF_UNIQUE_SUBJ      duplicate keys=0
FAIL  AE_SUBJ_LE_DENOM     ae_subjects=243 > denominator=241

--- OUTPUTS --------------------------------------------------------------------
output/tables/t_14_3_1.rtf  sha256=c7e0...  bytes=48,210

--- SUMMARY --------------------------------------------------------------------
exit_code      : 0
duration_sec   : 12.4
assertions     : 3 passed / 1 failed
result         : FAILED (assertion)
finished_at    : 2026-08-02T15:12:16+09:00
================================================================================
```

#### 5.4.3 프로그램 쪽 규약

SAS의 `GXPLLM-META` 블록에 대응하는 것을 docstring으로 둔다.

```python
"""
Table 14.3.1 이상반응 요약 (TEAE)

GXPLLM-META-BEGIN
program      : t_ae_summary.py
purpose      : Table 14.3.1 SOC/PT 별 TEAE 요약
sap_ref      : docs/sap.md#table-14-3-1
inputs       : data/derived/adsl.parquet, data/derived/adae.parquet
outputs      : output/tables/t_14_3_1.rtf
analysis_set : Safety Set (SAFFL='Y')
count_unit   : subject
author       : local-llm/Qwen3.6-35B-A3B
GXPLLM-META-END
"""
```

runner가 이 블록을 파싱한다. **SAS·Python·R 모두 동일한 키를 쓴다.**

#### 5.4.4 예외 처리

Python 예외의 traceback에는 데이터 값이 딸려 나올 수 있다.

- **전체 traceback은 `logs/runs/{run_id}/execution.log` 에만** 기록한다 (PC 내부, Opus 차단 대상)
- Opus에게 전달되는 것은 **예외 타입 + 메시지 첫 줄 + 발생 파일:행번호**로 정제

```python
def sanitize_traceback(stderr_text):
    """
    traceback 에서 Opus 전달용 요약을 추출한다

    데이터 값이 포함될 수 있는 본문은 제거하고 위치 정보만 남긴다

    Args:
        stderr_text: 원본 stderr 텍스트

    Returns:
        정제된 요약 문자열
    """
    lines = stderr_text.strip().splitlines()
    if not lines:
        return ''

    exception_line = lines[-1]
    # "ValueError: cannot merge on 'USUBJID': found 3847 duplicates in ..." 같은 경우
    # 콜론 뒤 상세는 값이 섞일 수 있으므로 60자로 절단
    if ':' in exception_line:
        exc_type, _, detail = exception_line.partition(':')
        exception_line = f"{exc_type.strip()}: {detail.strip()[:60]}"

    location = ''
    for line in reversed(lines):
        m = re.search(r'File "([^"]+)", line (\d+)', line)
        if m:
            location = f"{Path(m.group(1)).name}:{m.group(2)}"
            break

    return f"{exception_line} ({location})" if location else exception_line
```

### 5.5 R runner (`scripts/run_r.py`)

#### 5.5.1 실행 방식

```
Rscript --vanilla programs\r\f_km_curve.R
```

`--vanilla`는 `.Rprofile`, `.RData`, 환경변수 로딩을 모두 끈다.
**재현성 확보에 필수** — 이전 세션의 워크스페이스가 결과에 영향을 주지 못하게 한다.

환경 잠금은 `renv`를 사용한다. 프로젝트 루트의 `renv.lock`을 manifest에 해시로 기록한다.

```r
renv::restore(prompt = FALSE)   # 실행 전 lock 상태로 복원
```

#### 5.5.2 R 특유의 주의점

| 항목 | 조치 |
|---|---|
| **경고가 조용히 지나감** | `options(warn = 1)` — 경고를 즉시 출력. `submission_candidate`에서는 `options(warn = 2)`로 경고를 오류로 승격 |
| **`stringsAsFactors`** | R 4.0 이상은 기본 FALSE지만, 명시적으로 지정하도록 강제 |
| **부동소수점 출력 자릿수** | `options(digits = 15)`로 로그에는 최대 정밀도 기록. 표시 반올림은 출력 단계에서만 |
| **난수** | `set.seed()` 필수. manifest에 seed 기록 |
| **세션 정보** | 프로그램 끝에서 `sessionInfo()` 출력 → 로그에 포함 |
| **locale** | `Sys.setlocale("LC_ALL", "Korean_Korea.949")` 등 명시. locale에 따라 정렬 순서가 달라진다 |

#### 5.5.3 R 프로그램 규약

```r
# GXPLLM-META-BEGIN
# program      : f_km_curve.R
# purpose      : Figure 14.2.1 무진행생존 Kaplan-Meier 곡선
# sap_ref      : docs/sap.md#figure-14-2-1
# inputs       : data/derived/adtte.parquet
# outputs      : output/figures/f_14_2_1.png
# analysis_set : Full Analysis Set (FASFL='Y')
# GXPLLM-META-END

options(warn = 1)
options(digits = 15)
set.seed(20260802)
Sys.setlocale("LC_ALL", "Korean_Korea.949")

source("scripts/gxpllm_assert.R")

# ... 분석 ...

gxpllm_assert_rowcount(adtte, expected_min = 1, label = "ADTTE_LOADED")
gxpllm_assert_unique(adtte, keys = c("USUBJID", "PARAMCD"), label = "TTE_UNIQUE")

gxpllm_write_assertions()   # assertions.json 기록
print(sessionInfo())
```

### 5.6 세 언어 비교 요약

| 항목 | SAS 9.4 | Python | R |
|---|---|---|---|
| 실행 | `sas.exe -sysin` | `uv run --locked python` | `Rscript --vanilla` |
| 로그 자동 생성 | **예** (`.log`) | 아니오 → runner가 생성 | 아니오 → runner가 생성 |
| 출력 파일 | `.lst` | 없음 | 없음 |
| 환경 잠금 | 설치 버전 고정 | `uv.lock` | `renv.lock` |
| 반환 코드 신뢰도 | 낮음 (로그 스캔 필수) | 높음 | 중간 (`warn=2` 권장) |
| 인코딩 함정 | **CP949 로그** | UTF-8 | locale 의존 |
| 재현성 위험 | 낮음 | **높음** (패키지 변동) | **높음** (패키지 변동) |

**재현성 위험이 SAS < R ≈ Python 순**이라는 점이 중요하다.
제출 경로(`submission_candidate`) 분석은 SAS를 유지하는 것이 방어에 유리하다.

---

## 6. 감사 로그 (`audit/audit.jsonl`)

### 6.1 해시 체인

append-only 파일이며, 각 항목이 이전 항목의 해시를 포함한다.
중간 항목을 수정·삭제하면 체인이 깨져 탐지된다. WORM 저장소 없이도 **변조 탐지**는 가능하다.

```json
{"seq":1,"ts":"2026-08-02T14:30:12+09:00","event":"run_started","run_id":"20260802T143012-a3f9c1","user":"kimmingul","language":"sas","program_sha256":"9f2c...","prev_hash":"0000...0000","entry_hash":"5a1c..."}
{"seq":2,"ts":"2026-08-02T14:31:48+09:00","event":"run_finished","run_id":"20260802T143012-a3f9c1","exit_code":0,"assertions_failed":0,"manifest_sha256":"e91f...","prev_hash":"5a1c...","entry_hash":"b7d2..."}
```

```python
def append_audit(audit_path, entry):
    """
    감사 로그에 항목을 append 하고 해시 체인을 유지한다

    Args:
        audit_path: audit.jsonl 경로
        entry: 기록할 딕셔너리 (prev_hash, entry_hash, seq 는 자동 부여)

    Returns:
        기록된 항목의 entry_hash
    """
    prev_hash = '0' * 64
    seq = 0

    if audit_path.is_file():
        with open(audit_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    last = json.loads(line)
        prev_hash = last['entry_hash']
        seq = last['seq']

    entry = dict(entry)
    entry['seq'] = seq + 1
    entry['prev_hash'] = prev_hash

    # entry_hash 는 자기 자신을 제외한 canonical JSON 의 해시
    canonical = json.dumps(entry, sort_keys=True, ensure_ascii=False,
                           separators=(',', ':'))
    entry['entry_hash'] = hashlib.sha256(canonical.encode('utf-8')).hexdigest()

    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with open(audit_path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(entry, ensure_ascii=False) + '\n')

    return entry['entry_hash']
```

### 6.1b HMAC 강제 — 다운그레이드 위조 차단

키가 존재하면 **모든 항목이 `hmac-sha256` 이어야 한다.**

항목이 자기 `hash_alg` 를 고르게 두면 다음 위조가 성립한다.

```
1. 정상 체인은 HMAC-SHA256 으로 서명되어 있다
2. 공격자가 키를 모른 채 전체를 hash_alg="sha256" 으로 재작성한다
3. 순수 SHA-256 으로 재서명한다
4. 검증기가 "이 항목은 sha256 이니까 키 없이 검증" → 통과
```

따라서 `verify_audit_chain()` 은 키가 있는데 항목이 HMAC 이 아니면
**알고리즘 다운그레이드**로 판정해 실패시킨다. `hash_alg` 필드를 지운 경우도 같다.

키 위치는 `~/.gxpllm/audit.key` (study 트리 **밖**). 환경변수 `GXPLLM_AUDIT_KEY_DIR` 로
변경할 수 있다. study 디렉터리만 조작해서는 위조가 성립하지 않는다.

### 6.2 기록 대상 이벤트

| event | 시점 |
|---|---|
| `session_started` | Claude Code 세션 시작 (SessionStart hook) |
| `access_blocked` | PreToolUse hook이 차단했을 때 |
| `run_started` | runner 실행 시작 |
| `run_finished` | runner 실행 종료 |
| `assertion_failed` | assertion 실패 |
| `output_promoted` | exploratory → submission_candidate 승격 |
| `dictionary_built` | `/build-dictionary` 실행 |

**`access_blocked`는 반드시 기록한다.** 경계가 실제로 작동했다는 증거이자,
누군가 반복적으로 데이터에 접근하려 했다는 신호다.

### 6.3 검증 도구

```
python scripts/verify_audit.py --study D:\clinical\ABC-301
```

체인 무결성, manifest 존재, 로그 파일 해시 일치를 확인한다.
**주기적으로(예: 월 1회) 실행해 결과를 보관**하면 감사 대응 증적이 된다.

### 6.4 백업

`logs/`와 `audit/`은 **직원 PC에만 두면 안 된다.** PC 고장 시 증적이 소실된다.
사내 공유 드라이브(또는 eTMF)에 일 1회 동기화한다. 이는 SAS 시절에도 필요했던 것과 동일하다.

---

## 7. Assertion 프레임 (3개 언어 공통)

### 7.1 설계

세 언어가 각각 assertion 로직을 구현하면 유지보수가 3배가 된다.
대신 **각 언어는 결과를 JSON으로 뱉기만 하고, 판정은 공통 Python 검증기가 한다.**

```
SAS  ──%gxpllm_assert_*──┐
Python ──gxpllm_assert.*─┼──→ logs/runs/{run_id}/assertions.json ──→ 공통 검증기
R    ──gxpllm_assert_*───┘
```

### 7.2 `assertions.json` 형식

```json
{
  "run_id": "20260802T143012-a3f9c1",
  "language": "sas",
  "assertions": [
    {
      "label": "SAFETY_SET",
      "rule": "rowcount",
      "dataset": "saf",
      "observed": 241,
      "expected": {"min": 1},
      "result": "PASS",
      "message": "rows=241 >= 1"
    },
    {
      "label": "AE_SUBJ_LE_DENOM",
      "rule": "reconciliation",
      "observed": 243,
      "expected": {"max": 241},
      "result": "FAIL",
      "message": "AE subject count 243 exceeds Safety Set denominator 241"
    }
  ]
}
```

### 7.3 표준 assertion 목록

SAP의 내용을 코드로 옮긴 것이며, **§7.4의 임상 정합성 검증이 핵심**이다.

**데이터 무결성 (기계적)**

| rule | 검사 내용 |
|---|---|
| `rowcount` | 데이터셋 행 수가 기대 범위 안인가 |
| `rowcount_delta` | 변환 전후 행 수 변화가 의도한 것인가 |
| `join_loss` | 병합에서 행이 유실되지 않았는가 (SAS의 다대다 MERGE 탐지) |
| `unique` | key 조합이 유일한가 (USUBJID, USUBJID+PARAMCD 등) |
| `domain` | 값이 허용 도메인 안인가 (SEX ∈ {M, F}) |
| `missingness` | 결측률이 기대 범위 안인가 |
| `date_order` | 날짜 순서가 논리적인가 (TRTSDT ≤ TRTEDT) |

**임상 정합성 (의미적) — 더 중요하다**

| rule | 검사 내용 |
|---|---|
| `analysis_set_flag` | 분석군 flag가 SAP 정의와 일치하는가 (SAFFL, FASFL, PPROTFL) |
| `denominator` | 분모가 해당 분석군의 unique subject 수와 일치하는가 |
| `reconciliation` | AE subject 수 ≤ 분모, arm별 합 = 전체 합 |
| `count_unit` | subject 카운트인가 event 카운트인가 (SAP 명시와 대조) |
| `teae_window` | TEAE 판정 window가 SAP 정의와 일치하는가 |
| `baseline_rule` | baseline 정의(마지막 non-missing pre-dose)가 적용됐는가 |
| `coding_version` | MedDRA / WHODrug 버전이 DMP 명시와 일치하는가 |

앞서 검토에서 지적된 대로, **기계적 assertion만으로는 "기계적으로 완전하지만 임상적으로 틀린 분석"을
막을 수 없다.** §7.4의 임상 정합성 검증이 반드시 있어야 한다.

### 7.4 SAP를 assertion으로 변환하는 흐름

```
docs/sap.md (Opus 작성, 사람 승인)
    ↓  /derive-assertions
docs/assertions_spec.yaml (SAP의 정의를 기계 판독 가능하게)
    ↓  로컬 LLM이 코드 생성 시 참조
programs/{sas,python,r}/*.{sas,py,R}  ← assertion 호출 포함
    ↓  실행
logs/runs/{run_id}/assertions.json
```

`assertions_spec.yaml` 예시:

```yaml
study_id: ABC-301
sap_version: "2.0"
analysis_sets:
  safety:
    flag: SAFFL
    value: "Y"
    expected_n: 241
  fas:
    flag: FASFL
    value: "Y"
tables:
  t_14_3_1:
    title: "이상반응 요약 (TEAE)"
    analysis_set: safety
    count_unit: subject
    denominator: safety.unique_subjects
    teae_window:
      start: TRTSDT
      end: "TRTEDT + 30"
    coding:
      dictionary: MedDRA
      version: "27.0"
    reconciliation:
      - "ae_subject_count <= denominator"
      - "sum(arm_counts) == overall_count"
```

### 7.5 각 언어의 emitter (최소 구현)

**SAS** — `programs/macros/gxpllm_assert.sas`

```sas
%macro gxpllm_assert_rowcount(ds, expected_min=, expected_max=, label=);
    %local nobs;
    proc sql noprint;
        select count(*) into :nobs trimmed from &ds;
    quit;

    %local result;
    %let result = PASS;
    %if %length(&expected_min) and &nobs < &expected_min %then %let result = FAIL;
    %if %length(&expected_max) and &nobs > &expected_max %then %let result = FAIL;

    data _null_;
        file "&GXPLLM_RUN_DIR./assertions.jsonl" mod encoding='utf-8';
        put '{"label":"' "&label" '","rule":"rowcount","dataset":"' "&ds"
            '","observed":' "&nobs" ',"result":"' "&result" '"}';
    run;
%mend;
```

각 언어는 `assertions.jsonl`에 한 줄씩 append하고, runner가 이를 모아
`assertions.json`으로 정리한다. 이 방식이면 언어별 구현이 20줄 내외로 끝난다.

**Python** — `scripts/gxpllm_assert.py`
**R** — `scripts/gxpllm_assert.R`

동일한 시그니처와 출력 형식을 유지한다.

---

## 8. MCP: 로컬 LLM 연결

### 8.1 `.mcp.json`

```json
{
  "mcpServers": {
    "local-coder": {
      "command": "python",
      "args": ["${CLAUDE_PLUGIN_ROOT}/mcp/local_coder_server.py"],
      "env": {
        "GXPLLM_ENDPOINT": "http://dgx-spark.internal:8001/v1",
        "GXPLLM_MODEL": "Qwen3.6-35B-A3B-NVFP4"
      }
    }
  }
}
```

### 8.2 노출할 도구

| 도구 | 입력 | 출력 |
|---|---|---|
| `write_program` | language, sap_ref, table_shell, data_dictionary, assertions_spec | 프로그램 소스 |
| `revise_program` | 기존 소스, assertion 실패 요약, SAS 로그 스캔 결과(정제본) | 수정된 소스 |
| `profile_data` | 데이터셋 경로 | 프로파일링 코드 (실행은 runner가) |
| `structure_text` | 자유기술 텍스트, 출력 JSON Schema | 정형화 결과 + evidence span |

**노출하지 않을 도구**: 파일 읽기/쓰기, 셸 실행, 임의 SQL, URL fetch.
로컬 LLM은 **코드를 생성만** 하고 실행은 runner가 한다.

### 8.3 vLLM 서버 설정 (DGX Spark)

```bash
vllm serve /models/Qwen3.6-35B-A3B-NVFP4 \
  --served-model-name Qwen3.6-35B-A3B-NVFP4 \
  --port 8001 \
  --max-num-seqs 4 \
  --max-model-len 32768 \
  --disable-log-requests \
  --host 0.0.0.0
```

**`--disable-log-requests`는 필수다.** 코드 작성을 돕기 위해 데이터 구조와 값 샘플이
프롬프트에 들어가는데, 기본 설정에서는 이것이 서버 로그에 평문으로 쌓인다.

vLLM 버전은 **정확한 커밋으로 고정**한다. `latest` 태그 사용 금지.
(Qwen3.6-35B-A3B-NVFP4 로딩 관련 이슈가 특정 버전에 존재했으므로 반드시 검증 후 고정)

---

## 9. Commands

### 9.1 `/build-dictionary` — 최우선 구현

Data Dictionary가 없는 의뢰 건에서 가장 먼저 실행한다.

```
1. 로컬 LLM이 프로파일링 프로그램 생성
   SAS  : PROC CONTENTS / PROC FREQ / PROC MEANS
   Python: pandas.info() / describe() / nunique()
2. runner가 실행 → 프로파일 결과를 logs/runs/{run_id}/profile.json 에 저장
3. 로컬 LLM이 프로파일을 읽고 Data Dictionary 초안 작성
   - 변수명, 타입, 길이, 레이블, 값 도메인, 결측률
   - 데이터셋 간 key 관계 추정
   - 변수의 임상적 의미 추정 (CDISC 표준 변수명 매칭 포함)
4. docs/data_dictionary.md 로 저장
5. 사람이 검토·보완
6. 검토 완료된 Dictionary는 피험자 데이터가 없으므로 Opus 접근 허용
```

**출력 형식** (`docs/data_dictionary.md`):

| 데이터셋 | 변수 | 타입 | 길이 | 레이블 | 값 도메인 | 결측률 | 추정 의미 | 검토 |
|---|---|---|---|---|---|---|---|---|
| ADSL | USUBJID | char | 20 | Unique Subject ID | 고유 248개 | 0.0% | 피험자 식별자 (CDISC 표준) | ✅ |
| ADSL | SAFFL | char | 1 | Safety Set Flag | Y(241), N(7) | 0.0% | Safety Set 포함 여부 | ✅ |
| ADSL | TRT01A | char | 20 | Actual Treatment | (눈가림) | 0.0% | 실제 배정 치료군 | ⚠️ 눈가림 |

**추정 의미 컬럼은 반드시 사람 검토를 거친다.** LLM 추정이 그대로 SAP에 들어가면 안 된다.

### 9.2 나머지 command

| Command | 입력 | 출력 |
|---|---|---|
| `/draft-protocol` | 의뢰 내용, 적응증, 목적 | `docs/protocol.md` 초안 |
| `/draft-sap` | Protocol, Data Dictionary | `docs/sap.md` 초안 (table shell 포함) |
| `/draft-dmp` | Protocol, Data Dictionary | `docs/dmp.md` 초안 |
| `/derive-assertions` | SAP | `docs/assertions_spec.yaml` |
| `/write-program` | SAP의 table shell, Dictionary, assertions_spec, 언어 선택 | `programs/{lang}/*.{sas,py,R}` |
| `/run-program` | 프로그램 경로, purpose | runner 실행 + 결과 요약 |
| `/review-output` | Table/Figure 경로 | 검토 의견, CSR 문구 초안 |
| `/qc-program` | 원 프로그램의 SAP 참조만 (원 코드 미제공) | Independent programming 코드 |
| `/verify-audit` | — | 감사 체인 검증 결과 |

### 9.3 `/qc-program`의 독립성

Independent Programming(double programming)의 핵심은 **독립성**이다.

- QC 프로그램 작성 시 **원 프로그램 소스를 절대 제공하지 않는다**
- 입력은 SAP의 table shell과 Data Dictionary뿐
- 같은 로컬 LLM을 쓰더라도 원 코드를 안 보면 독립성이 부분적으로 유지된다
- **제출 경로에서는 QC를 사람이 직접 작성**하는 것이 원칙. LLM QC는 보조 수단으로만 취급
- 두 결과의 대조는 LLM이 아니라 **결정론적 비교 스크립트**가 수행

manifest에 `qc_of_run_id`와 `generator`(사람/LLM)를 기록해 독립성을 추적한다.

---

## 10. Plugin 파일 구조

```
gxpllm/
├── .claude-plugin/
│   └── plugin.json
├── .mcp.json
│
├── hooks/
│   ├── hooks.json
│   ├── guard_file_access.py
│   ├── guard_bash.py
│   ├── audit_append.py
│   └── snapshot_env.py
│
├── scripts/
│   ├── run_sas.py
│   ├── run_python.py
│   ├── run_r.py
│   ├── gxpllm_assert.py
│   ├── gxpllm_assert.R
│   ├── verify_audit.py
│   └── common.py            # config 로드, 해시, manifest, 감사 append 공통 함수
│
├── macros/
│   └── gxpllm_assert.sas
│
├── mcp/
│   └── local_coder_server.py
│
├── commands/
│   ├── build-dictionary.md
│   ├── draft-protocol.md
│   ├── draft-sap.md
│   ├── draft-dmp.md
│   ├── derive-assertions.md
│   ├── write-program.md
│   ├── run-program.md
│   ├── review-output.md
│   ├── qc-program.md
│   └── verify-audit.md
│
├── agents/
│   └── qc-programmer.md
│
├── skills/
│   ├── clinical-conventions/    # ADaM 관례, TLF 형식, 분석군 정의
│   │   └── SKILL.md
│   ├── sas-programming/         # SAS 코드 규약, GXPLLM-META 형식, 매크로 사용법
│   │   └── SKILL.md
│   ├── python-programming/
│   │   └── SKILL.md
│   └── r-programming/
│       └── SKILL.md
│
└── templates/
    └── study-scaffold/          # 새 study 디렉터리 생성 템플릿
```

### 10.1 `.claude-plugin/plugin.json`

```json
{
  "name": "gxpllm",
  "version": "0.1.0",
  "description": "임상 데이터 분석용 SAS/Python/R 코드 생성 및 감사 증적 관리",
  "author": { "name": "gxpllm" }
}
```

---

## 11. 개발 순서 및 완료 상태

### 11.0 완료 상태 요약 (2026-08-03)

| 단계 | 산출물 | 상태 | 검증 |
|---|---|---|---|
| **1** | `hooks/guard_file_access.py`, `guard_bash.py`, `hooks.json`, `snapshot_env.py` | **완료** | `test_hooks.py` 329건 |
| **2** | `gxpllm/core.py`, `scripts/_common.py`, `verify_audit.py` | **완료** | `test_audit.py` 9건 + **실환경 체인 검증** (§12.4) |
| **3** | `scripts/run_sas.py` + 로그 스캔 19종 | **코드 완료 / 실행 미검증** | SAS 미설치 (§11.2) |
| **4** | `scripts/run_python.py`, `run_r.py` | **Python 실환경 검증 11/11 / R 미검증** | R 미설치 (§11.2) |
| **5** | `macros/gxpllm_assert.sas`, `gxpllm_assert.py`, `gxpllm_assert.R` | **완료** | `test_assert_api.py` — 3개 언어 API 일치 |
| **6** | `mcp/local_coder_server.py`, `/write-program` | **완료** | `test_mcp.py` + `test_llm_path.py` + **실서버 왕복 5/5** (§12.4) |
| **7** | `/build-dictionary` | **완료** | command 정의 및 구조 검증 |
| **8** | 나머지 command 9종, skill 3종 | **완료** | `run_all.py` 구조 검증 |

**추가 산출물** (계획 외, 검증 과정에서 필요성이 드러남)

| 도구 | 목적 |
|---|---|
| `scripts/init_study.py` | study 표준 구조 생성 |
| `scripts/compare_outputs.py` | Independent Programming 결정론적 대조 |
| `scripts/benchmark_codegen.py` | 언어별 코드 생성 품질 실측 (§11.1) |
| `scripts/verify_environment.py` | 실제 PC 환경 end-to-end 검증 (§12.4) |
| `tests/mock_vllm_server.py` | vLLM 없이 LLM 경로 검증 |
| `tests/test_false_positives.py` | 오탐 회귀 방지 (오탐은 보안 이슈) |
| `tests/test_assert_api.py` | 3개 언어 API 및 문서 정합성 |

### 11.2 개발 환경에서 검증 불가했던 것

| 항목 | 이유 | 대응 |
|---|---|---|
| SAS 9.4 실행 | 개발 PC 에 SAS 9.4 미설치 | `verify_environment.py --only sas` 로 실제 PC 에서 확인 |
| R 실행 | R 미설치 (`C:\Program Files\R` 디렉터리만 존재) | `verify_environment.py --only r` |
| ~~vLLM~~ | ~~endpoint 없음~~ | **2026-08-03 실서버로 검증 완료 (5/5)**. §12.4 참조 |
| ~~Python runner~~ | — | **2026-08-03 실환경 검증 완료 (11/11)**. §12.4 참조 |

SAS 와 R 은 여전히 남아 있다. 두 소프트웨어가 설치된 PC 를 확보하기 전에는
§11.1 의 언어 비교를 할 수 없다.

> **주의**: 개발 PC 에서 `D:\Software\SAS 9.1.3 Portable\sas.exe` 가 발견됐으나
> 검증에 사용하지 않았다. `info.txt` 에 따르면 토렌트로 배포된 크랙 버전이며
> 라이선스 만료일을 강제 패치한 것이다.
>
> **규제 대상 임상 분석에 미인증 소프트웨어를 쓰면 밸리데이션이 무효가 되고
> 라이선스 위반이 된다.** GxP 는 qualified software 를 요구한다.
> 정품 SAS 9.4 로만 검증하십시오.

## 11.3 개발 순서 (원안)

**hook과 로깅이 먼저다.** 경계와 증적 없이 실데이터에 쓰기 시작하면 소급이 불가능하다.

| 단계 | 산출물 | 검증 방법 |
|---|---|---|
| **1** | `hooks/guard_file_access.py`, `guard_bash.py` | 의도적으로 `data\adsl.sas7bdat` 읽기 시도 → 차단 확인. 우회 시도 10종 테스트 |
| **2** | `scripts/common.py`, `audit_append`, `verify_audit.py` | 감사 체인 생성 후 중간 항목 변조 → 탐지 확인 |
| **3** | `scripts/run_sas.py` + 로그 스캔 | 의도적으로 다대다 MERGE를 넣은 프로그램 실행 → `MERGE_REPEAT_BY` 탐지 확인 |
| **4** | `scripts/run_python.py`, `run_r.py` | 동일 분석을 3개 언어로 작성 → 결과 일치 확인 |
| **5** | `macros/gxpllm_assert.sas` + Python/R emitter | assertion 실패 시나리오 → `assertions.json` 형식 일치 확인 |
| **6** | MCP 서버 + `/write-program` | **SAS 코드 생성 품질 실측** (아래 §11.1) |
| **7** | `/build-dictionary` | Dictionary 없는 실제 의뢰 건으로 검증 |
| **8** | 나머지 command | 실제 study 1건 파일럿 (비제출 탐색 분석) |

### 11.1 착수 전 반드시 측정할 것: 언어별 코드 생성 품질

**자동화 도구가 있습니다.**

```bash
# 1. 케이스 템플릿 생성
python scripts/benchmark_codegen.py --init-cases benchmark/cases.yaml

# 2. 실제 업무 프로그램 10개로 채운 뒤 실행
python scripts/benchmark_codegen.py --cases benchmark/cases.yaml --study D:\clinical\DEMO-001
```

도구가 하는 일: MCP 로 코드 생성 → runner 로 실행 → 실패 시 최대 3회 수정 →
언어별 지표 집계 → `validation/codegen_benchmark_{날짜}.json` 저장.

**케이스에 `human_minutes`(사람이 직접 작성한 시간)를 반드시 기록하십시오.**
이 값이 없으면 가장 중요한 판단 지표를 계산할 수 없습니다.


Qwen3.6-35B-A3B는 Python 대비 **SAS에서 성능이 낮을 가능성이 높다**.
SAS 매크로, PROC 문법, ADaM 파생 관례는 학습 데이터가 훨씬 적기 때문이다.

**측정 방법**

1. 실제 업무에서 쓰던 프로그램 10개 선정 (인구통계 요약, AE 요약, 생존분석, listing 등)
2. 각각의 SAP table shell + Data Dictionary만 주고 세 언어로 코드 생성
3. 실행 후 기존 결과와 대조

**평가 지표**

| 지표 | 의미 |
|---|---|
| 무수정 실행 성공률 | 생성된 코드가 그대로 도는 비율 |
| 결과 일치율 | 기존 SAS 결과와 숫자가 일치하는 비율 |
| 평균 수정 라운드 | 통과까지 필요한 수정 횟수 |
| **검토 시간 vs 직접 작성 시간** | **이게 마이너스면 프로젝트 성립 안 함** |

**분기 판단**

- SAS 결과가 좋으면 → 계획대로 3개 언어 모두 지원
- SAS만 나쁘면 → SAS 코드 작성은 **Opus에게** 맡긴다 (코드에는 피험자 데이터가 없으므로 경계 위반이 아님). 로컬 LLM은 Python/R과 비정형 텍스트 정형화를 담당
- 전반적으로 나쁘면 → 범위를 `/build-dictionary`와 탐색적 분석으로 축소

**이 측정 결과가 §8과 §9의 설계를 바꾼다. 개발 전에 수행한다.**

#### 참고: 호출 지연 실측 (2026-08-03)

품질 측정은 아직이지만, **응답 지연**은 로컬 vLLM(Qwen3.6-35B-A3B)에서 측정했다.

| 항목 | 값 |
|---|---|
| 코드 생성 호출 1회 | 약 160초 |
| 생성 + 수정 1회 합계 | 325초 |
| 생성된 코드 길이 | 89행 |

**호출당 2~3분이 이 서버의 정상 범위다.** `GXPLLM_TIMEOUT_SEC` 기본값 300 은
단일 호출에는 충분하고, 올릴 근거는 아직 없다. 다만 `structure_text` 는
단일 호출로 300초를 넘겨 실패한 적이 있다.

이 지연은 설계 판단에 직접 영향을 준다 — 케이스 10건 × 3개 언어를 측정하면
생성만으로 **1.5시간 이상**이 걸린다. §11.1 측정 일정을 잡을 때 반영할 것.

**품질은 측정되지 않았다.** 위 수치는 1케이스 Python 단독 실행이고,
입력 데이터·`expected` 결과·실제 `human_minutes` 가 모두 없었다.
성공률 0% 는 코드 품질이 아니라 입력 데이터 부재 때문일 가능성이 높지만
**확인하지 못했다** (경계 정책상 `execution.log` 를 읽을 수 없다).
이 수치를 IQ/OQ 증적으로 쓰면 안 된다.

---

## 12. 알려진 한계 (CSV 문서에 반드시 기재)

적대적 검토(2026-08-02)에서 확인된 것과, 설계상 남는 한계를 함께 기재한다.
**이 절은 CSV 문서와 SOP 에 그대로 옮겨야 한다.** 통제의 한계를 문서화하지 않으면
"경계가 있다"는 주장 자체가 감사에서 문제가 된다.

### 12.1 경계 통제의 한계

| # | 한계 | 영향 | 보완 |
|---|---|---|---|
| 1 | **hook 이 크래시하면 경계가 열린다** | Claude Code 는 exit 2 만 차단으로 처리한다. hook 스크립트에 구문 오류가 있으면 exit 1 이 되어 도구가 그대로 실행된다 | 배포 전 `tests/run_all.py` 필수 통과. hook 본문은 최상위 `try/except` 로 감싸 예외 시에도 exit 2 |
| 2 | **hook timeout 시 동작이 불확실** | 네트워크 드라이브나 백신 지연으로 20초를 넘기면 호스트가 allow 로 처리할 수 있다 | study 를 로컬 디스크에 두고, 백신 예외 경로로 등록 |
| 3 | **정규식 기반 셸 차단은 완전하지 않다** | 난독화·변수 치환으로 우회 가능 | 인터프리터를 전면 차단해 임의 코드 실행 창을 없앴다. 그래도 완전하지 않다 |
| 4 | **프로그램 자체가 유출 경로다** | Opus 가 작성한 프로그램이 데이터를 읽어 허용 디렉터리에 쓸 수 있다 (§5.1b) | runner 가 미선언 산출물을 탐지해 실패 처리한다. 선언하면 통과하므로 **탐지이지 방지가 아니다**. 실질적 통제는 코드 검토와 SOP |
| 5 | **MCP 서버는 로컬 LLM 을 통해 데이터에 닿는다** | `structure_text` 에 데이터를 넣어 호출하면 결과가 Opus 에게 돌아온다 | Opus 는 데이터를 읽을 수 없으므로 `text` 인자를 채울 수 없다. 다만 사용자가 붙여넣으면 우회된다 |
| 6 | **사람이 우회할 수 있다** | 분석가가 화면을 보고 직접 Opus 에게 붙여넣는 것은 기술로 막을 수 없다 | SOP 교육. "Table/Figure 는 되고 Listing 은 안 된다" 한 줄 규칙 |
| 7 | **오탐이 메타 우회를 부른다** | 정당한 작업이 막히면 사용자가 plugin 을 끈다 | 2·3차 검토에서 오탐 14건을 해소했다. 새 오탐이 보고되면 **보안 이슈로 다루어 즉시 수정**하십시오 |
| 8 | **study 밖 경로는 통제하지 않는다** | `C:\temp\*.parquet` 등은 허용된다 | 임상 데이터 고유 확장자만 위치 무관 차단. 데이터를 study 밖으로 복사하는 것은 SOP 로 통제 |
| 9 | **쓰기 허용 영역이 유출면이다** | Opus 가 어떤 경로로든 값을 알게 되면 `docs/stolen.md` 에 기록할 수 있다 | 데이터 읽기 자체를 막는 것이 1차 방어. runner 가 미선언 산출물로 탐지 |
| 10 | **셸 변수 확장을 평가하지 않는다** | hook 은 확장 전 문자열만 본다 | 확장 구문(`%VAR%`, `$VAR`, `set VAR=`)을 **전면 차단**해 회피했다. 불편이 크면 SOP 로 완화 여부를 판단하십시오 |

### 12.2 감사 증적의 한계

| # | 한계 | 설명 |
|---|---|---|
| 1 | **후미 절단은 탐지되지 않는다** | 체인이 선형이므로 마지막 N 개를 지우면 남은 부분은 여전히 유효하다. **사내 공유 드라이브 백업본과 대조해야만 탐지된다** |
| 2 | **HMAC 키에 같은 사용자가 접근할 수 있다** | 키는 `~/.gxpllm/audit.key` 에 있다. study 트리 밖에 두어 위조 난이도를 높였을 뿐, 완전한 방지가 아니다. 키를 확보하면 전체 재작성이 가능하다 |
| 3 | **외부 anchor 가 없다** | WORM 저장소, 원격 타임스탬프, 코드 사이닝이 없다. 규제 수준의 보증이 필요하면 별도 도입이 필요하다 |
| 4 | **허용된 읽기는 기록되지 않는다** | 차단(`access_blocked`)과 셸 명령(`command_executed`)은 기록되지만, 정상 파일 읽기는 기록되지 않는다 |
| 5 | **감사 기록 실패는 무시된다** | 감사 기록이 차단 동작을 방해하지 않도록 예외를 삼킨다. 차단은 되지만 증적이 유실될 수 있다 |

**보완 조치 (필수)**
- `audit/` 와 `logs/` 를 백업되는 사내 공유 드라이브에 **일 1회 동기화**
- `/verify-audit` 를 **월 1회** 실행하고 결과를 날짜별로 보관 (항목 수 감소는 절단 신호)
- 제출 경로 산출물이라면 WORM 저장소 도입 검토

### 12.3 재현성의 한계

| # | 한계 | 설명 |
|---|---|---|
| 1 | **Python/R 은 SAS 보다 재현성이 낮다** | `uv.lock` / `renv.lock` 으로 완화하지만 OS·컴파일러 수준 차이는 남는다 |
| 2 | **부동소수점 일치를 보장하지 않는다** | BLAS, 병렬 연산, 라이브러리 변경으로 마지막 자리가 달라질 수 있다. 검증 tolerance 를 명시해야 한다 |
| 3 | **LLM 출력은 비결정적이다** | 같은 프롬프트가 다른 코드를 만든다. **검증 대상은 생성된 코드이지 LLM 이 아니다** |
| 4 | **SAS 반환 코드는 신뢰할 수 없다** | 논리적으로 치명적인 상황에서도 0 을 반환한다. 로그 스캔이 필수다 |

### 12.4 검증되지 않은 것 — 실제 PC 에서 확인하십시오

개발 환경에 SAS / R / DGX Spark 가 없어 다음을 검증하지 못했다.
**`scripts/verify_environment.py` 가 이것을 자동으로 확인한다.**

```bash
python scripts/verify_environment.py --study D:\clinical\DEMO-001
```

| # | 항목 | 도구가 확인하는 것 | 상태 |
|---|---|---|---|
| 1 | **SAS 9.4 runner 실동작** | 배치 실행, `-noterminal`, WORK 분리, `.lst` 생성 | 미검증 |
| 2 | **한국어 Windows SAS 로그 인코딩** | CP949 자동 감지 여부 (manifest 의 `log_encoding`) | 미검증 |
| 3 | **SAS 로그 스캔 규칙** | 의도적 다대다 MERGE 를 넣고 `MERGE_REPEAT_BY` 탐지 확인 | 미검증 |
| 4 | **SAS assertion 매크로** | `assertions.jsonl` 기록, 의도적 실패가 `FAIL` 로 남는지 | 미검증 |
| 5 | **R runner 실동작** | `Rscript --vanilla`, wrapper 주입, `sessionInfo` | 미검증 |
| 6 | **R assertion 함수** | Python/SAS 와 동일한 JSON 형식 | 미검증 |
| 7 | **vLLM 연동** | endpoint 연결, 모델 서빙 확인, MCP 경유 코드 생성 | **검증됨** (아래) |
| 8 | **3개 언어 형식 일치** | 같은 필수 키, 같은 `language` 필드, 공통 rule 이름 | 부분 (Python 만) |
| 9 | **Python runner 실동작** | run 디렉터리, manifest, assertion, 의도적 실패 탐지 | **검증됨** (아래) |
| 10 | **감사 체인 실환경** | HMAC 체인, manifest 정합성, 고아 run 탐지 | **검증됨** (아래) |

#### 실환경 검증 결과 (2026-08-03, 로컬 vLLM)

**7. vLLM 연동 — 5/5 통과.** OpenAI 호환 endpoint(Qwen3.6-35B-A3B)에
실제로 연결해 확인했다.

```
OK  endpoint 연결        모델 17개
OK  모델 서빙 중
OK  MCP 경유 코드 생성    GXPLLM-META 헤더 포함
```

MCP stdio → HTTP → 응답 파싱 → 코드 펜스 제거까지 실제 왕복이고,
로컬 LLM 이 `GXPLLM-META` 헤더 규약을 지키는 것도 확인했다.

**9. Python runner — 11/11 통과.** run 디렉터리 생성, `manifest.json`,
`language` 필드, program SHA-256, assertion 5건 기록,
`VERIFY_INTENTIONAL_FAIL` 이 `FAIL` 로 남고 그로 인해 run 이 `FAILED` 가
되는 것, 실행 로그 생성과 해시 기록까지 전부 확인했다.

**10. 감사 체인 — 통과.** benchmark 실행 3 run 이 남긴 증적을
`verify_audit.py` 로 검증했다. 항목 7건, HMAC-SHA256 서명, 체인 정상,
manifest 정합성 정상, 감사 기록 없는 run 디렉터리 없음.

**SAS 와 R 은 여전히 미검증이다.** 두 소프트웨어가 설치된 PC 가 필요하다.
§11.1 의 핵심 판단("SAS 가 Python 보다 나쁜가")은 SAS 없이는 측정 자체가
불가능하므로, 그 PC 를 확보하기 전에는 일정을 확정하지 말 것.

**7번의 코드 경로는 모의 서버로도 검증돼 있다.** `tests/test_llm_path.py` 가
MCP 도구 호출 → HTTP 요청 구성 → 응답 파싱 → 코드 펜스 제거까지 확인하므로,
endpoint 없이도 회귀를 잡을 수 있다.

```bash
# 모의 서버로 코드 경로 검증 (endpoint 불필요)
python tests/test_llm_path.py

# 실제 DGX Spark 로 연결 검증
python scripts/verify_environment.py --study <경로> --only llm
```

**의도적 실패 케이스를 포함한다.** 성공만 확인하면 탐지 로직이 죽어 있어도
통과하기 때문이다. 예를 들어 `VERIFY_INTENTIONAL_FAIL` assertion 은 반드시
`FAIL` 로 기록되어야 하고, 그로 인해 run 이 `FAILED` 가 되어야 한다.

**수동 확인 1건**: DGX Spark 에서 `vllm serve ... --disable-log-requests` 가
적용됐는지. 이 옵션이 없으면 코드 작성 프롬프트에 담긴 데이터 구조가
서버 로그에 평문으로 쌓인다.

결과는 `validation/environment_verification_{타임스탬프}.json` 에 저장되며,
**CSV 문서(IQ/OQ)에 첨부해야 한다.**

### 12.4b 아직 측정되지 않은 것

**로컬 LLM 의 SAS 코드 생성 품질** — §11.1 의 측정 전에는 어떤 일정도 확정하지 말 것.
`scripts/benchmark_codegen.py` 로 자동화되어 있다.

### 12.5 이 시스템이 하지 않는 것

- **악의적 내부자를 막지 못한다.** 목적은 실수 방지와 감사 증적 확보다.
- **PHI 유출을 원천 차단하지 못한다.** 접근 통제를 강제할 뿐이다.
- **Part 11 준수를 자동으로 달성하지 못한다.** 증적을 자동 생산할 뿐이며,
  intended use 정의, 위험평가, IQ/OQ/PQ, SOP, 교육, 변경관리는 별도로 필요하다.
- **Independent Programming 요건을 LLM 만으로 충족하지 못한다.** 제출 경로에서는
  QC 를 사람이 직접 작성하는 것이 원칙이다.

---

## 13. 용어

| 약어 | 원어 | 국문 |
|---|---|---|
| SAP | Statistical Analysis Plan | 통계분석계획서 |
| DMP | Data Management Plan | 자료관리계획서 |
| CSR | Clinical Study Report | 임상시험결과보고서 |
| TLF | Tables, Listings, Figures | 표·목록·그림 |
| TEAE | Treatment-Emergent Adverse Event | 치료 중 발생 이상반응 |
| SDTM | Study Data Tabulation Model | CDISC 원자료 표준 |
| ADaM | Analysis Data Model | CDISC 분석용 데이터 표준 |
| ADSL | Subject-Level Analysis Dataset | 피험자 단위 분석 데이터셋 |
| FAS | Full Analysis Set | 전체 분석군 |
| PPS | Per-Protocol Set | 계획서 순응 분석군 |
| eTMF | electronic Trial Master File | 전자 시험 마스터 파일 |
