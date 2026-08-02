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

## 새 차단 규칙을 추가할 때

1. `tests/test_hooks.py` 에 **차단 케이스**를 추가합니다.
2. `tests/test_hooks.py` 에 그 규칙이 막으면 안 되는 **허용 케이스**도 추가합니다.
3. `tests/test_false_positives.py` 의 일상 명령이 여전히 통과하는지 확인합니다.
4. `docs/development.md` §4 에 규칙과 그 근거를 기록합니다.

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
