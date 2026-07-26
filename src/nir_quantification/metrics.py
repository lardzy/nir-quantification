from __future__ import annotations

import json
import math
from collections import defaultdict
from typing import Any

from .constants import FIBER_CLASSES
from .postprocess import postprocess_prediction


def search_best_threshold(
    truth_presence: list[list[int]],
    predicted_presence_probabilities: list[list[float]],
    predicted_composition_probabilities: list[list[float]] | None = None,
) -> float:
    composition_probabilities = (
        predicted_composition_probabilities
        if predicted_composition_probabilities is not None
        else predicted_presence_probabilities
    )
    _validate_prediction_shapes(
        truth_presence,
        predicted_presence_probabilities,
        composition_probabilities,
    )
    best_threshold = 0.5
    best_micro_f1 = -1.0
    best_macro_f1 = -1.0
    for step in range(5, 96):
        threshold = step / 100.0
        processed_predictions = [
            postprocess_prediction(
                presence_probabilities=presence_row,
                composition_probabilities=composition_row,
                threshold=threshold,
            )
            for presence_row, composition_row in zip(
                predicted_presence_probabilities,
                composition_probabilities,
            )
        ]
        predicted_binary = [
            [1 if index in processed["candidate_indices"] else 0 for index in range(len(FIBER_CLASSES))]
            for processed in processed_predictions
        ]
        micro = micro_f1_score(truth_presence, predicted_binary)
        macro = macro_f1_score(truth_presence, predicted_binary)
        if micro > best_micro_f1 or (math.isclose(micro, best_micro_f1) and macro > best_macro_f1):
            best_threshold = threshold
            best_micro_f1 = micro
            best_macro_f1 = macro
    return best_threshold


