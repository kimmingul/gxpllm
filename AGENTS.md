# AGENTS.md — gxpllm

## Purpose

Clinical trial data analysis with an enforced data boundary.
A local LLM writes SAS/Python/R; runners execute them and leave an audit trail;
the orchestrating model (Claude) never sees patient data.

See `docs/development.md` for the full design.

## Rules

1. **The orchestrating model must never read patient data.** The boundary is
   enforced by `hooks/guard_file_access.py` and `hooks/guard_bash.py`, not by
   prompts. Never weaken a hook to make a task easier.
2. **The policy lives in `gxpllm/core.py` only.** `.gxpllm/config.json` cannot
   widen `allowed_dirs`, `allowed_log_files`, or `blocked_extensions` — the
   study config is writable by the model, so it is not trusted.
3. **Analysis programs run through `scripts/run_*.py`.** Direct interpreter
   invocation is blocked so that every run produces a manifest and an audit
   entry. Add new runners to `RUNNER_ALLOW_PATTERN` in `hooks/guard_bash.py`.
4. **LLMs never produce numbers.** Calculation belongs to SAS/Python/R.
   The LLM writes code and prose; assertions verify the code.
5. **False positives are security issues.** If legitimate work is blocked, the
   user disables the plugin — the most reliable boundary bypass there is.
   Add the case to `tests/test_false_positives.py` and fix it.
6. **`python tests/run_all.py` must pass without SAS, R, or a live LLM.**

## Structure

```
gxpllm/       policy + audit core (standard library only)
hooks/        PreToolUse / PostToolUse / SessionStart enforcement
scripts/      runners, audit verification, study init, benchmarks
macros/       SAS assertion macros
mcp/          local LLM code-generation server
commands/     slash commands
skills/       clinical conventions, SAS, Python/R
tests/        verification suites (must all pass before release)
docs/         development document
```

## Verify

```bash
python tests/run_all.py                     # everything (no SAS/R/LLM needed)
python tests/test_hooks.py                  # boundary only
python tests/test_false_positives.py        # legitimate work must pass
python scripts/verify_environment.py --study <path>   # real SAS/R/vLLM
```

## Known limits

`docs/development.md` §12. These must be copied into the CSV documentation.
The short version: this prevents mistakes and produces evidence.
It does not stop a determined insider or a prompt-injected model.
