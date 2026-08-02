# CONTRIBUTING — gxpllm

## 이 저장소의 성격

임상시험 데이터를 다루는 규제 환경용 도구입니다.
일반적인 오픈소스 기여 규칙에 더해 몇 가지가 추가됩니다.

## 절대 하지 말 것

1. **임상 데이터를 커밋하지 마십시오.** `.gitignore` 가 막고 있지만
   `git add -f` 로 우회할 수 있습니다. 합성 데이터만 쓰십시오.
2. **경계를 약화시키지 마십시오.** hook 의 차단 규칙은 전부 적대적 검토에서
   실제로 뚫린 뒤 추가된 것입니다. `docs/development.md` 개정 이력에
   무엇이 왜 막혔는지 남아 있습니다. 되돌리면 이미 확인된 우회가 다시 열립니다.
3. **정책을 설정으로 옮기지 마십시오.** `allowed_dirs` 등은 `gxpllm/core.py` 에만
   존재합니다. `.gxpllm/config.json` 은 모델이 쓸 수 있는 영역이라 신뢰할 수 없습니다.

## 변경 전

```bash
python tests/run_all.py
```

기준선을 확인하십시오. SAS, R, DGX Spark 없이 전부 통과해야 합니다.

## 변경 후

```bash
python tests/run_all.py
```

**경계 관련 코드를 고쳤다면 `tests/test_hooks.py` 의 통과 건수가 줄지 않았는지
확인하십시오.** 건수가 줄었다면 케이스를 지운 것입니다.

## plugin 을 로컬에서 돌려볼 때

**clone 한 디렉터리를 그냥 열면 MCP 서버가 뜨지 않습니다.**

`.mcp.json` 의 `${CLAUDE_PLUGIN_ROOT}` 는 **설치된 plugin 에만** 주입됩니다.
저장소를 clone 해서 그 디렉터리에서 Claude Code 를 열면 `.mcp.json` 이
project-scoped 설정으로 읽히고, 변수가 빈 채로 남아 경로가 깨집니다.
서버 프로세스가 즉시 죽고 `Failed to reconnect to local-coder: -32000` 만 보입니다.
승인 문제로 보이지만 승인과 무관합니다.

로컬 clone 을 marketplace 로 등록해 설치하십시오.

```
/plugin marketplace add <clone 경로>
/plugin install gxpllm@gxpllm
/reload-plugins
```

`/mcp` 에 `plugin_gxpllm_local-coder` 로 뜨면 정상입니다.

**설치본은 커밋 시점 스냅샷입니다.** `~/.claude/plugins/cache/` 로 복사되며
작업 디렉터리를 참조하지 않습니다. 수정이 반영되지 않으면 대개 커밋을 안 한 것입니다.

```
수정 → 커밋 → /plugin update gxpllm@gxpllm → /reload-plugins
```

같은 디렉터리에서 계속 작업하면 project `.mcp.json` 의 `local-coder` 가
중복으로 등록되어 계속 실패합니다. `.claude/settings.local.json` 에서 끄십시오.
이 파일은 커밋되지 않습니다.

```json
{ "disabledMcpjsonServers": ["local-coder"] }
```

**설치하면 hook 이 이 저장소에서도 돕니다.** `.gxpllm/config.json` 이 없어
`study_root` 가 `None` 이므로 대부분 통과하고, `python tests/run_all.py` 와
`python scripts/verify_environment.py` 는 `DEV_COMMAND_PATTERN` 으로 허용됩니다
(`hooks/guard_bash.py`).

다만 이 예외는 **구간 선두부터 끝까지 정확히 일치**해야 하고 장옵션만 받습니다.
리디렉션이나 파이프를 붙이면 막힙니다.

```bash
python tests/run_all.py              # OK
python tests/run_all.py 2>&1 | tail  # 차단됨 — 의도한 제약입니다
```

앵커를 풀면 `python -c "..." # scripts/run_sas.py` 류의 우회가 다시 열립니다
(`docs/development.md` 0.8 → 0.9 참조). 그냥 붙여서 실행하십시오.

개발용 명령이 막히면 **우회하지 말고** 오탐으로 처리하십시오
(아래 "오탐 보고를 받으면").

## 새 차단 규칙을 추가할 때

1. `tests/test_hooks.py` 에 **차단 케이스**를 추가합니다.
2. `tests/test_hooks.py` 에 그 규칙이 막으면 안 되는 **허용 케이스**도 추가합니다.
3. `tests/test_false_positives.py` 의 일상 명령이 여전히 통과하는지 확인합니다.
4. `docs/development.md` §4 에 규칙과 그 근거를 기록합니다.

## 허용 규칙을 추가할 때

차단보다 위험합니다. 둘을 지키십시오.

**1. 앵커를 거십시오.** 허용 판정은 `search()` 가 아니라 구간 선두에서
`match()` 로 합니다. 앵커가 없으면 주석이나 인자에 문자열을 끼워 넣는 것만으로
면제받을 수 있습니다. `RUNNER_ALLOW_PATTERN` 이 실제로 이 상태였습니다.

**2. 면제 범위를 최소로 좁히십시오.** 구간 전체를 모든 검사에서 빼는 대신,
필요한 검사 하나에서만 빼십시오. `DEV_COMMAND_PATTERN` 은 `check_direct_exec`
안에서만 면제하므로 데이터 경로 검사는 그대로 적용됩니다.

## hook 과 core 의 메시지는 영어로 씁니다

대상은 `hooks/*.py` 와 `gxpllm/core.py` 입니다. 차단 사유, 감사 체인 검증 결과,
설정 오류 메시지가 전부 사용자 콘솔로 나갑니다. Windows 한국어 환경의 기본
콘솔은 cp949 라 한글이 깨집니다.

**차단 이유를 읽을 수 없으면 오탐인지 정당한 차단인지 판단할 수 없고,
사용자는 그냥 plugin 을 끕니다.** 차단 자체보다 나쁩니다.

주석과 docstring 은 한글 그대로 둡니다. 사용자에게 나가는 문자열만 영어입니다.
`scripts/*.py` (runner) 의 진행 출력은 아직 한글입니다.

## 오탐 보고를 받으면

**보안 이슈로 다루십시오.** 정당한 작업이 막히면 사용자가 plugin 을 끄고,
그것이 가장 확실한 경계 붕괴입니다.

1. `tests/test_false_positives.py` 에 케이스를 추가합니다.
2. 차단이 의도된 것인지 판단합니다 (`python -c` 는 의도된 차단입니다).
3. 의도되지 않았으면 즉시 고칩니다.

## assertion 을 추가할 때

세 언어에 **같은 이름과 같은 파라미터로** 추가해야 합니다.

- `macros/gxpllm_assert.sas`
- `scripts/gxpllm_assert.py`
- `scripts/gxpllm_assert.R`

`tests/test_assert_api.py` 가 이름·파라미터 일치와 문서 정합성을 검사합니다.
`skills/*.md` 와 `mcp/local_coder_server.py` 의 시그니처도 함께 고치십시오.

## 커밋 메시지

무엇을 왜 바꿨는지 쓰십시오. 경계 관련 변경은 **어떤 우회를 막는지** 명시하십시오.

```
guard_bash: block git range notation (HEAD~1..HEAD)

Range notation is a single token, so the ref-count check missed it.
Found in adversarial review round 6.
```

## 릴리스 전

1. `python tests/run_all.py` 통과
2. `python scripts/verify_environment.py --study <경로>` — 실제 SAS/R/vLLM
3. `CHANGELOG.md` 갱신
4. `docs/development.md` §12 의 알려진 한계가 최신인지 확인