def evaluate_split(
    records: list[dict[str, Any]],
    predicted_presence_probabilities: list[list[float]],
    predicted_composition_probabilities: list[list[float]],
    threshold: float,
    split_name: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    truth_presence = [record["present_14"] for record in records]
    _validate_prediction_shapes(
        truth_presence,
        predicted_presence_probabilities,
        predicted_composition_probabilities,
    )
    processed_predictions = [
        postprocess_prediction(
            presence_probabilities=presence_probs,
            composition_probabilities=composition_probs,
            threshold=threshold,
        )
        for presence_probs, composition_probs in zip(
            predicted_presence_probabilities,
            predicted_composition_probabilities,
        )
    ]
    predicted_presence = [
        [1 if index in processed["candidate_indices"] else 0 for index in range(len(FIBER_CLASSES))]
        for processed in processed_predictions
    ]
    per_class_rows = presence_class_report(truth_presence, predicted_presence)

    predicted_dense = []
    sample_rows = []
    for record, processed in zip(records, processed_predictions):
        dense = processed["dense_percentages"]
        predicted_dense.append(dense)
        sample_rows.append(
            {
                "split": split_name,
                "file_path": record["file_path"],
                "fabric_id": record["fabric_id"],
                "num_components": record["num_components"],
                "dominant_true": record["dominant_fiber"],
                "dominant_pred": _dominant_fiber_from_vector(dense),
                "true_components": json.dumps(_vector_to_ranked(record["composition_14"]), ensure_ascii=False),
                "predicted_components": json.dumps(processed["ranked_components"], ensure_ascii=False),
                "true_vector": json.dumps(record["composition_14"], ensure_ascii=False),
                "predicted_vector": json.dumps(dense, ensure_ascii=False),
            }
        )

    overall_metrics = {
        "presence_micro_f1": micro_f1_score(truth_presence, predicted_presence),
        "presence_macro_f1": macro_f1_score(truth_presence, predicted_presence),
        "presence_macro_f1_all_classes": macro_f1_score(
            truth_presence,
            predicted_presence,
            supported_only=False,
        ),
        "overall_mae": mean_absolute_error([record["composition_14"] for record in records], predicted_dense),
        "overall_rmse": root_mean_squared_error([record["composition_14"] for record in records], predicted_dense),
        "mean_sample_l1_error": mean_sample_l1_error(
            [record["composition_14"] for record in records],
            predicted_dense,
        ),
        "mean_sample_total_variation_error": mean_sample_total_variation_error(
            [record["composition_14"] for record in records],
            predicted_dense,
        ),
        "dominant_fiber_accuracy": dominant_fiber_accuracy(records, predicted_dense),
        "bucket_metrics": bucket_metrics(records, predicted_dense),
    }

    per_class_mae_values = per_class_mae([record["composition_14"] for record in records], predicted_dense)
    for row, mae in zip(per_class_rows, per_class_mae_values):
        row["mae"] = mae
        row["split"] = split_name

    return overall_metrics, sample_rows, per_class_rows


def presence_class_report(truth_presence: list[list[int]], predicted_presence: list[list[int]]) -> list[dict[str, Any]]:
    report = []
    class_count = len(FIBER_CLASSES)
    for index in range(class_count):
        tp = fp = fn = support = 0
        for truth_row, predicted_row in zip(truth_presence, predicted_presence):
            truth_value = truth_row[index]
            predicted_value = predicted_row[index]
            support += truth_value
            if truth_value == 1 and predicted_value == 1:
                tp += 1
            elif truth_value == 0 and predicted_value == 1:
                fp += 1
            elif truth_value == 1 and predicted_value == 0:
                fn += 1
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = _f1(precision, recall)
        report.append(
            {
                "fiber": FIBER_CLASSES[index],
                "support": support,
                "precision": precision,
                "recall": recall,
                "f1": f1,
            }
        )
    return report


def micro_f1_score(truth_presence: list[list[int]], predicted_presence: list[list[int]]) -> float:
    tp = fp = fn = 0
    for truth_row, predicted_row in zip(truth_presence, predicted_presence):
        for truth_value, predicted_value in zip(truth_row, predicted_row):
            if truth_value == 1 and predicted_value == 1:
                tp += 1
            elif truth_value == 0 and predicted_value == 1:
                fp += 1
            elif truth_value == 1 and predicted_value == 0:
                fn += 1
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    return _f1(precision, recall)


def macro_f1_score(
    truth_presence: list[list[int]],
    predicted_presence: list[list[int]],
    *,
    supported_only: bool = True,
) -> float:
    rows = presence_class_report(truth_presence, predicted_presence)
    if supported_only:
        rows = [row for row in rows if row["support"] > 0]
    return sum(row["f1"] for row in rows) / len(rows) if rows else 0.0


def mean_absolute_error(truth_vectors: list[list[float]], predicted_vectors: list[list[float]]) -> float:
    total = 0.0
    count = 0
    for truth_row, predicted_row in zip(truth_vectors, predicted_vectors):
        for truth_value, predicted_value in zip(truth_row, predicted_row):
            total += abs(truth_value - predicted_value)
            count += 1
    return total / count if count else 0.0


def root_mean_squared_error(truth_vectors: list[list[float]], predicted_vectors: list[list[float]]) -> float:
    total = 0.0
    count = 0
    for truth_row, predicted_row in zip(truth_vectors, predicted_vectors):
        for truth_value, predicted_value in zip(truth_row, predicted_row):
            total += (truth_value - predicted_value) ** 2
            count += 1
    return math.sqrt(total / count) if count else 0.0


def mean_sample_l1_error(truth_vectors: list[list[float]], predicted_vectors: list[list[float]]) -> float:
    sample_errors = [
        sum(abs(truth_value - predicted_value) for truth_value, predicted_value in zip(truth_row, predicted_row))
        for truth_row, predicted_row in zip(truth_vectors, predicted_vectors)
    ]
    return sum(sample_errors) / len(sample_errors) if sample_errors else 0.0


def mean_sample_total_variation_error(
    truth_vectors: list[list[float]],
    predicted_vectors: list[list[float]],
) -> float:
    return mean_sample_l1_error(truth_vectors, predicted_vectors) / 2.0


def per_class_mae(truth_vectors: list[list[float]], predicted_vectors: list[list[float]]) -> list[float]:
    values = []
    for index in range(len(FIBER_CLASSES)):
        total = 0.0
        count = 0
        for truth_row, predicted_row in zip(truth_vectors, predicted_vectors):
            total += abs(truth_row[index] - predicted_row[index])
            count += 1
        values.append(total / count if count else 0.0)
    return values


def dominant_fiber_accuracy(records: list[dict[str, Any]], predicted_vectors: list[list[float]]) -> float:
    correct = 0
    total = 0
    for record, predicted_row in zip(records, predicted_vectors):
        if record["dominant_fiber"] is None:
            continue
        predicted_dominant = _dominant_fiber_from_vector(predicted_row)
        if predicted_dominant == record["dominant_fiber"]:
            correct += 1
        total += 1
    return correct / total if total else 0.0


def bucket_metrics(records: list[dict[str, Any]], predicted_vectors: list[list[float]]) -> dict[str, dict[str, float]]:
    grouped_truth: dict[int, list[list[float]]] = defaultdict(list)
    grouped_pred: dict[int, list[list[float]]] = defaultdict(list)
    for record, predicted_row in zip(records, predicted_vectors):
        grouped_truth[record["num_components"]].append(record["composition_14"])
        grouped_pred[record["num_components"]].append(predicted_row)
    summary: dict[str, dict[str, float]] = {}
    for bucket in sorted(grouped_truth):
        summary[str(bucket)] = {
            "mae": mean_absolute_error(grouped_truth[bucket], grouped_pred[bucket]),
            "rmse": root_mean_squared_error(grouped_truth[bucket], grouped_pred[bucket]),
        }
    return summary


def _f1(precision: float, recall: float) -> float:
    return 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0


def _dominant_fiber_from_vector(vector: list[float]) -> str | None:
    if not vector:
        return None
    if max(vector) <= 0:
        return None
    index = max(range(len(vector)), key=lambda item: vector[item])
    return FIBER_CLASSES[index]


def _vector_to_ranked(vector: list[float]) -> list[dict[str, float]]:
    ranked = [
        {"fiber": FIBER_CLASSES[index], "percentage": value}
        for index, value in enumerate(vector)
        if value > 0
    ]
    ranked.sort(key=lambda item: item["percentage"], reverse=True)
    return ranked


def _validate_prediction_shapes(
    truth_presence: list[list[int]],
    predicted_presence_probabilities: list[list[float]],
    predicted_composition_probabilities: list[list[float]],
) -> None:
    sample_count = len(truth_presence)
    if (
        len(predicted_presence_probabilities) != sample_count
        or len(predicted_composition_probabilities) != sample_count
    ):
        raise ValueError("prediction sample count does not match the truth sample count")
    expected_width = len(FIBER_CLASSES)
    for name, rows in (
        ("truth", truth_presence),
        ("presence", predicted_presence_probabilities),
        ("composition", predicted_composition_probabilities),
    ):
        if any(len(row) != expected_width for row in rows):
            raise ValueError(f"{name} vectors must contain exactly {expected_width} classes")
