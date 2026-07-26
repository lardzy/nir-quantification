from __future__ import annotations

import random
from typing import Any


def build_split_definition(records: list[dict[str, Any]], ratios: tuple[float, float, float] = (0.8, 0.1, 0.1)) -> dict[str, Any]:
    if not records:
        raise ValueError("cannot build split definition from an empty manifest")
    if len(ratios) != 3 or any(ratio <= 0 for ratio in ratios):
        raise ValueError("split ratios must contain three positive values")
    if abs(sum(ratios) - 1.0) > 1e-9:
        raise ValueError(f"split ratios must sum to 1.0, got {sum(ratios):.6f}")

    groups = _group_records(records)
    total_samples = len(records)
    target_counts = {
        "train": int(total_samples * ratios[0]),
        "val": int(total_samples * ratios[1]),
    }
    target_counts["test"] = total_samples - target_counts["train"] - target_counts["val"]

    global_classes = set()
    global_buckets = set()
    for group in groups.values():
        global_classes.update(group["classes"])
        global_buckets.update(group["buckets"])

    _validate_coverage_feasibility(groups, global_classes, global_buckets)

    for seed in range(100):
        assignments, split_counts, split_classes, split_buckets = _assign_groups_with_seed(
            groups=groups,
            target_counts=target_counts,
            global_classes=global_classes,
            global_buckets=global_buckets,
            seed=seed,
        )
        if _is_valid_split(split_classes, split_buckets, global_classes, global_buckets):
            splits = {"train": [], "val": [], "test": []}
            for fabric_id, split_name in assignments.items():
                splits[split_name].append(fabric_id)
            for split_name in splits:
                splits[split_name].sort()
            return {
                "strategy": "group_split_by_fabric_id",
                "seed": seed,
                "ratios": {"train": ratios[0], "val": ratios[1], "test": ratios[2]},
                "sample_counts": split_counts,
                "group_counts": {split_name: len(splits[split_name]) for split_name in splits},
                "assignments": dict(sorted(assignments.items())),
                "splits": splits,
            }

    raise ValueError("failed to find a split in seeds 0..99 that preserves class and bucket coverage")


def _validate_coverage_feasibility(
    groups: dict[str, dict[str, Any]],
    global_classes: set[int],
    global_buckets: set[int],
) -> None:
    required_group_count = 3
    class_group_counts = {
        class_index: sum(class_index in group["classes"] for group in groups.values())
        for class_index in global_classes
    }
    bucket_group_counts = {
        bucket: sum(bucket in group["buckets"] for group in groups.values())
        for bucket in global_buckets
    }
    scarce_classes = {
        class_index: count
        for class_index, count in class_group_counts.items()
        if count < required_group_count
    }
    scarce_buckets = {
        bucket: count
        for bucket, count in bucket_group_counts.items()
        if count < required_group_count
    }
    if scarce_classes or scarce_buckets:
        details = []
        if scarce_classes:
            details.append(
                "classes with fewer than 3 independent fabric groups: "
                + ", ".join(f"{class_index}={count}" for class_index, count in sorted(scarce_classes.items()))
            )
        if scarce_buckets:
            details.append(
                "component buckets with fewer than 3 independent fabric groups: "
                + ", ".join(f"{bucket}={count}" for bucket, count in sorted(scarce_buckets.items()))
            )
        raise ValueError("three-way coverage is impossible; " + "; ".join(details))


def _group_records(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for record in records:
        fabric_id = record["fabric_id"]
        group = groups.setdefault(
            fabric_id,
            {"records": [], "count": 0, "classes": set(), "buckets": set()},
        )
        group["records"].append(record)
        group["count"] += 1
        group["classes"].update(index for index, present in enumerate(record["present_14"]) if present)
        group["buckets"].add(record["num_components"])
    return groups


def _assign_groups_with_seed(
    groups: dict[str, dict[str, Any]],
    target_counts: dict[str, int],
    global_classes: set[int],
    global_buckets: set[int],
    seed: int,
) -> tuple[dict[str, str], dict[str, int], dict[str, set[int]], dict[str, set[int]]]:
    rng = random.Random(seed)
    group_ids = list(groups)
    rng.shuffle(group_ids)

    assignments: dict[str, str] = {}
    split_counts = {"train": 0, "val": 0, "test": 0}
    split_classes = {"train": set(), "val": set(), "test": set()}
    split_buckets = {"train": set(), "val": set(), "test": set()}

    for fabric_id in group_ids:
        group = groups[fabric_id]
        chosen_split = max(
            ("train", "val", "test"),
            key=lambda split_name: _score_assignment(
                split_name=split_name,
                group=group,
                target_counts=target_counts,
                split_counts=split_counts,
                split_classes=split_classes,
                split_buckets=split_buckets,
                global_classes=global_classes,
                global_buckets=global_buckets,
            ),
        )
        assignments[fabric_id] = chosen_split
        split_counts[chosen_split] += group["count"]
        split_classes[chosen_split].update(group["classes"])
        split_buckets[chosen_split].update(group["buckets"])
    return assignments, split_counts, split_classes, split_buckets


def _score_assignment(
    split_name: str,
    group: dict[str, Any],
    target_counts: dict[str, int],
    split_counts: dict[str, int],
    split_classes: dict[str, set[int]],
    split_buckets: dict[str, set[int]],
    global_classes: set[int],
    global_buckets: set[int],
) -> float:
    missing_classes = global_classes - split_classes[split_name]
    missing_buckets = global_buckets - split_buckets[split_name]
    class_gain = len(group["classes"] & missing_classes)
    bucket_gain = len(group["buckets"] & missing_buckets)
    projected_count = split_counts[split_name] + group["count"]
    target_count = target_counts[split_name]
    size_penalty = abs(projected_count - target_count)
    over_target_penalty = max(0, projected_count - target_count)
    split_bias = {"train": 0.0, "val": -0.1, "test": -0.2}[split_name]
    return class_gain * 1000 + bucket_gain * 100 - size_penalty - over_target_penalty * 2 + split_bias


def _is_valid_split(
    split_classes: dict[str, set[int]],
    split_buckets: dict[str, set[int]],
    global_classes: set[int],
    global_buckets: set[int],
) -> bool:
    for split_name in ("train", "val", "test"):
        if not global_classes.issubset(split_classes[split_name]):
            return False
        if not global_buckets.issubset(split_buckets[split_name]):
            return False
    return True
