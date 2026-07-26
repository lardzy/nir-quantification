from __future__ import annotations

import csv
import hashlib
import json
import random
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import torch
import torch.nn.functional as F
from torch import nn
from torch.utils.data import DataLoader, Dataset

from .constants import FIBER_CLASSES, FIXED_GRID_SIZE, FIXED_WAVELENGTHS
from .metrics import evaluate_split, search_best_threshold
from .modeling import InceptionQuantModel

MODEL_BUNDLE_SCHEMA_VERSION = 1


@dataclass
class TrainingConfig:
    batch_size: int = 64
    learning_rate: float = 1e-3
    weight_decay: float = 1e-4
    max_epochs: int = 80
    early_stopping_patience: int = 12
    seed: int = 42


class ManifestDataset(Dataset):
    def __init__(self, records: list[dict[str, Any]], mean: torch.Tensor, std: torch.Tensor) -> None:
        self.records = records
        self.mean = mean
        self.std = std

    def __len__(self) -> int:
        return len(self.records)

    def __getitem__(self, index: int) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        record = self.records[index]
        features = torch.tensor(record["fixed_absorbance"], dtype=torch.float32)
        features = (features - self.mean) / self.std
        features = features.unsqueeze(0)
        composition = torch.tensor(record["composition_14"], dtype=torch.float32)
        presence = (composition > 0).to(torch.float32)
        ratios = composition / 100.0
        return features, presence, ratios


def train_pipeline(
    manifest_path: str | Path,
    splits_path: str | Path,
    output_dir: str | Path,
    config: TrainingConfig | None = None,
) -> dict[str, Any]:
    config = config or TrainingConfig()
    _seed_everything(config.seed)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    records = _load_jsonl(Path(manifest_path))
    split_definition = _load_json(Path(splits_path))
    split_assignments = _validate_split_definition(records, split_definition)

    train_records = [record for record in records if split_assignments[record["fabric_id"]] == "train"]
    val_records = [record for record in records if split_assignments[record["fabric_id"]] == "val"]
    test_records = [record for record in records if split_assignments[record["fabric_id"]] == "test"]

    mean, std = _compute_feature_stats(train_records)
    pos_weight = _compute_pos_weight(train_records)

    train_dataset = ManifestDataset(train_records, mean, std)
    val_dataset = ManifestDataset(val_records, mean, std)
    test_dataset = ManifestDataset(test_records, mean, std)

    train_loader = DataLoader(train_dataset, batch_size=config.batch_size, shuffle=True)
    train_eval_loader = DataLoader(train_dataset, batch_size=config.batch_size, shuffle=False)
    val_loader = DataLoader(val_dataset, batch_size=config.batch_size, shuffle=False)
    test_loader = DataLoader(test_dataset, batch_size=config.batch_size, shuffle=False)

    model = InceptionQuantModel()
    optimizer = torch.optim.AdamW(model.parameters(), lr=config.learning_rate, weight_decay=config.weight_decay)
    presence_loss = nn.BCEWithLogitsLoss(pos_weight=pos_weight)
    composition_loss = nn.KLDivLoss(reduction="batchmean")

    best_state = None
    best_val_loss = float("inf")
    epochs_without_improvement = 0
    history = []

    for epoch in range(config.max_epochs):
        train_epoch = _run_epoch(model, train_loader, presence_loss, composition_loss, optimizer=optimizer)
        val_epoch = _run_epoch(model, val_loader, presence_loss, composition_loss, optimizer=None)
        history.append(
            {
                "epoch": epoch + 1,
                "train": train_epoch["losses"],
                "val": val_epoch["losses"],
            }
        )
        if val_epoch["losses"]["total_loss"] < best_val_loss:
            best_val_loss = val_epoch["losses"]["total_loss"]
            best_state = deepcopy(model.state_dict())
            epochs_without_improvement = 0
        else:
            epochs_without_improvement += 1
            if epochs_without_improvement >= config.early_stopping_patience:
                break

    if best_state is None:
        raise RuntimeError("training did not produce a valid checkpoint")

    model.load_state_dict(best_state)

    val_eval = _run_epoch(model, val_loader, presence_loss, composition_loss, optimizer=None)
    threshold = search_best_threshold(
        truth_presence=val_eval["truth_presence"],
        predicted_presence_probabilities=val_eval["predicted_presence_probabilities"],
        predicted_composition_probabilities=val_eval["predicted_composition_probabilities"],
    )

    train_eval = _run_epoch(model, train_eval_loader, presence_loss, composition_loss, optimizer=None)
    test_eval = _run_epoch(model, test_loader, presence_loss, composition_loss, optimizer=None)

    train_metrics, train_rows, _ = evaluate_split(
        train_records,
        train_eval["predicted_presence_probabilities"],
        train_eval["predicted_composition_probabilities"],
        threshold,
        "train",
    )
    val_metrics, val_rows, _ = evaluate_split(
        val_records,
        val_eval["predicted_presence_probabilities"],
        val_eval["predicted_composition_probabilities"],
        threshold,
        "val",
    )
    test_metrics, test_rows, class_rows = evaluate_split(
        test_records,
        test_eval["predicted_presence_probabilities"],
        test_eval["predicted_composition_probabilities"],
        threshold,
        "test",
    )

    bundle = {
        "schema_version": MODEL_BUNDLE_SCHEMA_VERSION,
        "model_name": "InceptionQuantModel",
        "model_state_dict": best_state,
        "threshold": threshold,
        "feature_mean": mean.tolist(),
        "feature_std": std.tolist(),
        "fiber_classes": FIBER_CLASSES,
        "fixed_wavelengths": FIXED_WAVELENGTHS,
        "feature_grid_size": FIXED_GRID_SIZE,
        "preprocessing": {
            "name": "per_wavelength_standardization",
            "version": 1,
        },
        "manifest_sha256": _sha256_file(Path(manifest_path)),
        "splits_sha256": _sha256_file(Path(splits_path)),
        "config": asdict(config),
    }
    torch.save(bundle, output_path / "model_bundle.pt")

    metrics_payload = {
        "config": asdict(config),
        "threshold": threshold,
        "history": history,
        "train": train_metrics,
        "val": val_metrics,
        "test": test_metrics,
    }
    _write_json(output_path / "metrics.json", metrics_payload)
    _write_predictions_csv(output_path / "per_sample_predictions.csv", train_rows + val_rows + test_rows)
    _write_class_report_csv(output_path / "class_wise_report.csv", class_rows)

    return {
        "output_dir": str(output_path.resolve()),
        "threshold": threshold,
        "train_metrics": train_metrics,
        "val_metrics": val_metrics,
        "test_metrics": test_metrics,
    }


