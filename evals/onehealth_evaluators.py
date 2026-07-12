"""Deterministic LangSmith evaluators for OneHealth trajectory datasets.

These functions are intentionally stdlib-only so they can be uploaded as
LangSmith custom code evaluators. Each evaluator returns exactly one metric.
"""

from __future__ import annotations

import json
from typing import Any


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


def _get_outputs(run: Any) -> dict[str, Any]:
    outputs = run.outputs if hasattr(run, "outputs") else run.get("outputs", {})
    return _as_dict(outputs)


def _get_example_outputs(example: Any) -> dict[str, Any]:
    outputs = example.outputs if hasattr(example, "outputs") else example.get("outputs", {})
    outputs = _as_dict(outputs)
    return _as_dict(outputs.get("expected_result", outputs))


def _normalize_trajectory(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    return value if isinstance(value, list) else []


def _message_text(outputs: dict[str, Any]) -> str:
    messages = _as_list(outputs.get("messages"))
    parts = []
    for message in messages:
        if isinstance(message, dict):
            parts.append(str(message.get("text", "")))
    return "\n".join(parts).lower()


def _has_keyboard(outputs: dict[str, Any]) -> bool:
    messages = _as_list(outputs.get("messages"))
    return any(isinstance(message, dict) and bool(message.get("keyboard")) for message in messages)


def _trajectory_variants(trajectory: list[str]) -> list[list[str]]:
    variants = [trajectory]
    if trajectory and trajectory[-1] == "__end__":
        variants.append(trajectory[:-1])
    else:
        variants.append([*trajectory, "__end__"])
    return variants


def _path_join(prefix: str, key: str) -> str:
    return f"{prefix}.{key}" if prefix else key


def _subset_mismatches(expected: Any, actual: Any, prefix: str = "") -> list[str]:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [f"{prefix or '<root>'}: expected object, got {type(actual).__name__}"]

        mismatches: list[str] = []
        for key, expected_value in expected.items():
            path = _path_join(prefix, str(key))
            if key not in actual:
                mismatches.append(f"{path}: missing")
                continue
            mismatches.extend(_subset_mismatches(expected_value, actual[key], path))
        return mismatches

    if isinstance(expected, list):
        if actual != expected:
            return [f"{prefix or '<root>'}: expected {expected!r}, got {actual!r}"]
        return []

    if actual != expected:
        return [f"{prefix or '<root>'}: expected {expected!r}, got {actual!r}"]
    return []


def trajectory_match_evaluator(run: Any, example: Any) -> dict[str, Any]:
    """Score 1 when actual node trajectory matches expected trajectory.

    `__end__` is treated as optional because some dataset rows include it and
    some successful booking rows stop at `book_appointment`.
    """

    run_outputs = _get_outputs(run)
    example_outputs = _get_example_outputs(example)
    actual = _normalize_trajectory(run_outputs.get("trajectory"))
    expected = _normalize_trajectory(example_outputs.get("expected_trajectory"))

    matched = actual in _trajectory_variants(expected) or expected in _trajectory_variants(actual)
    if matched:
        return {"score": 1, "comment": "Trajectory matched expected node order."}

    return {
        "score": 0,
        "comment": f"Expected {expected}, got {actual}.",
    }


def expected_state_match_evaluator(run: Any, example: Any) -> dict[str, Any]:
    """Score 1 when expected state fields are present in final agent state."""

    run_outputs = _get_outputs(run)
    example_outputs = _get_example_outputs(example)
    expected_state = _as_dict(example_outputs.get("expected_state"))
    actual_state = _as_dict(run_outputs.get("final_state"))

    mismatches = _subset_mismatches(expected_state, actual_state)
    if not mismatches:
        return {"score": 1, "comment": "Expected state fields matched final state."}

    preview = "; ".join(mismatches[:8])
    remaining = len(mismatches) - 8
    if remaining > 0:
        preview = f"{preview}; {remaining} more mismatch(es)"
    return {"score": 0, "comment": preview}


def user_experience_assertions_evaluator(run: Any, example: Any) -> dict[str, Any]:
    """Score UX assertions declared in expected_result.ux_assertions."""

    run_outputs = _get_outputs(run)
    example_outputs = _get_example_outputs(example)
    assertions = _as_dict(example_outputs.get("ux_assertions"))
    if not assertions:
        return {"score": 1, "comment": "No UX assertions requested."}

    text = _message_text(run_outputs)
    trajectory = _normalize_trajectory(run_outputs.get("trajectory"))
    final_state = _as_dict(run_outputs.get("final_state"))

    checks = {
        "no_slot_recovery": (
            "try another date" in text
            and "different provider" in text
            and "book_appointment" not in trajectory
        ),
        "privacy_copy": (
            "before i can book" in text
            or "i will store only the fields you confirm" in text
        ),
        "profile_privacy_copy": "i will store only the fields you confirm" in text,
        "cancel_flow": (
            final_state.get("conversation_status") == "cancelled"
            and "book_appointment" not in trajectory
            and "store_in_supabase" not in trajectory
        ),
        "invalid_choice_retry": "i did not recognize" in text,
        "telegram_buttons": _has_keyboard(run_outputs),
        "view_lists_appointments": (
            "view_appointments" in trajectory
            and (
                bool(final_state.get("viewed_appointments"))
                or "upcoming appointment" in text
            )
        ),
        "booking_deduplicated": (
            "book_appointment" in trajectory
            and bool(final_state.get("appointment_booking_status"))
            and not final_state.get("book_appointment_result")
        ),
    }

    failures = [
        key for key, expected in assertions.items()
        if expected is True and not checks.get(key, False)
    ]
    if not failures:
        return {"score": 1, "comment": "UX assertions passed."}

    return {
        "score": 0,
        "comment": "Failed UX assertions: " + ", ".join(failures),
    }
