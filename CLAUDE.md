# CLAUDE.md — gxpllm

이 저장소는 **임상 데이터 분석용 Claude Code plugin** 입니다.

## 이 저장소에서 작업할 때

`AGENTS.md` 의 규칙을 그대로 따르십시오. 특히 다음 셋은 협상 대상이 아닙니다.

1. **경계를 약화시키지 마십시오.** hook 이 막는 것을 우회하기 쉽게 고치지 마십시오.
   각 차단 규칙은 적대적 검토에서 실제로 뚫린 뒤에 추가된 것입니다.
   `docs/development.md` 의 개정 이력에 무엇이 왜 막혔는지 남아 있습니다.

2. **정책은 `gxpllm/core.py` 에만 둡니다.** `.gxpllm/config.json` 으로
   `allowed_dirs` 를 넓힐 수 있게 만들면 정책 루트가 무너집니다.

3. **오탐을 보안 이슈로 다루십시오.** 정당한 작업이 막히면 사용자가 plugin 을 끄고,
   그것이 가장 확실한 경계 붕괴입니다. 새 오탐은 `tests/test_false_positives.py` 에
   케이스를 추가하고 즉시 고치십시오.

## 변경 후 반드시

```bash
python tests/run_all.py
```

SAS, R, DGX Spark 없이도 전부 통과해야 합니다.
경계 관련 코드를 고쳤다면 `tests/test_hooks.py` 의 건수가 줄지 않았는지 확인하십시오.

## 이 plugin 이 하지 않는 것

- LLM 이 숫자를 만들지 않습니다. 계산은 SAS/Python/R 만 합니다.
- 적대적 내부자를 막지 못합니다. 목적은 실수 방지와 감사 증적 확보입니다.
- Part 11 준수를 자동 달성하지 않습니다. 증적을 자동 생산할 뿐입니다.

## 아직 검증되지 않은 것

`docs/development.md` §12.4 를 보십시오. SAS 9.4 / R 실행은 실제 PC 에서
`python scripts/verify_environment.py --study <경로>` 로 확인해야 합니다.
