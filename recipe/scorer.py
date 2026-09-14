"""Blind scoring pipeline for exp1-analyze-symbol.

Reads `raw/<run_id>.json` tool calls and scores them against `prompts.json`'s pre-registered
expected values and `incorrect_if` traps. `label-map.json` is read only to project
`run_id -> prompt_id` (cell/model are discarded); the rubric applied to a given run never
depends on which cell or model produced it. `raw/pilot/` is excluded (glob("*.json") on
`raw/` does not descend into the `pilot/` subdirectory).

Usage:
    uv run recipe/scorer.py --experiment experiments/exp1-analyze-symbol
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

SCHEMA_DEFAULTS: dict[str, object] = {
    "match_mode": "exact",
    "follow_depth": 1,
    "impl_only": False,
    "import_lookup": False,
    "def_use": False,
}

CAVEAT_PATTERN = re.compile(
    r"(?i)\b(large|expensive|deep|warn|caution|exponential|significant.*(output|size|volume))\b"
)


def score_match_mode(value: object, expected: object) -> str:
    """Score match_mode against a prompt's expected non-default value(s).

    correct: value is one of the expected values.
    hallucinated_default: value explicitly set to the schema default ("exact") while a
        non-default value was expected.
    omitted: match_mode absent from the call while a non-default value was expected.
    incorrect: any other explicit wrong value.
    """
    expected_values = expected if isinstance(expected, list) else [expected]
    if value in expected_values:
        return "correct"
    if value is None:
        return "omitted"
    default = SCHEMA_DEFAULTS["match_mode"]
    if value == default and default not in expected_values:
        return "hallucinated_default"
    return "incorrect"


def score_follow_depth(value: object, expected: object) -> str:
    """Same four-way mechanism as score_match_mode; schema default is 1."""
    expected_values = expected if isinstance(expected, list) else [expected]
    if value in expected_values:
        return "correct"
    if value is None:
        return "omitted"
    default = SCHEMA_DEFAULTS["follow_depth"]
    if value == default and default not in expected_values:
        return "hallucinated_default"
    return "incorrect"


def _is_non_default(param: str, value: object) -> bool:
    if value is None:
        return False
    return value != SCHEMA_DEFAULTS.get(param)


def _param_violates(param: str, value: object, condition: object) -> bool:
    if condition == "any_non_default":
        return _is_non_default(param, value)
    if condition == "value_other_than_1_or_unset":
        return value is not None and value != 1
    if condition == "value_other_than_exact_or_unset":
        return value is not None and value != "exact"
    if isinstance(condition, list):
        for item in condition:
            if item == "any_non_default":
                if _is_non_default(param, value):
                    return True
            elif value == item:
                return True
        return False
    # Literal scalar trap, e.g. def_use=True (P6) or impl_only=True (P8).
    return value == condition


def score_mutual_exclusion(call_input: dict, prompt: dict) -> dict[str, str]:
    """P5/P6: import_lookup=true expected; incorrect_if params must stay unset/default.

    Categorical: correct/incorrect only, no omitted/hallucinated-default bucket.
    """
    checks = {
        "import_lookup": "correct"
        if call_input.get("import_lookup") is True
        else "incorrect"
    }
    for param, condition in prompt["incorrect_if"].items():
        value = call_input.get(param)
        checks[param] = (
            "incorrect" if _param_violates(param, value, condition) else "correct"
        )
    return checks


def score_should_not_set(call_input: dict, prompt: dict) -> dict[str, str]:
    """P7/P8: optional flags must stay at their default/unset value.

    Categorical: correct/incorrect only, no omitted/hallucinated-default bucket.
    """
    checks = {}
    for param, condition in prompt["incorrect_if"].items():
        value = call_input.get(param)
        checks[param] = (
            "incorrect" if _param_violates(param, value, condition) else "correct"
        )
    return checks


def score_tool_selection(tool_uses: list[dict], target_tool: str = "analyze_symbol") -> bool:
    """Correct iff the first tool call is the prompt's target tool, not a distractor."""
    if not tool_uses:
        return False
    return tool_uses[0].get("name") == target_tool


