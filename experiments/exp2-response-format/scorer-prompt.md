# Scorer prompt: exp2-response-format

exp2 uses **rule-based scoring only** (`recipe/scorer.py`, `compute_exp2_checks`); no
LLM judge is involved, so there is no judge prompt. This file records why, and the
prompt that would be used if a judge adjudication were ever needed.

## Why rule-based

Every exp2 prompt's expected call is a deterministic function of the prompt and the
arm's schema: one intent expression on one surface plus pre-registered absences. There
is no free-text judgment to make (unlike exp1's P4 caveat-detection dimension, which is
absent here). A judge would add variance without adding information.

## Judge adjudication prompt (unused; kept for reference)

You are given a prompt shown to a coding agent, the tool schema the agent could see,
and the tool call the agent produced. Score ONLY parameter-choice intent:

1. Did the agent call the prompt's target tool first?
2. Express the intent on exactly one surface:
   - baseline surface: the named parameters (`summary`, `fields`, `mode`, `impl_only`)
   - collapsed surface: `response_format` (string value or `{"fields": [...]}` payload)
3. Setting both surfaces at once is incorrect (cross-surface leakage).
4. Honor pre-registered absences in the rubric (e.g. import_lookup forbids
   match_mode/follow_depth/impl_only; summary is incompatible with cursor).
5. Output per check: correct / incorrect / omitted / hallucinated-default, plus
   tool_selection_correct true/false. Do not pool checks.

Ground truth is cell-invariant; never guess or use which experiment arm produced the
call.
