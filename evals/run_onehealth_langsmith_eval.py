"""Run OneHealth LangSmith evaluations against trajectory dataset examples.

This runner executes the LangGraph agent, feeds each example's resume_sequence,
and returns outputs shaped for evals/onehealth_evaluators.py.

Live runs capture Telegram messages, but can still write Supabase rows and book
NexHealth appointments. The CLI requires --allow-side-effects before evaluate().
"""

from __future__ import annotations

import argparse
import json
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from langsmith import evaluate

import nodes as graph_nodes
from agent import build_graph
from evals.onehealth_evaluators import (
    expected_state_match_evaluator,
    trajectory_match_evaluator,
    user_experience_assertions_evaluator,
)


DEFAULT_DATASET_NAME = "OneHealth Trajectory Dataset"


class RecordingSendMessage:
    def __init__(self, messages: list[dict[str, Any]]):
        self.messages = messages

    def invoke(self, payload: dict[str, Any]) -> dict[str, Any]:
        message = dict(payload)
        self.messages.append(message)
        return {
            "chat_id": str(payload.get("chat_id", "")),
            "outbound_message_content": str(payload.get("text", "")),
            "message_thread_id": None,
        }


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _case_from_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    if "agent_test_case" in inputs:
        return _as_dict(inputs["agent_test_case"])
    if "inputs" in inputs:
        return _case_from_inputs(_as_dict(inputs["inputs"]))
    return dict(inputs)


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
    except TypeError:
        if isinstance(value, dict):
            return {str(key): _json_safe(item) for key, item in value.items()}
        if isinstance(value, list):
            return [_json_safe(item) for item in value]
        return str(value)
    return value