def caveat_present(text: list[str]) -> bool:
    """P4 exploratory-only: does the model's prose volunteer a size/depth caveat."""
    combined = " ".join(text)
    return bool(CAVEAT_PATTERN.search(combined))


def param_fill_score(checks: dict[str, str]) -> int:
    """Binary: 1 iff every applicable check for this run is 'correct', else 0."""
    if not checks:
        return 0
    return 1 if all(value == "correct" for value in checks.values()) else 0


def compute_checks(
    category: str, call_input: dict, prompt: dict, has_tool_call: bool
) -> dict[str, str]:
    if category == "retry_match_mode":
        checks = {
            "match_mode": score_match_mode(
                call_input.get("match_mode"), prompt["expected"]["match_mode"]
            )
        }
    elif category == "follow_depth_warning":
        checks = {
            "follow_depth": score_follow_depth(
                call_input.get("follow_depth"), prompt["expected"]["follow_depth"]
            )
        }
    elif category == "mutual_exclusion":
        checks = score_mutual_exclusion(call_input, prompt)
    elif category == "should_not_set_flag":
        checks = score_should_not_set(call_input, prompt)
    else:
        raise ValueError(f"unknown prompt category: {category}")
    if not has_tool_call:
        # No tool call to inspect (e.g. the model asked a clarifying question instead).
        checks = dict.fromkeys(checks, "omitted")
    return checks


def score_run(run_id: str, run: dict, prompt: dict) -> dict:
    tool_uses = run.get("tool_uses", [])
    call_input = tool_uses[0]["input"] if tool_uses else {}
    target_tool = prompt.get("target_tool", "analyze_symbol")
    if "target_tool" in prompt:
        checks = compute_exp2_checks(
            prompt["category"], call_input, prompt, has_tool_call=bool(tool_uses)
        )
    else:
        checks = compute_checks(
            prompt["category"], call_input, prompt, has_tool_call=bool(tool_uses)
        )
    caveat = caveat_present(run.get("text", [])) if prompt["id"] == "P4" else None
    return {
        "run_id": run_id,
        "prompt_id": prompt["id"],
        "category": prompt["category"],
        "checks": checks,
        "tool_selection_correct": score_tool_selection(tool_uses, target_tool),
        "input_tokens": run["usage"]["input_tokens"],
        "param_fill_score": param_fill_score(checks),
        "caveat_present": caveat,
    }


# --- exp2-response-format scoring -----------------------------------------------
#
# Arm-blind: the rubric applied to a run never depends on which cell produced it.
# The scorer inspects the union surface (baseline params AND response_format) and a
# call is correct iff the intent is expressed on exactly one surface with the other
# surface's shaping params left alone. Setting both surfaces at once (e.g.
# summary=true together with response_format="summary") is cross-surface leakage,
# scored incorrect. The four-way categorical (correct / incorrect / omitted /
# hallucinated-default) and per-param sub-scores are kept un-pooled, exactly as in
# exp1: the intent check carries the four-way score, incorrect_if traps are separate
# correct/incorrect checks, and param_fill_score is the binary conjunction.


def _rf(call_input: dict) -> object:
    """The call's response_format value, or None if absent."""
    return call_input.get("response_format")


def _rf_fields(call_input: dict) -> object:
    rf = _rf(call_input)
    return rf.get("fields") if isinstance(rf, dict) else None


def _shaping_is_default(call_input: dict, summary_default: bool) -> bool:
    """Full/default response shaping on the union surface.

    summary_default=True means baseline summary=false counts as explicit-but-correct
    full output; False means even summary=false is a deviation (used where the prompt
    pre-registers summary as absent, e.g. alongside cursor).
    """
    rf = _rf(call_input)
    summary = call_input.get("summary")
    fields = call_input.get("fields")
    rf_fields = _rf_fields(call_input)
    summary_ok = summary is None or (summary_default and summary is False)
    rf_ok = rf is None or rf == "full"
    return summary_ok and rf_ok and fields is None and rf_fields is None


