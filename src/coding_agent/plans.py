from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

PlanStatus = Literal["pending", "in_progress", "completed"]

PLAN_STATUSES = frozenset({"pending", "in_progress", "completed"})
PLAN_MIN_ITEMS = 1
PLAN_MAX_ITEMS = 20
PLAN_MAX_STEP_CHARS = 200
PLAN_MAX_EXPLANATION_CHARS = 500
PLAN_MAX_IN_PROGRESS = 1


@dataclass(frozen=True)
class PlanItem:
    """One normalized, immutable step in an agent plan."""

    step: str
    status: PlanStatus

    def __post_init__(self) -> None:
        if not isinstance(self.step, str):
            raise TypeError("plan item step must be a string.")
        normalized_step = self.step.strip()
        if not normalized_step:
            raise ValueError("plan item step must be non-empty after trimming.")
        if len(normalized_step) > PLAN_MAX_STEP_CHARS:
            raise ValueError(
                f"plan item step must be at most {PLAN_MAX_STEP_CHARS} characters."
            )
        if self.status not in PLAN_STATUSES:
            raise ValueError(f"Unsupported plan status: {self.status}")
        object.__setattr__(self, "step", normalized_step)


@dataclass(frozen=True)
class PlanState:
    """Latest immutable plan projection rebuilt from session events."""

    explanation: str = ""
    items: tuple[PlanItem, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.explanation, str):
            raise TypeError("plan explanation must be a string.")
        if len(self.explanation) > PLAN_MAX_EXPLANATION_CHARS:
            raise ValueError(
                "plan explanation must be at most "
                f"{PLAN_MAX_EXPLANATION_CHARS} characters."
            )
        if not isinstance(self.items, tuple):
            raise TypeError("plan items must be a tuple.")
        if len(self.items) > PLAN_MAX_ITEMS:
            raise ValueError(f"plan must contain at most {PLAN_MAX_ITEMS} items.")
        if not all(isinstance(item, PlanItem) for item in self.items):
            raise TypeError("plan items must contain PlanItem values.")

        steps = [item.step for item in self.items]
        if len(steps) != len(set(steps)):
            raise ValueError("plan item steps must be unique.")
        in_progress_count = sum(
            item.status == "in_progress" for item in self.items
        )
        if in_progress_count > PLAN_MAX_IN_PROGRESS:
            raise ValueError(
                f"plan may contain at most {PLAN_MAX_IN_PROGRESS} in_progress item."
            )
        object.__setattr__(self, "explanation", self.explanation.strip())

    @property
    def is_completed(self) -> bool:
        return bool(self.items) and all(
            item.status == "completed" for item in self.items
        )


EMPTY_PLAN = PlanState()


def plan_state_to_dict(value: PlanState) -> dict[str, object]:
    if not isinstance(value, PlanState):
        raise TypeError("value must be a PlanState.")
    return {
        "explanation": value.explanation,
        "items": [
            {"step": item.step, "status": item.status}
            for item in value.items
        ],
    }


def plan_state_from_dict(
    data: Mapping[str, object],
    *,
    allow_empty: bool = True,
    explanation_optional: bool = False,
) -> PlanState:
    """Strictly decode a plan from persisted or tool-provided JSON data."""

    if not isinstance(data, Mapping):
        raise TypeError("PlanState must be an object.")
    if not all(isinstance(key, str) for key in data):
        raise TypeError("PlanState keys must be strings.")

    required = {"items"} if explanation_optional else {"explanation", "items"}
    allowed = {"explanation", "items"}
    fields = set(data)
    missing = sorted(required - fields)
    unknown = sorted(fields - allowed)
    if missing:
        raise ValueError(f"PlanState is missing fields: {', '.join(missing)}")
    if unknown:
        raise ValueError(f"PlanState contains unknown fields: {', '.join(unknown)}")

    explanation = data.get("explanation", "")
    if not isinstance(explanation, str):
        raise TypeError("plan explanation must be a string.")
    raw_items = data["items"]
    if not isinstance(raw_items, (list, tuple)):
        raise TypeError("plan items must be an array.")
    if not allow_empty and len(raw_items) < PLAN_MIN_ITEMS:
        raise ValueError(f"plan must contain at least {PLAN_MIN_ITEMS} item.")
    if len(raw_items) > PLAN_MAX_ITEMS:
        raise ValueError(f"plan must contain at most {PLAN_MAX_ITEMS} items.")

    items: list[PlanItem] = []
    for index, raw_item in enumerate(raw_items):
        label = f"plan item {index + 1}"
        if not isinstance(raw_item, Mapping):
            raise TypeError(f"{label} must be an object.")
        if not all(isinstance(key, str) for key in raw_item):
            raise TypeError(f"{label} keys must be strings.")
        item_fields = set(raw_item)
        required_item_fields = {"step", "status"}
        missing_item_fields = sorted(required_item_fields - item_fields)
        unknown_item_fields = sorted(item_fields - required_item_fields)
        if missing_item_fields:
            raise ValueError(
                f"{label} is missing fields: {', '.join(missing_item_fields)}"
            )
        if unknown_item_fields:
            raise ValueError(
                f"{label} contains unknown fields: {', '.join(unknown_item_fields)}"
            )
        step = raw_item["step"]
        status = raw_item["status"]
        if not isinstance(step, str):
            raise TypeError(f"{label} step must be a string.")
        if not isinstance(status, str):
            raise TypeError(f"{label} status must be a string.")
        items.append(PlanItem(step=step, status=cast(PlanStatus, status)))

    return PlanState(explanation=explanation, items=tuple(items))


def parse_plan_update(data: Mapping[str, object]) -> PlanState:
    """Decode the complete-plan arguments accepted by update_plan."""

    return plan_state_from_dict(
        data,
        allow_empty=False,
        explanation_optional=True,
    )


def validate_plan_transition(previous: PlanState, updated: PlanState) -> None:
    """Reject reopening a plan after every item reached completed."""

    if not isinstance(previous, PlanState) or not isinstance(updated, PlanState):
        raise TypeError("plan transitions require PlanState values.")
    if previous.is_completed and not updated.is_completed:
        raise ValueError(
            "A fully completed plan cannot return to pending or in_progress."
        )


__all__ = [
    "EMPTY_PLAN",
    "PLAN_MAX_EXPLANATION_CHARS",
    "PLAN_MAX_IN_PROGRESS",
    "PLAN_MAX_ITEMS",
    "PLAN_MAX_STEP_CHARS",
    "PLAN_MIN_ITEMS",
    "PLAN_STATUSES",
    "PlanItem",
    "PlanState",
    "PlanStatus",
    "parse_plan_update",
    "plan_state_from_dict",
    "plan_state_to_dict",
    "validate_plan_transition",
]