@contextmanager
def _env_overrides(overrides: dict[str, Any]) -> Iterator[None]:
    previous: dict[str, str | None] = {}
    try:
        for key, value in overrides.items():
            previous[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _resume_payload(entry: dict[str, Any]) -> dict[str, Any] | str:
    message = _as_dict(entry.get("message", entry))
    payload: dict[str, Any] = {}
    if "text" in message:
        payload["text"] = message["text"]
    if "location" in message:
        payload["location"] = message["location"]
    if payload:
        return payload
    return str(message)


def _consume_debug_stream(
    graph: Any,
    value: Any,
    config: dict[str, Any],
    trajectory: list[str],
    seen_task_ids: set[str],
    errors: list[str],
) -> tuple[str | None, bool, list[str]]:
    interrupted_at: str | None = None
    finished = False
    appended: list[str] = []

    for event in graph.stream(value, config=config, stream_mode="debug"):
        event_type = event.get("type")
        payload = event.get("payload") or {}

        if event_type == "task":
            task_id = str(payload.get("id"))
            name = payload.get("name")
            if name and name != "__start__" and task_id not in seen_task_ids:
                seen_task_ids.add(task_id)
                trajectory.append(name)
                appended.append(name)

        elif event_type == "task_result":
            name = payload.get("name")
            if payload.get("interrupts"):
                interrupted_at = str(name) if name else None
            if payload.get("error"):
                errors.append(f"{name}: {payload['error']}")

        elif event_type == "checkpoint":
            if payload.get("next") == []:
                finished = True

    return interrupted_at, finished, appended


def run_onehealth_agent(inputs: dict[str, Any]) -> dict[str, Any]:
    """LangSmith run function.

    Input can be either dataset.json style fields or CSV-upload style
    {"agent_test_case": <json>}.
    """

    load_dotenv()
    case = _case_from_inputs(inputs)
    initial_state = _as_dict(case.get("initial_state"))
    resume_sequence = case.get("resume_sequence") or []
    env_overrides = _as_dict(case.get("env_overrides"))
    example_id = case.get("example_id") or inputs.get("example_id") or "example"

    trajectory: list[str] = []
    seen_task_ids: set[str] = set()
    interrupted_nodes: list[str] = []
    messages: list[dict[str, Any]] = []
    errors: list[str] = []
    finished = False
    final_state: dict[str, Any] = {}

    thread_id = f"eval-{example_id}-{uuid.uuid4()}"
    config = {"configurable": {"thread_id": thread_id}}

    original_send_message = graph_nodes.send_message
    graph_nodes.send_message = RecordingSendMessage(messages)
    try:
        with _env_overrides(env_overrides):
            graph = build_graph(InMemorySaver())
            try:
                interrupted_at, finished, _ = _consume_debug_stream(
                    graph,
                    initial_state,
                    config,
                    trajectory,
                    seen_task_ids,
                    errors,
                )
                if interrupted_at:
                    interrupted_nodes.append(interrupted_at)

                for entry in resume_sequence:
                    expected_resume_node = entry.get("node") if isinstance(entry, dict) else None
                    interrupted_at, finished, appended = _consume_debug_stream(
                        graph,
                        Command(resume=_resume_payload(_as_dict(entry))),
                        config,
                        trajectory,
                        seen_task_ids,
                        errors,
                    )

                    if (
                        expected_resume_node
                        and interrupted_at == expected_resume_node
                        and expected_resume_node not in appended
                    ):
                        trajectory.append(str(expected_resume_node))

                    if interrupted_at:
                        interrupted_nodes.append(interrupted_at)

                    if finished:
                        break

                final_state = dict(graph.get_state(config).values)
            except Exception as exc:  # LangSmith should record compareable outputs.
                errors.append(f"{type(exc).__name__}: {exc}")
                try:
                    final_state = dict(graph.get_state(config).values)
                except Exception:
                    final_state = {}
    finally:
        graph_nodes.send_message = original_send_message

    if finished and (not trajectory or trajectory[-1] != "__end__"):
        trajectory.append("__end__")

    return {
        "example_id": example_id,
        "trajectory": trajectory,
        "final_state": _json_safe(final_state),
        "messages": _json_safe(messages),
        "interrupted_nodes": interrupted_nodes,
        "errors": errors,
        "finished": finished,
    }


def _load_local_examples(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text())
    if not isinstance(data, list):
        raise ValueError(f"Expected list of examples in {path}")
    return data


def _local_smoke(path: Path, limit: int) -> None:
    examples = _load_local_examples(path)[:limit]
    for example in examples:
        inputs = {"example_id": example.get("id"), **_as_dict(example.get("inputs"))}
        outputs = run_onehealth_agent(inputs)
        run = {"outputs": outputs}
        result = {
            "trajectory_match": trajectory_match_evaluator(run, example),
            "expected_state_match": expected_state_match_evaluator(run, example),
            "user_experience_assertions": user_experience_assertions_evaluator(run, example),
        }
        print(json.dumps({"id": example.get("id"), "outputs": outputs, "scores": result}, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run OneHealth LangSmith trajectory evaluations.")
    parser.add_argument(
        "--dataset",
        default=os.getenv("ONEHEALTH_LANGSMITH_DATASET", DEFAULT_DATASET_NAME),
        help="LangSmith dataset name or ID.",
    )
    parser.add_argument(
        "--experiment-prefix",
        default="onehealth-trajectory",
        help="LangSmith experiment prefix.",
    )
    parser.add_argument(
        "--local-json",
        type=Path,
        help="Run a local smoke check from dataset.json instead of LangSmith evaluate().",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=1,
        help="Number of local JSON examples to run with --local-json.",
    )
    parser.add_argument(
        "--allow-side-effects",
        action="store_true",
        help="Required. Evaluation captures messages, but may write Supabase data and book appointments.",
    )
    args = parser.parse_args()

    if not args.allow_side_effects:
        raise SystemExit("Refusing live eval without --allow-side-effects.")

    if args.local_json:
        _local_smoke(args.local_json, args.limit)
        return

    evaluate(
        run_onehealth_agent,
        data=args.dataset,
        evaluators=[
            trajectory_match_evaluator,
            expected_state_match_evaluator,
            user_experience_assertions_evaluator,
        ],
        experiment_prefix=args.experiment_prefix,
        max_concurrency=1,
    )


if __name__ == "__main__":
    main()