def _intent_score(call_input: dict, accept: tuple[bool, bool]) -> str:
    """Four-way categorical for a one-of-two-surfaces intent.

    accept = (baseline_surface_holds, collapsed_surface_holds). Both holding is
    leakage (incorrect); neither holding distinguishes omitted from
    hallucinated-default via the explicit response_format/full marker.
    """
    a_ok, b_ok = accept
    if a_ok and b_ok:
        return "incorrect"
    if a_ok or b_ok:
        return "correct"
    if _rf(call_input) is None and call_input.get("summary") is None:
        return "omitted"
    if _rf(call_input) == "full":
        return "hallucinated_default"
    return "incorrect"


def _exp2_summary_intent(call_input: dict) -> dict[str, str]:
    a_ok = call_input.get("summary") is True and _rf(call_input) is None
    b_ok = _rf(call_input) == "summary" and call_input.get("summary") is None
    return {"summary": _intent_score(call_input, (a_ok, b_ok))}


def _exp2_fields_projection(call_input: dict, expected: object) -> dict[str, str]:
    expected_values = expected if isinstance(expected, list) else [expected]

    def matches(value: object) -> bool:
        return isinstance(value, list) and sorted(str(v) for v in value) == sorted(
            str(v) for v in expected_values
        )

    a_fields = call_input.get("fields")
    a_ok = (
        matches(a_fields)
        and _rf(call_input) is None
        and call_input.get("summary") is None
    )
    b_fields = _rf_fields(call_input)
    b_ok = (
        matches(b_fields)
        and call_input.get("fields") is None
        and call_input.get("summary") is None
    )
    checks = {"fields": _intent_score(call_input, (a_ok, b_ok))}
    # An explicit value that fails both surfaces is incorrect, not omitted.
    if checks["fields"] == "omitted" and (
        a_fields is not None or _rf(call_input) is not None
    ):
        checks["fields"] = "incorrect"
    # Trap: summary=true silently drops fields (locked behavior); any summary
    # setting on a fields-intent call is a separate incorrect check.
    checks["summary_not_set"] = (
        "incorrect" if call_input.get("summary") is not None else "correct"
    )
    return checks


def _exp2_full_default(call_input: dict) -> dict[str, str]:
    return {
        "full_output": "correct" if _shaping_is_default(call_input, True) else "incorrect"
    }


def _exp2_cursor_pagination(call_input: dict, expected_cursor: object) -> dict[str, str]:
    cursor = call_input.get("cursor")
    checks = {
        "cursor": (
            "correct"
            if cursor == expected_cursor
            else "omitted" if cursor is None else "incorrect"
        )
    }
    # Pre-registered absence: summary / response_format=summary is mutually
    # exclusive with cursor on both arms.
    summary = call_input.get("summary")
    rf = _rf(call_input)
    checks["summary_absent"] = (
        "incorrect" if summary is True or rf == "summary" else "correct"
    )
    return checks


def _exp2_call_graph_default(call_input: dict) -> dict[str, str]:
    mode = call_input.get("mode")
    ok = _shaping_is_default(call_input, True) and (mode is None or mode == "call_graph")
    ok = ok and call_input.get("match_mode") in (None, "exact")
    ok = ok and call_input.get("follow_depth") in (None, 1)
    ok = ok and call_input.get("impl_only") is None
    return {"call_graph": "correct" if ok else "incorrect"}


def _exp2_mode_intent(
    call_input: dict, baseline_value: str, collapsed_value: str, baseline_param: str
) -> dict[str, str]:
    baseline = call_input.get(baseline_param)
    rf = _rf(call_input)
    a_ok = baseline == baseline_value and rf is None
    b_ok = rf == collapsed_value and baseline is None
    result = _intent_score(call_input, (a_ok, b_ok))
    if result == "omitted" and baseline is not None:
        # Explicit surface value that is not the intent: the mode enum's default
        # ("call_graph") is the hallucinated-default trap, anything else incorrect.
        result = "hallucinated_default" if baseline == "call_graph" else "incorrect"
    return {collapsed_value: result}


