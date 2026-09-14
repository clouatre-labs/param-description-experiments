"""Experiment harness for the MCP parameter-description ablation.

Loads the frozen fixtures and prompts for an experiment directory, builds the Anthropic
Messages API tools array per cell, and runs the full cells x models x prompts x runs grid.

Run IDs are opaque (run_0001, run_0002, ...) and carry no cell/model/prompt information;
that mapping lives only in label-map.json, sealed at the start of the run and not consulted
until scoring is done, so a scorer reading raw/<run_id>.json cannot infer which cell produced
a given call.

Usage:
    uv run recipe/harness.py --experiment experiments/exp1-analyze-symbol --dry-run
    uv run recipe/harness.py --experiment experiments/exp1-analyze-symbol --smoke-test
    uv run recipe/harness.py --experiment experiments/exp1-analyze-symbol --confirm-full-run
"""

from __future__ import annotations

import argparse
import json
import os
import random
import string
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import sleep

MODELS = ["claude-haiku-4-5-20251001", "claude-sonnet-5"]
RUNS_PER_CELL_MODEL_PROMPT = 5
MAX_TOKENS = 1024

OPENROUTER_MODEL_SLUGS = {
    "claude-haiku-4-5-20251001": "anthropic/claude-haiku-4.5",
    "claude-sonnet-5": "anthropic/claude-sonnet-5",
}
OPENROUTER_MESSAGES_URL = "https://openrouter.ai/api/v1/messages"
OPENROUTER_BATCHES_URL = "https://openrouter.ai/api/beta/batches"
OPENROUTER_BATCH_TERMINAL_STATUSES = {"completed", "failed", "expired", "cancelled"}
OPENROUTER_BATCH_POLL_SECONDS = 5
# 1h margin over OpenRouter's documented 24h completion window (see README.md).
OPENROUTER_BATCH_MAX_WAIT_SECONDS = 25 * 60 * 60


@dataclass
class Call:
    run_id: str
    cell: str
    model: str
    prompt_id: str
    run_index: int
    tools: list[dict]
    prompt_text: str
    target_tool: str = ""


def load_experiment(exp_dir: Path) -> tuple[dict[str, dict], list[dict], list[dict]]:
    """Load fixtures and prompts.

    Cells are derived from the fixture files present (exp1: a-d; exp2: a-b), so the
    grid adapts per experiment without editing this module. A cell fixture has either
    a single "tool" (exp1 style) or a "tools" dict keyed by tool name plus per-cell
    "distractors" (exp2 multi-tool style, where the arm must vary distractor schemas
    too). Top-level distractors.json is the exp1 fallback and is shared across cells.
    """
    fixtures_dir = exp_dir / "fixtures"
    cell_ids = sorted(
        p.stem.removeprefix("cell-")
        for p in fixtures_dir.glob("cell-*.json")
    )
    cells = {
        c: json.loads((fixtures_dir / f"cell-{c}.json").read_text())
        for c in cell_ids
    }
    distractors_path = fixtures_dir / "distractors.json"
    distractors = (
        json.loads(distractors_path.read_text())["tools"]
        if distractors_path.exists()
        else []
    )
    prompts = json.loads((exp_dir / "prompts.json").read_text())["prompts"]
    return cells, distractors, prompts


def build_tools(cell: dict, distractors: list[dict], target_tool: str) -> list[dict]:
    """Assemble the tools array for one call.

    exp1 cells carry a single "tool"; target_tool is then informational. exp2 cells
    carry a "tools" dict; the prompt's target_tool picks the primary variant and the
    cell's own distractors (minus the target itself) fill out the array.
    """
    if "tools" in cell:
        primary = cell["tools"][target_tool]
        others = []
        for t in cell["distractors"]:
            if t["name"] == target_tool:
                continue
            # Distractor entries may alias a cell tool's schema to avoid duplicating
            # large JSON blobs in the fixture ("SAME_AS tools.<name>").
            schema = t["input_schema"]
            if isinstance(schema, str) and schema.startswith("SAME_AS tools."):
                schema = cell["tools"][schema.removeprefix("SAME_AS tools.")][
                    "input_schema"
                ]
            others.append({**t, "input_schema": schema})
    else:
        primary = cell["tool"]
        others = distractors
    return [
        {
            "name": t["name"],
            "description": t["description"],
            "input_schema": t["input_schema"],
        }
        for t in [primary, *others]
    ]


