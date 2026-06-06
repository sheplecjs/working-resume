# Working Resume — Project Guide

## What this is

A personal CLI tool for tailoring job-application documents (resumes, cover letters, interview prep) to specific job descriptions using Claude. The current entrypoint is `tailor.py`, a phase-gated script. The project is being refactored into a reusable framework built around a `Phase` protocol and `FlowRunner`.

## Running the project

```
task        # start the CLI (runs tailor.py via uv)
task check  # lint, type-check, YAML lint, dep audit, secrets scan
```

Requires: a `.env` file with `anthropic_api_key="..."`, a `content_bank.yaml`, and a base `resume.yaml`.

## File layout

| Path | Purpose |
|---|---|
| `tailor.py` | Current monolithic CLI — phases exist as functions, not yet as Phase objects |
| `bank.py` | Pydantic models for `ContentBank`, `HighlightEntry`, `ProfileEntry` |
| `settings.py` | Pydantic-settings `Settings` — loads `ANTHROPIC_API_KEY` from `.env` |
| `content_bank.yaml` | Source of truth for profile variants and highlight pool |
| `resume.yaml` | Base resume in rendercv YAML format |
| `jds/` | Job descriptions (PDF or Markdown) |
| `tailored/` | Output YAMLs and rendercv renders |
| `logs/` | Per-run logs; one file per session named `YYYYMMDD_HHMMSS_<jd_stem>.log` |
| `classic/`, `markdown/` | rendercv Typst/Markdown templates |

## Model

The project uses `claude-sonnet-4-6`. Do not change the model without checking current pricing and capability tradeoffs.

---

## Planned architecture — Phase protocol and FlowRunner

The refactor goal is a framework where flows are composed from discrete, testable `Phase` objects rather than hardcoded function calls. Everything below describes the *target* design; `tailor.py` is the *current* state.

### Phase protocol

```python
class Phase(Protocol):
    name: str          # identity; used as log label and in display
    description: str   # human-readable; for CLI introspection and flow summaries
    skippable: bool    # runner may bypass this phase in non-interactive/automated modes
    idempotent: bool   # runner may safely retry this phase on failure

    def run(self, ctx: FlowContext) -> FlowContext: ...
```

**`name`** drives log section headers (currently the `phase` parameter in `parse_json`) and display in the CLI. It must be unique within a flow.

**`skippable`** indicates the runner is *permitted* to bypass the phase. It does not mean the phase will be skipped — that decision is made by the runner based on mode (interactive vs. automated). A clarification phase is not skippable; a revision phase is.

**`idempotent`** indicates the phase is safe to re-run with the same input. The runner will only apply a retry policy to a phase when this is `True`. A phase is idempotent when: (a) its only side effect is returning an updated `FlowContext`, or (b) any external writes it makes are overwrite-safe. Interactive phases (user Q&A) are never idempotent.

### FlowContext

`FlowContext` is the state object that flows through phases. The invariant is:

> **`run()` must return a new or updated `FlowContext`. It must never mutate the context it receives.**

The runner snapshots `ctx` before calling `run()`. If the phase fails and a retry policy applies, the runner re-calls `run()` with the original snapshot. Mutation in place would corrupt that snapshot and make retries unsafe.

### FlowEntry and flow composition

```python
@dataclass
class RetryPolicy:
    max_attempts: int = 3
    retryable_on: tuple[type[Exception], ...] = (json.JSONDecodeError,)
    backoff_seconds: float = 1.0

@dataclass
class FlowEntry:
    phase: Phase
    condition: Callable[[FlowContext], bool] | None = None
    retry_policy: RetryPolicy | None = None
```

**Branching logic belongs in `FlowEntry.condition`, not on the `Phase`.** Encoding branching inside a phase couples it to `FlowContext`'s field structure and prevents reuse across flows where that structure differs. A condition callable receives the context at runtime and returns whether the phase should run. The runner evaluates it; the phase knows nothing about it.

**Retry policy belongs in `FlowEntry.retry_policy`, not on the `Phase`.** The same phase may be retried aggressively in a production flow and not at all in a fast debug run. The runner checks `phase.idempotent` before applying any retry policy — a non-idempotent phase with a retry policy configured is a misconfiguration; the runner ignores the policy rather than retrying.

### Runner responsibilities

The `FlowRunner` is the only place that:
- snapshots `FlowContext` before each phase
- evaluates `FlowEntry.condition`
- calls `phase.run(ctx)`
- wraps each call with entry/exit/duration logging keyed to `phase.name`
- applies `retry_policy` on eligible exceptions (idempotent phases only)
- respects `phase.skippable` in non-interactive modes

Phases must not log, retry, or branch themselves. Those concerns belong to the runner.

### Phase I/O typing

Phase inputs and outputs will be typed as Pydantic models (`GapAnalysis`, `TailoringResult`, `RecruiterReview`). `FlowContext` holds typed fields, not raw dicts. This is a prerequisite for meaningful unit tests, where a phase can be exercised in isolation by constructing a fixture `FlowContext` with known values.

### Panel phases

A `Panel` is a `Phase` that fans out to N sub-phases in parallel, each with its own system prompt and persona, then aggregates their outputs through a synthesis step. This is the mechanism for multi-reviewer flows (e.g. a recruiter, a hiring manager, and a technical peer each reviewing a resume and cover letter simultaneously). A `Panel` is idempotent if all its sub-phases are idempotent.

### Idempotency reference

| Phase | `idempotent` | Reason |
|---|---|---|
| JD extraction | `True` | LLM call → context write only |
| Gap analysis | `True` | LLM call → context write only |
| Tailoring | `True` | LLM call → context write only |
| Apply + render | `True` | File writes are overwrite-safe |
| Recruiter review | `True` | LLM call → context write only |
| Revision | `True` | LLM call → context write only |
| Clarification | `False` | Re-prompts the user interactively |

---

## Logging

Each run writes a single log file to `logs/`. The runner writes a section for every phase using `phase.name` as the header. LLM responses are logged before JSON parsing so that parse failures are diagnosable from the raw response. Format: `====` separator, `[phase.name.event]` label, raw content.

Do not add phase-level logging inside `Phase.run()` implementations. Logging is the runner's job.

---

## Testing intent

Tests will exercise phases in isolation: construct a `FlowContext` fixture, stub the Anthropic client to return a canned string, assert the returned `FlowContext` matches the expected Pydantic model. Integration tests run a full `FlowRunner` with a mock client. The immutability invariant on `FlowContext` is the key property to verify in retry-path tests.

---

## Constraints and conventions

- **No comments explaining what code does.** Only add a comment when the *why* is non-obvious (hidden constraint, invariant, workaround).
- **No error handling for scenarios that can't happen.** Trust Pydantic validation at system boundaries; do not defensively guard internal code.
- **Pydantic for all external data.** `ContentBank`, `Settings`, and all LLM response models use Pydantic. Raw dicts do not cross module boundaries.
- **`uv` for all Python tooling.** Do not use `pip` or `python` directly; always `uv run` or `uv add`.
- **Type-check with `ty`, lint with `ruff`.** Run `task check` before considering any change complete.
