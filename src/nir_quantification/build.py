from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from .constants import FIBER_CLASSES
from .parser import parse_csv_file
from .splitting import build_split_definition


def build_manifest_bundle(
    input_dir: str | Path,
    manifest_out: str | Path,
    rejections_out: str | Path,
    audit_out: str | Path,
    splits_out: str | Path,
) -> dict[str, Any]:
    input_path = Path(input_dir)
    manifest_path = Path(manifest_out)
    rejections_path = Path(rejections_out)
    audit_path = Path(audit_out)
    splits_path = Path(splits_out)

    records: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    for csv_path in sorted(input_path.rglob("*.csv")):
        record, rejection = parse_csv_file(csv_path, require_labels=True)
        if record is not None:
            records.append(record)
        if rejection is not None:
            rejections.append(rejection)

    accepted_records = [record for record in records if record["parse_status"].startswith("ok")]
    split_error = None
    split_definition: dict[str, Any] | None = None
    try:
        split_definition = build_split_definition(accepted_records)
    except ValueError as error:
        split_error = str(error)

    audit_report = build_audit_report(accepted_records, rejections)
    if split_error is not None:
        audit_report["split_error"] = split_error

    _write_jsonl(manifest_path, accepted_records)
    _write_jsonl(rejections_path, rejections)
    _write_json(audit_path, audit_report)
    _write_json(splits_path, split_definition or {"error": split_error, "assignments": {}, "splits": {"train": [], "val": [], "test": []}})

    return {
        "accepted": len(accepted_records),
        "rejected": len(rejections),
        "split_error": split_error,
        "manifest_path": str(manifest_path.resolve()),
        "rejections_path": str(rejections_path.resolve()),
        "audit_path": str(audit_path.resolve()),
        "splits_path": str(splits_path.resolve()),
    }


def build_audit_report(records: list[dict[str, Any]], rejections: list[dict[str, Any]]) -> dict[str, Any]:
    fiber_positive_counts = {name: 0 for name in FIBER_CLASSES}
    component_bucket_counts: Counter[int] = Counter()
    device_distribution: Counter[str] = Counter()
    warning_counts: Counter[str] = Counter()
    fabric_group_sizes: Counter[str] = Counter()

    for record in records:
        for index, present in enumerate(record["present_14"]):
            if present:
                fiber_positive_counts[FIBER_CLASSES[index]] += 1
        component_bucket_counts[record["num_components"]] += 1
        device_distribution[record.get("device_serial") or "UNKNOWN"] += 1
        fabric_group_sizes[record["fabric_id"]] += 1
        for warning in record.get("warnings", []):
            warning_counts[warning] += 1

    repeated_groups = {fabric_id: count for fabric_id, count in fabric_group_sizes.items() if count > 1}
    rejection_reasons = Counter(rejection["reason"] for rejection in rejections)

    return {
        "summary": {
            "total_accepted": len(records),
            "total_rejected": len(rejections),
            "unique_fabric_ids": len(fabric_group_sizes),
        },
        "fiber_positive_counts": fiber_positive_counts,
        "component_bucket_counts": {str(bucket): count for bucket, count in sorted(component_bucket_counts.items())},
        "device_distribution": dict(device_distribution.most_common()),
        "rejection_reasons": dict(rejection_reasons.most_common()),
        "warning_counts": dict(warning_counts.most_common()),
        "repeat_scan_stats": {
            "repeated_fabric_ids": len(repeated_groups),
            "repeated_samples": sum(repeated_groups.values()),
            "max_samples_per_fabric_id": max(repeated_groups.values(), default=1),
            "fabric_id_distribution": dict(sorted(repeated_groups.items())),
        },
        "rejection_files": [rejection["file_path"] for rejection in rejections],
    }


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