def gen_run_id(existing: set[str]) -> str:
    while True:
        rid = "run_" + "".join(random.choices(string.digits, k=6))
        if rid not in existing:
            existing.add(rid)
            return rid


def plan_calls(
    cells: dict[str, dict], distractors: list[dict], prompts: list[dict]
) -> list[Call]:
    calls: list[Call] = []
    seen_ids: set[str] = set()
    for cell_id in cells:
        for model in MODELS:
            for prompt in prompts:
                target_tool = prompt.get("target_tool") or (
                    next(iter(cells[cell_id]["tools"]))
                    if "tools" in cells[cell_id]
                    else cells[cell_id]["tool"]["name"]
                )
                tools = build_tools(cells[cell_id], distractors, target_tool)
                for run_index in range(1, RUNS_PER_CELL_MODEL_PROMPT + 1):
                    calls.append(
                        Call(
                            run_id=gen_run_id(seen_ids),
                            cell=cell_id,
                            model=model,
                            prompt_id=prompt["id"],
                            run_index=run_index,
                            tools=tools,
                            prompt_text=prompt["text"],
                            target_tool=target_tool,
                        )
                    )
    random.shuffle(calls)  # execution order decorrelated from generation order
    return calls


def resolve_provider() -> tuple[str, str]:
    """Pick a calling provider from environment credentials.

    ANTHROPIC_API_KEY takes priority when both are set, preserving the harness's original
    default behavior. OPENROUTER_API_KEY is the alternate path (native Anthropic Messages
    shape via OpenRouter's /v1/messages and async batch endpoints).
    """
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if anthropic_key:
        return "anthropic", anthropic_key
    openrouter_key = os.environ.get("OPENROUTER_API_KEY")
    if openrouter_key:
        return "openrouter", openrouter_key
    raise SystemExit(
        "Set ANTHROPIC_API_KEY or OPENROUTER_API_KEY in the environment to run "
        "--smoke-test or --confirm-full-run."
    )


def build_messages_body(call: Call, model: str) -> dict:
    # No temperature/top_p/top_k passed: claude-sonnet-5 returns HTTP 400 on any explicit
    # value, so both models run at API default for comparability (frozen run spec).
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "tools": call.tools,
        "tool_choice": {"type": "auto"},
        "messages": [{"role": "user", "content": call.prompt_text}],
    }


def _normalize_messages_response(body: dict) -> dict:
    tool_uses = [
        {"name": b["name"], "input": b["input"]}
        for b in body["content"]
        if b["type"] == "tool_use"
    ]
    text_blocks = [b["text"] for b in body["content"] if b["type"] == "text"]
    return {
        "stop_reason": body["stop_reason"],
        "tool_uses": tool_uses,
        "text": text_blocks,
        "usage": {
            "input_tokens": body["usage"]["input_tokens"],
            "output_tokens": body["usage"]["output_tokens"],
        },
    }


def _send_anthropic(client, call: Call) -> dict:
    response = client.messages.create(
        model=call.model,
        max_tokens=MAX_TOKENS,
        tools=call.tools,
        tool_choice={"type": "auto"},
        messages=[{"role": "user", "content": call.prompt_text}],
    )
    return _normalize_messages_response(response.model_dump())


def _send_openrouter(http_client, api_key: str, call: Call) -> dict:
    model_slug = OPENROUTER_MODEL_SLUGS[call.model]
    body = build_messages_body(call, model_slug)
    response = http_client.post(
        OPENROUTER_MESSAGES_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json=body,
    )
    response.raise_for_status()
    return _normalize_messages_response(response.json())


def execute_call(provider: str, client, call: Call, api_key: str) -> dict:
    if provider == "anthropic":
        return _send_anthropic(client, call)
    return _send_openrouter(client, api_key, call)


