# Protocol: exp2-response-format

Pre-registered design for the response_format collapse A/B experiment. This file freezes
the run-time parameters; see `rubric.md` for the scoring definitions. Design spec:
aptu-coder `docs/audit/2026-09-14-response-format-experiment-design.md` (PR #1553).

## Question

Does collapsing the response-shaping parameters (`summary`, `fields` on `analyze_file`;
`mode`, `impl_only` on `analyze_symbol`) into a single payload-carrying `response_format`
enum improve or regress agent parameter-filling accuracy, and at what token cost?

## Design (2 arms x 2 target tools x 2 models)

- **Arm A (baseline, cell a):** current production schemas -- schemars serialization of
  `AnalyzeFileParams` / `AnalyzeSymbolParams` generated 2026-09-14 from aptu-coder
  worktree `20260914_65` (origin/main HEAD, post-#1543 `mode` enum) via `schema_for!`.
- **Arm B (collapsed, cell b):** same schemas with `summary`+`fields` (file) and
  `mode`+`impl_only` (symbol) removed and replaced by a `response_format` property per
  the design doc's mapping table. Lookup semantics (`match_mode`, `follow_depth`,
  `max_depth`, `cursor`, `git_ref`) stay top-level. `Full` (the omitted default)
  preserves tri-state auto-summarize above 50K chars.
- Every cell presents both target tools plus `analyze_directory` and the opposite
  target tool as distractors, so each tools array is arm-consistent.
- Each of the 8 prompts is bound to one target tool (`analyze_file` or `analyze_symbol`);
  the scorer uses that binding for tool-selection accuracy and category dispatch.

## Frozen run parameters

```text
Models: claude-haiku-4-5-20251001, claude-sonnet-5
Thinking: off
Temperature: default/unset (claude-sonnet-5 rejects explicit temperature/top_p/top_k)
tool_choice: auto
Prompts: N = 8 (prompts.json), 4 analyze_file + 4 analyze_symbol
Runs/prompt/cell/model: 5 -> 40 observations/cell/model (meets the design doc's n >= 40)
Total tool-use calls: 2 cells x 2 models x 8 prompts x 5 runs = 160
Primary test: Mann-Whitney U, collapsed (b) vs baseline (a), param_fill_score,
  two-tailed alpha=0.05, per model
Secondary test: same on input_tokens (token-cost regression gate)
Effect size: rank-biserial r with bootstrap 95% percentile CI
```

Note: the experiment plan text states "320 calls" for the same factorization; the
correct product of its own factors (2 x 2 x 8 x 5) is 160, which is what was run and
what satisfies the design doc's n >= 40 per cell per model.

## Metrics

1. Parameter-filling accuracy (`param_fill_score`): binary conjunction of per-param
   checks. The intent check carries the four-way categorical (correct / incorrect /
   omitted / hallucinated-default); incorrect_if traps are separate correct/incorrect
   checks. Never pooled.
2. Tool-selection accuracy (first tool call == prompt's target tool).
3. Schema token cost (`usage.input_tokens` per call).

Scoring is arm-blind: the scorer inspects the union surface (baseline params AND
`response_format`) and credits intent expressed on exactly one surface. Setting both
surfaces at once is cross-surface leakage, scored incorrect.

## Blinding

Same seal-and-reveal mechanics as exp1: `label-map.json` (run_id -> cell/model/prompt)
is written at harness run time and joined only after blind scoring.

## GO/NO-GO gates

- GO: statistically significant accuracy win (p < 0.05, MWU per model on
  param_fill_score) with no token-cost regression (input_tokens MWU not significantly
  worse for arm b).
- NO-GO: null result or token-cost regression. On NO-GO, keep the 4-param surface and
  record the negative result in the aptu-coder cognitive-load audit trail.
- Implementation (if GO) is a separate PR: legacy param removal, sequenced after #1543.

## Status

Fixtures, rubric, and harness extension: done; dry-run, grid-completeness, and scorer
sanity checks passed; pilot (4 calls, one per cell/model) clean. Full 160-call run:
executed 2026-09-14 under `OPENROUTER_API_KEY` via the async Batch API (batches
`batch-1789419778-z37wwwGUPj3UiaQWfTFr` sonnet-5, `batch-1789420056-PztapBS08ceE2VubNv8m`
haiku-4.5), blind-scored, and analyzed; see `scores.json` and `analysis.json`.
Outcome: NO-GO (null accuracy result at significant token-cost savings); see the results
entry in aptu-coder `docs/audit/2026-09-14-response-format-experiment-results.md`.
