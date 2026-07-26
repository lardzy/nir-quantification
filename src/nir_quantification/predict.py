from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import torch

from .constants import FIBER_CLASSES, FIXED_GRID_SIZE, FIXED_WAVELENGTHS
from .modeling import InceptionQuantModel
from .parser import parse_csv_file
from .postprocess import postprocess_prediction
from .train import MODEL_BUNDLE_SCHEMA_VERSION


def predict_single_csv(csv_path: str | Path, bundle_path: str | Path) -> dict[str, Any]:
    bundle = torch.load(Path(bundle_path), map_location="cpu", weights_only=True)
    _validate_bundle(bundle)
    record, rejection = parse_csv_file(csv_path, require_labels=False)
    if rejection is not None or record is None:
        reason = rejection["details"] if rejection else "unknown parsing error"
        raise ValueError(f"failed to parse input csv: {reason}")

    model = InceptionQuantModel(fiber_count=len(bundle["fiber_classes"]))
    model.load_state_dict(bundle["model_state_dict"])
    model.eval()

    mean = torch.tensor(bundle["feature_mean"], dtype=torch.float32)
    std = torch.tensor(bundle["feature_std"], dtype=torch.float32)
    features = torch.tensor(record["fixed_absorbance"], dtype=torch.float32)
    features = ((features - mean) / std).unsqueeze(0).unsqueeze(0)

    with torch.no_grad():
        presence_logits, composition_logits = model(features)
        presence_probabilities = torch.sigmoid(presence_logits).squeeze(0).tolist()
        composition_probabilities = torch.softmax(composition_logits, dim=-1).squeeze(0).tolist()

    processed = postprocess_prediction(
        presence_probabilities=presence_probabilities,
        composition_probabilities=composition_probabilities,
        threshold=float(bundle["threshold"]),
    )
    return {
        "file_path": record["file_path"],
        "fabric_id": record["fabric_id"],
        "predicted_components": processed["ranked_components"],
        "threshold": float(bundle["threshold"]),
    }


def _validate_bundle(bundle: Any) -> None:
    if not isinstance(bundle, dict):
        raise ValueError("model bundle must be a dictionary")
    required_keys = {
        "schema_version",
        "model_name",
        "model_state_dict",
        "threshold",
        "feature_mean",
        "feature_std",
        "fiber_classes",
        "fixed_wavelengths",
        "feature_grid_size",
        "preprocessing",
    }
    missing = sorted(required_keys - set(bundle))
    if missing:
        raise ValueError(f"model bundle is missing required keys: {', '.join(missing)}")
    if bundle["schema_version"] != MODEL_BUNDLE_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported model bundle schema {bundle['schema_version']}; "
            f"expected {MODEL_BUNDLE_SCHEMA_VERSION}"
        )
    if bundle["model_name"] != "InceptionQuantModel":
        raise ValueError(f"unsupported model type: {bundle['model_name']}")
    if list(bundle["fiber_classes"]) != FIBER_CLASSES:
        raise ValueError("model bundle fiber class order does not match the current application")
    if int(bundle["feature_grid_size"]) != FIXED_GRID_SIZE:
        raise ValueError("model bundle feature grid size does not match the current application")
    if len(bundle["feature_mean"]) != FIXED_GRID_SIZE or len(bundle["feature_std"]) != FIXED_GRID_SIZE:
        raise ValueError("model bundle feature statistics have an invalid length")
    mean_values = [float(value) for value in bundle["feature_mean"]]
    std_values = [float(value) for value in bundle["feature_std"]]
    if not all(math.isfinite(value) for value in mean_values):
        raise ValueError("model bundle feature means contain NaN or infinite values")
    if not all(math.isfinite(value) and value > 0 for value in std_values):
        raise ValueError("model bundle feature standard deviations must be finite and positive")
    threshold = float(bundle["threshold"])
    if not math.isfinite(threshold) or not 0.0 < threshold < 1.0:
        raise ValueError("model bundle threshold must be between 0 and 1")
    wavelengths = [float(value) for value in bundle["fixed_wavelengths"]]
    if len(wavelengths) != FIXED_GRID_SIZE or any(
        not math.isfinite(left) or abs(left - right) > 1e-6
        for left, right in zip(wavelengths, FIXED_WAVELENGTHS)
    ):
        raise ValueError("model bundle wavelength grid does not match the current application")
    preprocessing = bundle["preprocessing"]
    if preprocessing != {"name": "per_wavelength_standardization", "version": 1}:
        raise ValueError(f"unsupported preprocessing configuration: {preprocessing}")
