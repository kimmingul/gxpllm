# Changelog

이 프로젝트는 [Semantic Versioning](https://semver.org/lang/ko/) 을 따릅니다.

## [Unreleased]

### 수정 — 조용한 실패 두 건

실제 로컬 LLM(Qwen3.6-35B-A3B, lemonade-server)에 붙여 처음 실측한 결과
발견했습니다. 두 결함은 하나의 시나리오로 이어집니다 —
**잘린 프로그램이 저장되고, 검증 없이 PASSED 로 보고된다.**

- `call_llm` 이 `finish_reason` 을 확인하지 않아 잘린 응답을 정상 반환했습니다.
  추론 모델은 추론과 본문이 **같은** `max_tokens` 예산을 나눠 씁니다.
  실측: 인구통계 요약표 요청 하나에 reasoning 26,023자 / content 859자,
  `completion_tokens` 8,192 로 예산을 전부 소진하고 `finish_reason=length`.
  이제 `finish_reason` 을 **allowlist(`stop`)** 로 판정하고, 본문이 비었으면
  (`None` / 공백) 거부합니다. `length` 만 막으면 `content_filter`, 필드 누락,
  프록시 고유 값이 통과합니다.
- `decide_result` 가 실패 수만 보고 총 건수를 보지 않아 **assertion 0건을
  PASSED** 로 판정했습니다. assertion 이 통째로 빠진 프로그램 —
  잘린 응답으로 저장된 것이든 assertion 호출을 빠뜨린 것이든 — 이
  경로로 빠져나갔습니다. 이제 0건은 실패입니다.
  `qc` / `submission_candidate` 는 예외를 허용하지 않고,
  `exploratory` 는 `--allow-no-assertions` 로만 열 수 있으며 그 사실이
  manifest(`assertions.no_assertions_allowed`)와 감사 로그에 남습니다.

### 변경 — 설정을 환경변수로 분리

`.mcp.json` 이 `${VAR:-기본값}` 확장을 쓰도록 바꿨습니다. 저장소에는 안전한
기본값만 커밋되고, 각자 자기 서버는 환경변수로 지정합니다.
**`.mcp.json` 을 직접 고칠 필요가 없습니다** — 고치면 `git pull` 마다
충돌하고 사내 주소를 실수로 커밋하게 됩니다.

| 환경변수 | 기본값 |
|---|---|
| `GXPLLM_ENDPOINT` | `http://dgx-spark.internal:8001/v1` |
| `GXPLLM_MODEL` | `Qwen3.6-35B-A3B` |
| `GXPLLM_API_KEY` | (기본값 없음 — 비밀값이므로) |
| `GXPLLM_MAX_TOKENS` | `32768` |
| `GXPLLM_ENCODING` | `utf-8` |

- 서버가 인증을 요구하면 `GXPLLM_API_KEY` 로 `Authorization: Bearer` 를
  보냅니다. **키는 오류 메시지에 넣지 않습니다** — MCP 오류는 오케스트레이터
  대화 기록에 그대로 남기 때문에, 설정 여부만 알립니다.
- 확장되지 않은 `${VAR}` 자리표시자를 빈 값으로 처리합니다.
  Claude Code 는 변수가 없고 기본값도 없으면 `${VAR}` 를 **문자열 그대로**
  전달하므로, 그대로 쓰면 `Authorization: Bearer ${GXPLLM_API_KEY}` 가 나가
  인증이 조용히 실패합니다.

### 추가

- `GXPLLM_MAX_TOKENS` 환경변수. 기본값을 8,192 → 32,768 로 올렸습니다.
  **상향은 완화책이지 안전장치가 아닙니다** — 추론이 예산을 다시 잠식할 수
  있으므로 잘림 자체는 `finish_reason` 검사로 막습니다.
  자동으로 토큰을 늘려 재시도하지 마십시오.
- `structure_text` 가 스키마를 받은 경우 응답의 JSON 완결성을 검사합니다.
  깨진 JSON 을 넘기면 downstream 이 "추출된 항목 없음" 으로 조용히 흡수합니다.
- `tests/test_live_llm.py` — 실제 LLM 서버로 MCP 경로를 왕복시키는 검증.
  서버가 있는 PC 에서 사람이 실행합니다. `run_all.py` 에는 넣지 않습니다.
- 오프라인 회귀 테스트 — `mock_vllm_server.py` 에 `truncated`,
  `empty_content`, `null_content`, `broken_json` 시나리오를 추가하고
  `test_llm_path.py` 가 거부를 확인합니다. `test_runners.py` 는 0건 정책과
  `--allow-no-assertions` 배선을 end-to-end 로 검증합니다.

경계 설계는 grok-4.5 와 codex(GPT-5.6) 의 교차 검토를 거쳤습니다.

## [0.1.0] — 2026-08-02

첫 릴리스. **아직 밸리데이션되지 않았습니다** — 규제 제출 경로에 사용하기 전에
`docs/development.md` §12.4 의 환경 검증과 IQ/OQ/PQ 를 완료하십시오.

### 추가

**경계 강제 (hooks)**
- `guard_file_access.py` — study 루트 안 기본 거부(default-deny) + 허용 목록.
  matcher `*` 로 모든 도구를 덮고, `tool_input` 전체를 재귀 탐색해 경로를 찾는다.
  읽기와 쓰기 허용 목록이 다르다 (`.gxpllm/` `audit/` `logs/` `output/` 은 읽기 전용).
- `guard_bash.py` — 인터프리터 직접 실행, 셸 래퍼, 재귀 탐색, 변수 확장,
  git 내용 출력 하위명령 차단. 명령을 연결 연산자로 나눠 각 구간을 독립 판정.
- `audit_append.py` — 실행된 셸 명령을 감사 로그에 기록.
- `snapshot_env.py` — 세션 시작 시 SAS/Python/R 버전과 lock 해시 캡처.

**감사 증적**
- HMAC-SHA256 해시 체인 (`audit/audit.jsonl`). 키는 study 트리 밖
  (`~/.gxpllm/audit.key`) 에 두어 study 만 조작해서는 위조가 성립하지 않는다.
- 알고리즘 다운그레이드 거부 — 키가 있으면 모든 항목이 HMAC 이어야 한다.
- `scripts/verify_audit.py` — 체인 무결성, manifest 내용 해시, 고아 run 탐지.

**runner**
- `run_sas.py` — SAS 9.4 배치 실행, CP949 로그 자동 감지, 로그 스캔 19종
  (다대다 MERGE, 타입 변환, remerge 등 종료 코드 0 에서도 발생하는 치명적 NOTE).
- `run_python.py` — `uv run --locked` 환경 잠금, SAS `.log` 대응 로그 생성,
  traceback 정제 (데이터 값이 오케스트레이터로 넘어가지 않게).
- `run_r.py` — `Rscript --vanilla`, `options(warn=)` 주입, `sessionInfo()` 기록.
- 미선언 산출물 탐지 — 내용 해시 비교라 `os.utime` 복원으로 피할 수 없다.

**assertion**
- SAS 매크로 / Python 모듈 / R 함수 3종. 동일한 이름과 파라미터, 동일한 JSON 형식.
- 데이터 무결성 (행 수, join 손실, key 유일성, 도메인, 결측률, 날짜 순서)
- 임상 정합성 (분석군 flag, 분모, reconciliation, 코딩 사전 버전)

**MCP**
- `local_coder_server.py` — DGX Spark 의 vLLM 에 붙어 코드를 생성한다.
  파일 읽기 / 셸 실행 / 임의 SQL / URL fetch 는 노출하지 않는다.

**command 10종**
`/build-dictionary` `/draft-protocol` `/draft-sap` `/draft-dmp`
`/derive-assertions` `/write-program` `/run-program` `/qc-program`
`/review-output` `/verify-audit`

**skill 3종**
`clinical-conventions` `sas-programming` `python-r-programming`

**도구**
- `scripts/init_study.py` — study 표준 구조 생성
- `scripts/compare_outputs.py` — Independent Programming 결정론적 대조
- `scripts/benchmark_codegen.py` — 언어별 코드 생성 품질 실측
- `scripts/verify_environment.py` — 실제 PC 에서 SAS/R/vLLM end-to-end 검증

**검증 스위트 9종** — 경계 327건, 감사 체인 9건, 오탐, assertion API,
runner, MCP, LLM 경로(모의 서버), 실측 도구, 구조.

### 알려진 한계

`docs/development.md` §12 참조. **CSV 문서와 SOP 에 그대로 옮겨야 합니다.**

- hook 이 크래시하면 경계가 열린다 (exit 2 만 차단으로 처리됨)
- 해시 체인의 후미 절단은 탐지되지 않는다 — 백업본 대조 필요
- `scripts/` → runner → `output/tables/` 덤프는 탐지만 가능하고 방지는 불가
- 사람이 화면을 보고 붙여넣는 것은 기술로 막을 수 없다

### 미검증

- SAS 9.4 실행 (개발 환경에 미설치)
- R 실행 (미설치)
- 실제 DGX Spark vLLM (모의 서버로 코드 경로만 검증)
- 로컬 LLM 의 SAS 코드 생성 품질 — `scripts/benchmark_codegen.py` 로 실측 필요
