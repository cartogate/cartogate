"""Live agent A/B runner — V1 (token usage with vs. without Cartogate).

For each task, runs a real Claude tool-use loop twice — once with the Cartogate tools and
once with only generic read/list/grep — over N trials, and records the token cost of each
arm. Token usage is read from the API ``usage`` field (exact). Results (mean ± stdev over
trials) are merged into ``evaluation/value_results.json`` under key ``V1``.

Usage:
    export ANTHROPIC_API_KEY=...
    python -m evaluation.corpus.fetch_corpus
    python -m evaluation.run_ab --trials 3 --model claude-sonnet-4-6

Run ``--corpus PATH`` to point at any importable package (defaults to the fetched click
snapshot). Nothing here runs in CI; commit the refreshed results file for retraceability.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cartogate.extract.pipeline import index_package
from cartogate.mcp.tools import CartogateTools
from cartogate.store import InMemoryStore

from . import agent_tools_with, agent_tools_without, tasks
from .tasks import SYSTEM_PROMPT, Task

REPO_ROOT = Path(__file__).resolve().parents[1]
RESULTS_PATH = REPO_ROOT / "docs" / "value_results.json"
DEFAULT_MODEL = "claude-sonnet-4-6"
MAX_TURNS = 12
MAX_TOKENS = 1024

Executor = Callable[[str, dict[str, Any]], dict[str, Any]]


@dataclass
class Episode:
    input_tokens: int
    output_tokens: int
    turns: int
    tool_calls: int
    final_text: str

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def run_episode(
    client: Any, model: str, prompt: str, tool_schemas: list[dict[str, Any]], executor: Executor
) -> Episode:
    """Drive one tool-use conversation to completion; tally tokens/turns/tool-calls."""
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    in_tok = out_tok = turns = tool_calls = 0
    final_text = ""
    for _ in range(MAX_TURNS):
        resp = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            tools=tool_schemas,
            messages=messages,
        )
        in_tok += resp.usage.input_tokens
        out_tok += resp.usage.output_tokens
        turns += 1
        messages.append({"role": "assistant", "content": [b.model_dump() for b in resp.content]})
        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        final_text = "".join(b.text for b in resp.content if b.type == "text")
        if not tool_uses:
            break
        results = []
        for tu in tool_uses:
            tool_calls += 1
            try:
                out = executor(tu.name, dict(tu.input))
            except Exception as exc:  # surface tool errors to the model, don't crash the run
                out = {"error": str(exc)}
            results.append(
                {"type": "tool_result", "tool_use_id": tu.id, "content": json.dumps(out)}
            )
        messages.append({"role": "user", "content": results})
    return Episode(in_tok, out_tok, turns, tool_calls, final_text)


def _summarize(values: list[float]) -> dict[str, float]:
    return {
        "mean": round(statistics.fmean(values), 1),
        "stdev": round(statistics.stdev(values), 1) if len(values) > 1 else 0.0,
        "n": len(values),
    }


def run(corpus: Path, model: str, trials: int) -> dict[str, Any]:
    import anthropic  # lazy: only needed for a live run

    client = anthropic.Anthropic()
    store = InMemoryStore()
    result = index_package(corpus, repo_id=corpus.name, store=store, resolve=True, index_docs=True)
    tools = CartogateTools(store)

    with_schemas = agent_tools_with.tool_schemas()
    with_exec = agent_tools_with.make_executor(tools)
    without_schemas = agent_tools_without.tool_schemas()
    without_exec = agent_tools_without.make_executor(corpus)

    task_list: list[Task] = tasks.build_tasks(result.nodes, tools)
    per_task: list[dict[str, Any]] = []
    with_totals: list[float] = []
    without_totals: list[float] = []

    for task in task_list:
        with_runs = [
            run_episode(client, model, task.prompt, with_schemas, with_exec) for _ in range(trials)
        ]
        without_runs = [
            run_episode(client, model, task.prompt, without_schemas, without_exec)
            for _ in range(trials)
        ]
        w_tokens = [float(e.total_tokens) for e in with_runs]
        wo_tokens = [float(e.total_tokens) for e in without_runs]
        with_totals += w_tokens
        without_totals += wo_tokens
        w_mean = statistics.fmean(w_tokens)
        wo_mean = statistics.fmean(wo_tokens)
        per_task.append({
            "id": task.id,
            "kind": task.kind,
            "with_cartogate_tokens": _summarize(w_tokens),
            "without_cartogate_tokens": _summarize(wo_tokens),
            "token_reduction_pct": round(100 * (1 - w_mean / wo_mean), 1) if wo_mean else 0.0,
            "with_turns_mean": round(statistics.fmean([e.turns for e in with_runs]), 1),
            "without_turns_mean": round(statistics.fmean([e.turns for e in without_runs]), 1),
            "with_correct": sum(tasks.grade(task, e.final_text).correct for e in with_runs),
            "without_correct": sum(tasks.grade(task, e.final_text).correct for e in without_runs),
        })

    agg_with = statistics.fmean(with_totals) if with_totals else 0.0
    agg_without = statistics.fmean(without_totals) if without_totals else 0.0
    reduction = round(100 * (1 - agg_with / agg_without), 1) if agg_without else 0.0
    return {
        "id": "V1",
        "bucket": "A",
        "title": "Token usage (live agent, with vs. without Cartogate)",
        "claim": "An agent answers codebase questions with fewer tokens when Cartogate's tools "
        "are available than when it must read/grep the source.",
        "metric": {
            "model": model,
            "trials_per_arm": trials,
            "corpus": corpus.name,
            "overall_token_reduction_pct": reduction,
            "with_cartogate_tokens": _summarize(with_totals),
            "without_cartogate_tokens": _summarize(without_totals),
            "tasks": per_task,
        },
        "passed": reduction > 0,
        "reproduce": "python -m evaluation.run_ab --trials 3 --model " + model,
        "notes": "Token counts are exact (API usage field); means ± stdev over trials. "
        "Model output is non-deterministic — re-run for fresh draws.",
    }


def _merge_results(row: dict[str, Any]) -> None:
    data: dict[str, Any] = {"schema_version": 1, "hypotheses": {}}
    if RESULTS_PATH.exists():
        data = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    data.setdefault("hypotheses", {})[row["id"]] = row
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Live agent A/B token study (V1).")
    parser.add_argument("--trials", type=int, default=3, help="Trials per arm per task.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Claude model id.")
    parser.add_argument(
        "--corpus", type=Path, default=None, help="Path to an importable package to study."
    )
    args = parser.parse_args()

    corpus = args.corpus
    if corpus is None:
        from .corpus.fetch_corpus import fetch

        corpus = fetch()
    row = run(corpus.resolve(), args.model, args.trials)
    _merge_results(row)
    print(json.dumps(row["metric"], indent=2))


if __name__ == "__main__":
    main()
