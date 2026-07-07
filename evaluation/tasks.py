"""Tasks + deterministic graders for the live A/B study.

Each task is a realistic codebase question with an objective answer. Targets are *selected
from the indexed corpus at runtime* (not hardcoded), so the same task set works on any
package. Both arms are told to finish with a single ``ANSWER: ...`` line, which the grader
parses — so grading is identical and deterministic across arms regardless of prose style.

Truth is computed from Cartogate's resolved graph. That makes Cartogate the oracle for the
*correctness* dimension, so we treat the headline V1 metric as **token cost** (oracle-free)
and report correctness as a secondary, clearly-labeled signal.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from cartogate.mcp.tools import CartogateTools
from cartogate.schema.enums import NodeKind
from cartogate.schema.nodes import Node

_ANSWER_RE = re.compile(r"ANSWER:\s*(.*)", re.IGNORECASE)

SYSTEM_PROMPT = (
    "You are a precise coding assistant working in a codebase you can inspect only through "
    "the provided tools. Investigate as needed, then finish your reply with exactly one line:\n"
    "ANSWER: <comma-separated fully-qualified names, or NONE>\n"
    "Do not write or modify any code — only report findings."
)


@dataclass(frozen=True)
class Task:
    id: str
    kind: str
    prompt: str
    truth: frozenset[str]
    #: For the duplicate task, the single existing qualified name (else empty).
    expect_name: str = ""


@dataclass
class Grade:
    correct: bool
    precision: float
    recall: float
    answer: frozenset[str] = field(default_factory=frozenset)


def parse_answer(text: str) -> frozenset[str]:
    """Extract the ``ANSWER:`` set from a final message (last match wins)."""
    matches = _ANSWER_RE.findall(text)
    if not matches:
        return frozenset()
    raw = matches[-1].strip()
    if raw.upper() == "NONE" or not raw:
        return frozenset()
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def grade(task: Task, final_text: str) -> Grade:
    answer = parse_answer(final_text)
    if task.kind == "duplicate":
        # Correct = it identified the existing symbol (by qualified name, suffix-tolerant).
        bare = task.expect_name.split(".")[-1]
        hit = any(task.expect_name == a or a.endswith(bare) for a in answer)
        return Grade(correct=hit and bool(answer), precision=1.0 if hit else 0.0,
                     recall=1.0 if hit else 0.0, answer=answer)
    truth = task.truth
    tp = len(answer & truth)
    precision = tp / len(answer) if answer else (1.0 if not truth else 0.0)
    recall = tp / len(truth) if truth else 1.0
    return Grade(correct=(recall == 1.0 and precision == 1.0), precision=precision,
                 recall=recall, answer=answer)


def build_tasks(nodes: Iterable[Node], tools: CartogateTools, *, sample: int = 60) -> list[Task]:
    """Select concrete targets from the indexed corpus and build the task set."""
    top_level = [
        n for n in nodes
        if n.kind is NodeKind.SYMBOL and n.is_top_level and n.signature and "." in n.qualified_name
    ]
    top_level.sort(key=lambda n: n.qualified_name)
    tasks: list[Task] = []

    # 1) Impact: the symbol (among a sample) with the most references.
    best_refs: tuple[int, Node, frozenset[str]] | None = None
    for n in top_level[:sample]:
        refs = tools.find_references(n.qualified_name)["references"]
        names = frozenset(r["qualified_name"] for r in refs)
        if len(names) >= 2 and (best_refs is None or len(names) > best_refs[0]):
            best_refs = (len(names), n, names)
    if best_refs:
        _, node, names = best_refs
        tasks.append(Task(
            id="impact",
            kind="impact",
            prompt=f"Which functions or methods reference `{node.qualified_name}`? "
                   "List their fully-qualified names.",
            truth=names,
        ))

    # 2) Duplicate avoidance: ask to add a function that already exists.
    dup = top_level[0]
    tasks.append(Task(
        id="duplicate",
        kind="duplicate",
        prompt=f"I want to add a new function with the signature `{dup.signature}`. "
               "First check whether an equivalent function already exists in this codebase. "
               "If it does, report its fully-qualified name instead of writing a duplicate.",
        truth=frozenset({dup.qualified_name}),
        expect_name=dup.qualified_name,
    ))

    # 3) Test selection: a symbol that some test exercises.
    for n in top_level[:sample]:
        report = tools.suggest_tests(symbols=[n.qualified_name])
        names = frozenset(t["qualified_name"] for t in report["tests"])
        if names:
            tasks.append(Task(
                id="test_selection",
                kind="test_selection",
                prompt=f"Which test functions exercise `{n.qualified_name}`? "
                       "List their fully-qualified names.",
                truth=names,
            ))
            break

    return tasks
