# Rubric: exp2-response-format

Arm-blind scoring rules. A run's score never depends on which cell produced it; the
scorer sees only the tool call the model produced, joined against `prompts.json`'s
pre-registered expectations. The union surface is scored: a correct call expresses the
prompt's intent on exactly one surface (baseline params OR `response_format`) and leaves
the other surface's shaping params untouched. Setting both surfaces simultaneously is
cross-surface leakage -> incorrect.

## Check types

1. **Intent check** (four-way categorical, one per prompt):
   - `correct`: the intent expression holds on exactly one surface with the expected
     value(s).
   - `omitted`: neither surface expresses the intent (no tool call, or no shaping/mode
     param set where a non-default intent was expected).
   - `hallucinated_default`: the model explicitly set the surface default
     (`response_format: "full"`) where a non-default intent was expected (analogous to
     exp1's match_mode=exact trap).
   - `incorrect`: any other explicit wrong value, or both surfaces set (leakage).
2. **Trap checks** (correct/incorrect only): each pre-registered `incorrect_if`
   condition, scored independently. Never pooled into the intent check.
3. **`param_fill_score`**: 1 iff every applicable check is `correct`; else 0.
4. **`tool_selection_correct`**: first tool_use block's name == the prompt's
   `target_tool`.

## Per-category intent rules

| Category | Baseline (arm a) correct | Collapsed (arm b) correct |
| --- | --- | --- |
| `summary_intent` | `summary: true` (and no `response_format`) | `response_format: "summary"` (and `summary` absent) |
| `fields_projection` | `fields: ["functions"]` (no `summary`, no `response_format`) | `response_format: {"fields": ["functions"]}` (no `fields`, no `summary`) |
| `full_default` | `summary` absent-or-false, `fields` absent | `response_format` absent or `"full"` |
| `cursor_pagination` | `cursor` == expected token; `summary` absent (four-way on cursor) | same; `response_format: "summary"` is the forbidden equivalent |
| `call_graph_default` | `mode` absent-or-call_graph, `match_mode` absent-or-exact, `follow_depth` absent-or-1, `impl_only` absent, shaping default | same with `response_format` absent-or-full |
| `impl_only_mode` | `impl_only: true` (and no `response_format`; `mode` absent-or-call_graph) | `response_format: "impl_only"` (and `impl_only` absent; `mode` absent-or-call_graph) |
| `def_use_mode` | `mode: "def_use"` | `response_format: "def_use"` |
| `import_lookup_mode` | `mode: "import_lookup"` with `match_mode`/`follow_depth`/`impl_only` ABSENT | `response_format: "import_lookup"` with the same three ABSENT |

## Pre-registered absences (both arms where noted)

- `import_lookup` rejects `match_mode`, `follow_depth`, `impl_only` (server-side mutual
  exclusion, post-#1543 `mode` enum). Setting any is scored incorrect as a separate trap
  check, even though the server would only warn. Exception: `follow_depth: 1` is
  absence-equivalent because the baseline schema (schemars serialization) marks
  `follow_depth` as required with default 1, so a conforming baseline-arm call always
  carries it.
- `summary` (baseline) / `response_format: "summary"` (collapsed) is mutually exclusive
  with `cursor`; a correct cursor-pagination call sets neither.
- `summary: true` together with `fields` is incorrect: `fields` is silently dropped
  (behavior locked by aptu-coder's `analyze_file_fields_summary.rs` integration test),
  so the call does not do what the prompt asks.

## >50K auto-summarize

Server-side state, not behaviorally verifiable from tool inputs. Scored only as
parameter-choice intent: `full_default` and `call_graph_default` prompts require the
default (auto-summarize-preserving) surface value on both arms. No prompt claims to
verify the rendered output.