def _seed_everything(seed: int) -> None:
    random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def _compute_feature_stats(records: list[dict[str, Any]]) -> tuple[torch.Tensor, torch.Tensor]:
    stacked = torch.tensor([record["fixed_absorbance"] for record in records], dtype=torch.float32)
    mean = stacked.mean(dim=0)
    # Population standard deviation remains defined for a one-sample training
    # split; the default unbiased estimator would yield NaN in that case.
    std = stacked.std(dim=0, correction=0)
    std = torch.where(std < 1e-6, torch.full_like(std, 1e-6), std)
    return mean, std


def _compute_pos_weight(records: list[dict[str, Any]]) -> torch.Tensor:
    positives = torch.tensor([record["present_14"] for record in records], dtype=torch.float32).sum(dim=0)
    total = float(len(records))
    negatives = torch.tensor([total] * len(FIBER_CLASSES), dtype=torch.float32) - positives
    weights = []
    for positive, negative in zip(positives.tolist(), negatives.tolist()):
        weights.append(negative / positive if positive > 0 else 1.0)
    return torch.tensor(weights, dtype=torch.float32)


def _run_epoch(
    model: InceptionQuantModel,
    loader: DataLoader,
    presence_loss: nn.Module,
    composition_loss: nn.Module,
    optimizer: torch.optim.Optimizer | None,
) -> dict[str, Any]:
    training = optimizer is not None
    model.train(training)

    total_presence = 0.0
    total_composition = 0.0
    total_loss = 0.0
    total_samples = 0

    truth_presence: list[list[int]] = []
    predicted_presence_probabilities: list[list[float]] = []
    predicted_composition_probabilities: list[list[float]] = []

    for features, presence_targets, ratio_targets in loader:
        batch_size = int(features.shape[0])
        if training:
            optimizer.zero_grad()
        with torch.set_grad_enabled(training):
            presence_logits, composition_logits = model(features)
            batch_presence_loss = presence_loss(presence_logits, presence_targets)
            batch_composition_loss = composition_loss(F.log_softmax(composition_logits, dim=-1), ratio_targets)
            batch_total_loss = 0.4 * batch_presence_loss + 0.6 * batch_composition_loss

            if training:
                batch_total_loss.backward()
                optimizer.step()

        total_presence += float(batch_presence_loss.detach()) * batch_size
        total_composition += float(batch_composition_loss.detach()) * batch_size
        total_loss += float(batch_total_loss.detach()) * batch_size
        total_samples += batch_size

        truth_presence.extend(presence_targets.detach().cpu().int().tolist())
        predicted_presence_probabilities.extend(torch.sigmoid(presence_logits).detach().cpu().tolist())
        predicted_composition_probabilities.extend(torch.softmax(composition_logits, dim=-1).detach().cpu().tolist())

    return {
        "losses": {
            "presence_loss": total_presence / max(total_samples, 1),
            "composition_loss": total_composition / max(total_samples, 1),
            "total_loss": total_loss / max(total_samples, 1),
        },
        "truth_presence": truth_presence,
        "predicted_presence_probabilities": predicted_presence_probabilities,
        "predicted_composition_probabilities": predicted_composition_probabilities,
    }


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _validate_split_definition(
    records: list[dict[str, Any]],
    split_definition: dict[str, Any],
) -> dict[str, str]:
    split_error = split_definition.get("error")
    if split_error:
        raise ValueError(f"split definition is invalid: {split_error}")
    assignments = split_definition.get("assignments")
    if not isinstance(assignments, dict):
        raise ValueError("split definition does not contain an assignments object")
    required_fabric_ids = {str(record["fabric_id"]) for record in records}
    missing = sorted(required_fabric_ids - set(assignments))
    if missing:
        preview = ", ".join(missing[:5])
        raise ValueError(f"split definition is missing {len(missing)} fabric ids: {preview}")
    invalid = sorted(
        fabric_id
        for fabric_id in required_fabric_ids
        if assignments.get(fabric_id) not in {"train", "val", "test"}
    )
    if invalid:
        raise ValueError(f"split definition contains invalid assignments for: {', '.join(invalid[:5])}")
    empty_splits = [
        split_name
        for split_name in ("train", "val", "test")
        if not any(assignments[fabric_id] == split_name for fabric_id in required_fabric_ids)
    ]
    if empty_splits:
        raise ValueError(f"split definition contains empty splits: {', '.join(empty_splits)}")
    return {str(key): str(value) for key, value in assignments.items()}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def _write_predictions_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _write_class_report_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