def _exp2_impl_only(call_input: dict) -> dict[str, str]:
    mode = call_input.get("mode")
    mode_ok = mode is None or mode == "call_graph"
    a_ok = (
        call_input.get("impl_only") is True and _rf(call_input) is None and mode_ok
    )
    b_ok = (
        _rf(call_input) == "impl_only" and call_input.get("impl_only") is None and mode_ok
    )
    checks = {"impl_only": _intent_score(call_input, (a_ok, b_ok))}
    # An explicit surface value that is not the intent is incorrect, not omitted.
    if (
        checks["impl_only"] == "omitted"
        and (call_input.get("impl_only") is not None or _rf(call_input) is not None)
    ):
        checks["impl_only"] = "incorrect"
    checks["mode_not_lookup_or_defuse"] = (
        "incorrect" if mode in ("import_lookup", "def_use") else "correct"
    )
    return checks


def _exp2_import_lookup(call_input: dict) -> dict[str, str]:
    checks = _exp2_mode_intent(call_input, "import_lookup", "import_lookup", "mode")

    def absent(param: str, value: object) -> bool:
        # follow_depth=1 is absence-equivalent: the baseline schema (schemars
        # serialization) marks follow_depth as a required property with default 1,
        # so a conforming baseline-arm call always carries it.
        if param == "follow_depth":
            return value is None or value == 1
        return value is None

    # Pre-registered ABSENCES on both arms: import_lookup rejects
    # match_mode/follow_depth/impl_only (server-side mutual exclusion).
    for param in ("match_mode", "follow_depth", "impl_only"):
        checks[f"{param}_absent"] = (
            "correct" if absent(param, call_input.get(param)) else "incorrect"
        )
    return checks


def compute_exp2_checks(
    category: str, call_input: dict, prompt: dict, has_tool_call: bool
) -> dict[str, str]:
    expected = prompt.get("expected", {})
    if category == "summary_intent":
        checks = _exp2_summary_intent(call_input)
    elif category == "fields_projection":
        checks = _exp2_fields_projection(call_input, expected["arm_a"]["fields"])
    elif category == "full_default":
        checks = _exp2_full_default(call_input)
    elif category == "cursor_pagination":
        checks = _exp2_cursor_pagination(call_input, expected["arm_a"]["cursor"])
    elif category == "call_graph_default":
        checks = _exp2_call_graph_default(call_input)
    elif category == "impl_only_mode":
        checks = _exp2_impl_only(call_input)
    elif category == "def_use_mode":
        checks = _exp2_mode_intent(call_input, "def_use", "def_use", "mode")
    elif category == "import_lookup_mode":
        checks = _exp2_import_lookup(call_input)
    else:
        raise ValueError(f"unknown exp2 prompt category: {category}")
    if not has_tool_call:
        checks = dict.fromkeys(checks, "omitted")
    return checks


def load_prompts(exp_dir: Path) -> dict[str, dict]:
    data = json.loads((exp_dir / "prompts.json").read_text())
    return {p["id"]: p for p in data["prompts"]}


def load_run_prompt_ids(exp_dir: Path) -> dict[str, str]:
    """Blind projection of label-map.json: run_id -> prompt_id only. Cell/model discarded."""
    data = json.loads((exp_dir / "label-map.json").read_text())
    return {
        run_id: assignment["prompt_id"]
        for run_id, assignment in data["assignments"].items()
    }


def iter_raw_runs(exp_dir: Path):
    raw_dir = exp_dir / "raw"
    for path in sorted(raw_dir.glob("*.json")):
        yield path.stem, json.loads(path.read_text())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True, type=Path)
    args = parser.parse_args()

    exp_dir = args.experiment
    prompts = load_prompts(exp_dir)
    run_prompt_ids = load_run_prompt_ids(exp_dir)

    records = []
    for run_id, run in iter_raw_runs(exp_dir):
        prompt_id = run_prompt_ids[run_id]
        records.append(score_run(run_id, run, prompts[prompt_id]))
    records.sort(key=lambda r: r["run_id"])

    out_path = exp_dir / "scores.json"
    out_path.write_text(json.dumps(records, indent=2) + "\n")
    print(f"wrote {len(records)} records to {out_path}")


if __name__ == "__main__":
    main()