def submit_openrouter_batch(
    http_client, api_key: str, model_slug: str, calls: list[Call]
) -> str:
    body = {
        "endpoint": "/v1/messages",
        "model": model_slug,
        "requests": [
            {"custom_id": call.run_id, "body": build_messages_body(call, model_slug)}
            for call in calls
        ],
    }
    response = http_client.post(
        OPENROUTER_BATCHES_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json=body,
    )
    response.raise_for_status()
    return response.json()["id"]


def poll_openrouter_batch(http_client, api_key: str, batch_id: str) -> dict:
    url = f"{OPENROUTER_BATCHES_URL}/{batch_id}"
    elapsed = 0
    while elapsed <= OPENROUTER_BATCH_MAX_WAIT_SECONDS:
        response = http_client.get(url, headers={"Authorization": f"Bearer {api_key}"})
        if response.status_code == 404:
            # A batch can be briefly unqueryable right after creation (eventual
            # consistency on OpenRouter's side); treat as not-ready rather than fatal.
            sleep(OPENROUTER_BATCH_POLL_SECONDS)
            elapsed += OPENROUTER_BATCH_POLL_SECONDS
            continue
        response.raise_for_status()
        batch = response.json()
        if batch["status"] in OPENROUTER_BATCH_TERMINAL_STATUSES:
            return batch
        sleep(OPENROUTER_BATCH_POLL_SECONDS)
        elapsed += OPENROUTER_BATCH_POLL_SECONDS
    raise TimeoutError(
        f"Batch {batch_id} did not reach a terminal status within "
        f"{OPENROUTER_BATCH_MAX_WAIT_SECONDS}s. It may still be running; check "
        f"GET {OPENROUTER_BATCHES_URL}/{batch_id} directly."
    )


