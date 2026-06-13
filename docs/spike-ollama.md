# Spike: Running Maestro Reconcilers via `ollama launch claude`

**Date:** 2026-06-13  
**Ticket:** M-4  
**Verdict:** Go — the plumbing works; quality risk is contained to the `implementing` phase.

---

## What `ollama launch claude` Actually Does

`ollama launch claude` is a first-class integration in ollama ≥0.30. When invoked with
`--yes --model <local-model>` it launches the `claude` CLI with these env vars:

```
ANTHROPIC_BASE_URL=http://127.0.0.1:11434
ANTHROPIC_AUTH_TOKEN=ollama
ANTHROPIC_DEFAULT_HAIKU_MODEL=<local-model>
ANTHROPIC_DEFAULT_SONNET_MODEL=<local-model>
ANTHROPIC_DEFAULT_OPUS_MODEL=<local-model>
```

No `ANTHROPIC_API_KEY` is set. The auth header sent to ollama is `x-api-key: ollama` or
`Authorization: Bearer ollama`, which ollama accepts unconditionally.

Critically, ollama 0.30.8 serves **the full Anthropic Messages API** at `/v1/messages`
(not just the OpenAI-compat `/v1/chat/completions`), including `tool_use` content blocks
and `stop_reason: "tool_use"`.

---

## Confirmed Working

All tests run with `ANTHROPIC_BASE_URL=http://127.0.0.1:11434` + `qwen3:8b` locally.

| Capability | Result | Notes |
|---|---|---|
| `claude -p <prompt>` headless | ✓ | Verified output matches |
| `--permission-mode acceptEdits` | ✓ | Framework-level; model-agnostic |
| `--permission-mode auto` | ✓ | Same |
| `--model <local>` flag | ✓ | Maps to local model ID |
| `-n <session-name>` flag | ✓ | Cosmetic; claims still use PIDs |
| Bash tool | ✓ | Subshell executes correctly |
| `maestro` CLI calls | ✓ | Ran `maestro snapshot M-4` inside session |
| Agent tool (subagent spawn) | ✓ | Child `claude -p` inherits `ANTHROPIC_BASE_URL`; also hits local model |
| tool_use response format | ✓ | ollama returns Anthropic-format `tool_use` blocks |
| `ANTHROPIC_AUTH_TOKEN` acceptance | ✓ | claude CLI accepts it as bearer token |

---

## Capability Gaps

### 1. Model quality for `implementing` phase
The `implementing` phase requires writing correct Python code, fixing failing tests in a
loop, and reasoning about multi-file codebases. `qwen3:8b` handles simple tasks but is
significantly less reliable than `claude-sonnet` for multi-step agentic coding.
`deepseek-r1:70b-llama-distill-q8_0` (already in ollama) is materially stronger for code.

**Mitigation:** Use deepseek-r1:70b for all phases. Accept lower success rate on
`implementing` relative to claude-sonnet. Suitable for low/medium complexity tickets.

### 2. Skills / `/maestro-reconcile` loading
Skills are resolved as prompt files loaded by the claude CLI. The skill system is
model-agnostic — it's loaded before the first API call. No gap here as long as `--bare`
is not passed.

### 3. No streaming / session persistence differences
`claude -p` with a local model runs identically to cloud — no differences in headless
operation. Session persistence (`~/.claude/projects/`) works the same way.

### 4. Token limits
Local models typically have shorter effective context windows (~8k–32k output tokens vs.
200k for claude-sonnet). Very long reconciler prompts with large specs could truncate.

---

## sessions.py Provider Seam (Sketch)

Current `ClaudeCliSessions.spawn()` hardcodes the subprocess env as `os.environ` with
`MAESTRO_HOME` injected. To support ollama, add a `provider_env` field:

```python
@dataclass
class ProviderConfig:
    """Model provider settings passed to the spawned claude process."""
    model: str = "sonnet"
    extra_env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def anthropic(cls, model: str = "sonnet") -> "ProviderConfig":
        return cls(model=model)

    @classmethod
    def ollama(cls, local_model: str, base_url: str = "http://127.0.0.1:11434") -> "ProviderConfig":
        return cls(
            model=local_model,
            extra_env={
                "ANTHROPIC_BASE_URL": base_url,
                "ANTHROPIC_AUTH_TOKEN": "ollama",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": local_model,
                "ANTHROPIC_DEFAULT_SONNET_MODEL": local_model,
                "ANTHROPIC_DEFAULT_OPUS_MODEL": local_model,
            },
        )
```

Then in `ClaudeCliSessions.__init__`, replace `model: str` with
`provider: ProviderConfig`, and in `spawn()`:

```python
env = {**os.environ, "MAESTRO_HOME": str(self.home), **self.provider.extra_env}
cmd = ["claude", "-p", prompt, "--model", self.provider.model, ...]
```

The `fleet up` command would accept `--provider ollama --ollama-model deepseek-r1:70b-...`
or read from `~/.maestro/config.json`.

---

## Go/No-Go Recommendation

**Go.** The full maestro reconciler loop works locally:

- `triaging`, `awaiting-human`, `awaiting-ci`, `in-review`: pure decision logic +
  CLI calls — local models handle these reliably.
- `implementing`: works for low-complexity tickets; deepseek-r1:70b is the right
  local model for this.

The `sessions.py` change is small: one new `ProviderConfig` dataclass + 3-line diff in
`spawn()`. No changes to the reconciler protocol, events format, or maestro CLI.

**Constraint:** Requires ollama ≥0.30 running locally (`ollama serve`). The deepseek-r1:70b
model (74 GB q8) is already present on this machine.