def run_calls_sync(
    provider: str,
    client,
    api_key: str,
    todo: list[Call],
    raw_dir: Path,
    label_map: dict | None = None,
) -> None:
    for call in todo:
        result = execute_call(provider, client, call, api_key)
        (raw_dir / f"{call.run_id}.json").write_text(
            json.dumps(result, indent=2) + "\n"
        )
        if label_map is not None:
            label_map["assignments"][call.run_id] = {
                "cell": call.cell,
                "model": call.model,
                "prompt_id": call.prompt_id,
                "run_index": call.run_index,
            }
        print(
            f"  {call.run_id}: {result['stop_reason']}, "
            f"{len(result['tool_uses'])} tool_use block(s)"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run", action="store_true", help="Build payloads, send nothing."
    )
    mode.add_argument(
        "--smoke-test", action="store_true", help="Send exactly 1 real call."
    )
    mode.add_argument(
        "--confirm-full-run",
        action="store_true",
        help="Send all 320 calls. Costs real money. Requires explicit user go-ahead.",
    )
    mode.add_argument(
        "--pilot",
        action="store_true",
        help="Send exactly 8 real calls (one per cell/model pair) synchronously to "
        "raw/pilot/, outside the sealed run.",
    )
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Force the synchronous per-call path for --confirm-full-run under "
        "provider=openrouter, bypassing the batch API. No-op for provider=anthropic "
        "(already synchronous) and for other modes.",
    )
    args = parser.parse_args()

    random.seed(705)  # deterministic run-id/order generation for reproducibility

    exp_dir = args.experiment
    cells, distractors, prompts = load_experiment(exp_dir)
    calls = plan_calls(cells, distractors, prompts)
    print(
        f"Planned {len(calls)} calls across {len(cells)} cells x {len(MODELS)} models x "
        f"{len(prompts)} prompts x {RUNS_PER_CELL_MODEL_PROMPT} runs."
    )

    if args.dry_run:
        sample = calls[0]
        print(
            f"Sample call (run_id={sample.run_id}, cell={sample.cell}, model={sample.model}, "
            f"prompt={sample.prompt_id}):"
        )
        print(
            json.dumps(
                {
                    "model": sample.model,
                    "max_tokens": MAX_TOKENS,
                    "tools": sample.tools,
                    "tool_choice": {"type": "auto"},
                    "messages": [{"role": "user", "content": sample.prompt_text}],
                },
                indent=2,
            )
        )
        return

    provider, api_key = resolve_provider()

    if provider == "anthropic":
        import anthropic  # deferred: only needed for real API calls

        client = anthropic.Anthropic()
        if args.sync:
            print("--sync has no effect for provider=anthropic (already synchronous)")
    else:
        import httpx  # deferred: only needed for real API calls

        client = httpx.Client(timeout=120.0)

    todo = calls[:1] if args.smoke_test else calls

    if args.smoke_test:
        # Smoke-test output is scratch, kept out of raw/ and label-map.json so it can never
        # be mistaken for (or pollute) the sealed experimental record.
        raw_dir = exp_dir / "raw" / "smoke-test"
        raw_dir.mkdir(parents=True, exist_ok=True)
        run_calls_sync(provider, client, api_key, todo, raw_dir, label_map=None)
        print(
            f"Smoke test: wrote {len(todo)} result(s) to {raw_dir} (not part of the sealed run)"
        )
        return

    if args.pilot:
        # Pilot output is scratch, kept out of raw/ and label-map.json, same rationale as
        # smoke-test: one call per (cell, model) pair to sanity-check the full grid's
        # coverage without touching the sealed experimental record.
        pilot_by_cell_model: dict[tuple[str, str], Call] = {}
        for call in calls:
            pilot_by_cell_model.setdefault((call.cell, call.model), call)
        pilot_calls = list(pilot_by_cell_model.values())
        raw_dir = exp_dir / "raw" / "pilot"
        raw_dir.mkdir(parents=True, exist_ok=True)
        run_calls_sync(provider, client, api_key, pilot_calls, raw_dir, label_map=None)
        print(
            f"Pilot: wrote {len(pilot_calls)} result(s) to {raw_dir} "
            f"(not part of the sealed run)"
        )
        return

    print(
        f"Executing all {len(todo)} calls against the live API. This spends real money."
    )
    raw_dir = exp_dir / "raw"
    raw_dir.mkdir(exist_ok=True)
    label_map_path = exp_dir / "label-map.json"
    label_map = json.loads(label_map_path.read_text())

    if provider == "anthropic" or args.sync:
        run_calls_sync(provider, client, api_key, todo, raw_dir, label_map)
    else:
        # Async Batch API: OpenRouter rejects a batch whose request bodies' model fields
        # don't all match the top-level model field (HTTP 400), so submit one batch per
        # model rather than one batch for the whole run.
        calls_by_model: dict[str, list[Call]] = {}
        for call in todo:
            calls_by_model.setdefault(call.model, []).append(call)

        results_by_run_id: dict[str, dict] = {}
        for model, model_calls in calls_by_model.items():
            model_slug = OPENROUTER_MODEL_SLUGS[model]
            batch_id = submit_openrouter_batch(client, api_key, model_slug, model_calls)
            # Logged before polling so the ID survives a network interruption during the
            # 24h window; the batch keeps running server-side and can be checked manually.
            print(
                f"  submitted batch {batch_id} for {model} ({len(model_calls)} calls)"
            )
            batch = poll_openrouter_batch(client, api_key, batch_id)
            for item in batch["results"]:
                if item.get("error"):
                    raise RuntimeError(
                        f"OpenRouter batch {batch_id} entry {item['custom_id']} failed: "
                        f"{item['error']}"
                    )
                results_by_run_id[item["custom_id"]] = _normalize_messages_response(
                    item["response"]["body"]
                )

        for call in todo:
            result = results_by_run_id[call.run_id]
            (raw_dir / f"{call.run_id}.json").write_text(
                json.dumps(result, indent=2) + "\n"
            )
            label_map["assignments"][call.run_id] = {
                "cell": call.cell,
                "model": call.model,
                "prompt_id": call.prompt_id,
                "run_index": call.run_index,
            }
            print(
                f"  {call.run_id}: {result['stop_reason']}, "
                f"{len(result['tool_uses'])} tool_use block(s)"
            )

    label_map["sealed_at"] = datetime.now(UTC).isoformat()
    label_map_path.write_text(json.dumps(label_map, indent=2) + "\n")
    print(f"Wrote {len(todo)} raw result file(s) to {raw_dir}, sealed {label_map_path}")


if __name__ == "__main__":
    main()
