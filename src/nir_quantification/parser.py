from __future__ import annotations

import csv
import math
from collections import Counter
from pathlib import Path
from typing import Any

from .constants import (
    FIBER_CLASSES,
    FIBER_TO_INDEX,
    FIXED_GRID_SIZE,
    FIXED_WAVELENGTHS,
    LABEL_SUM_TARGET,
    LABEL_SUM_TOLERANCE,
    normalize_fiber_name,
)


WAVELENGTH_COVERAGE_TOLERANCE = 1.0


def parse_csv_file(path: str | Path, require_labels: bool = True) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    csv_path = Path(path)
    try:
        rows = list(_read_csv_rows(csv_path))
        filename_meta = _parse_filename_metadata(csv_path)
        header_meta = _extract_header_metadata(rows)
        scan_rows, footer_rows, warnings = _extract_scan_and_footer(rows, require_labels=require_labels)
        wavelengths, absorbance, reference_signal, sample_signal = _parse_scan_rows(scan_rows)
        if len(wavelengths) != FIXED_GRID_SIZE:
            return None, _reject(csv_path, "invalid_scan_length", f"expected {FIXED_GRID_SIZE} points, got {len(wavelengths)}")
        if not _is_monotonic_increasing(wavelengths):
            return None, _reject(csv_path, "non_monotonic_wavelengths", "wavelength sequence is not strictly increasing")
        if (
            wavelengths[0] > FIXED_WAVELENGTHS[0] + WAVELENGTH_COVERAGE_TOLERANCE
            or wavelengths[-1] < FIXED_WAVELENGTHS[-1] - WAVELENGTH_COVERAGE_TOLERANCE
        ):
            return None, _reject(
                csv_path,
                "insufficient_wavelength_coverage",
                (
                    f"scan range {wavelengths[0]:.4f}-{wavelengths[-1]:.4f} nm does not cover "
                    f"{FIXED_WAVELENGTHS[0]:.4f}-{FIXED_WAVELENGTHS[-1]:.4f} nm"
                ),
            )

        footer_labels = _parse_footer_labels(footer_rows, csv_path, require_labels=require_labels)
        if footer_labels is None:
            return None, _reject(csv_path, "missing_footer_labels", "footer labels are required for manifest building")

        if footer_labels:
            composition_vector, footer_label_map = _build_composition_vector(footer_labels, csv_path)
        else:
            composition_vector = [0.0] * len(FIBER_CLASSES)
            footer_label_map = {}

        filename_label_map = {}
        if filename_meta["filename_labels"]:
            filename_label_map = _parse_filename_labels(filename_meta["filename_labels"], csv_path)
            if filename_label_map and footer_label_map and not _labels_match(filename_label_map, footer_label_map):
                warnings.append("filename_label_mismatch")

        fixed_absorbance = _linear_interpolate(wavelengths, absorbance, FIXED_WAVELENGTHS)
        present_vector = [1 if value > 0.0 else 0 for value in composition_vector]
        non_zero_indices = [index for index, value in enumerate(composition_vector) if value > 0.0]
        dominant_index = max(range(len(composition_vector)), key=lambda index: composition_vector[index]) if non_zero_indices else None

        record = {
            "file_path": str(csv_path.resolve()),
            "fabric_id": filename_meta["fabric_id"],
            "date": filename_meta["date"],
            "time": filename_meta["time"],
            "repeat": filename_meta["repeat"],
            "device_serial": header_meta.get("device_serial"),
            "scan_config": header_meta.get("scan_config"),
            "raw_wavelengths": wavelengths,
            "absorbance": absorbance,
            "reference_signal": reference_signal,
            "sample_signal": sample_signal,
            "fixed_wavelengths": FIXED_WAVELENGTHS,
            "fixed_absorbance": fixed_absorbance,
            "composition_14": composition_vector,
            "present_14": present_vector,
            "num_components": len(non_zero_indices),
            "dominant_fiber": FIBER_CLASSES[dominant_index] if dominant_index is not None else None,
            "label_source": "footer" if footer_label_map else "none",
            "parse_status": "ok_with_warning" if warnings else ("ok" if footer_label_map else "ok_unlabeled"),
            "warnings": warnings,
        }
        return record, None
    except ParseError as error:
        return None, _reject(csv_path, error.reason, error.details)


class ParseError(Exception):
    def __init__(self, reason: str, details: str) -> None:
        super().__init__(details)
        self.reason = reason
        self.details = details


def _read_csv_rows(path: Path) -> list[list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.reader(handle))


def _parse_filename_metadata(path: Path) -> dict[str, Any]:
    stem = path.stem
    parts = stem.split("_")
    if len(parts) < 4:
        raise ParseError("filename_parse_error", "filename does not contain enough underscore-delimited parts")

    fabric_index = len(parts) - 4
    fabric_id = parts[fabric_index].strip()
    if not fabric_id:
        raise ParseError("filename_parse_error", "fabric_id segment is empty")

    label_segment = "_".join(parts[2:fabric_index]).strip() if fabric_index > 2 else ""
    date = parts[-3].strip() if len(parts) >= 3 else None
    time = parts[-2].strip() if len(parts) >= 2 else None
    repeat = parts[-1].strip() if parts else None

    return {
        "fabric_id": fabric_id,
        "date": date if date else None,
        "time": time if time else None,
        "repeat": int(repeat) if repeat and repeat.isdigit() else None,
        "filename_labels": label_segment,
    }


def _extract_header_metadata(rows: list[list[str]]) -> dict[str, str | None]:
    scan_config = None
    device_serial = None
    for row in rows:
        if not row:
            continue
        if row[0] == "***Scan Data***":
            break
        if row[0].strip() == "Scan Config Name:" and len(row) > 1 and not scan_config:
            scan_config = row[1].strip() or None
        if row[0].strip() == "Serial Number:" and len(row) > 1 and not device_serial:
            device_serial = row[1].strip() or None
    return {"scan_config": scan_config, "device_serial": device_serial}


def _extract_scan_and_footer(rows: list[list[str]], require_labels: bool) -> tuple[list[list[str]], list[list[str]], list[str]]:
    scan_rows: list[list[str]] = []
    footer_rows: list[list[str]] = []
    warnings: list[str] = []
    in_scan = False
    after_scan = False
    found_end = False
    for row in rows:
        if row and row[0] == "***Scan Data***":
            in_scan = True
            after_scan = False
            continue
        if row and row[0] == "***End of Scan Data***":
            in_scan = False
            after_scan = True
            found_end = True
            continue
        if in_scan:
            if row and row[0] == "Wavelength (nm)":
                continue
            scan_rows.append(row)
        elif after_scan:
            footer_rows.append(row)

    if not scan_rows:
        raise ParseError("missing_scan_data", "scan data section was not found")
    if not found_end:
        raise ParseError("missing_end_of_scan", "***End of Scan Data*** marker was not found")
    if require_labels and not any(_row_has_two_values(row) for row in footer_rows):
        raise ParseError("missing_footer_labels", "footer labels are missing")
    return scan_rows, footer_rows, warnings


def _parse_scan_rows(scan_rows: list[list[str]]) -> tuple[list[float], list[float], list[float], list[float]]:
    wavelengths: list[float] = []
    absorbance: list[float] = []
    reference_signal: list[float] = []
    sample_signal: list[float] = []
    for row in scan_rows:
        if len(row) < 4:
            continue
        if not _looks_like_numeric_row(row[:4]):
            continue
        values = [float(cell.strip()) for cell in row[:4]]
        if not all(math.isfinite(value) for value in values):
            raise ParseError("invalid_scan_data", "scan data contains NaN or infinite values")
        wavelengths.append(values[0])
        absorbance.append(values[1])
        reference_signal.append(values[2])
        sample_signal.append(values[3])
    return wavelengths, absorbance, reference_signal, sample_signal


def _parse_footer_labels(footer_rows: list[list[str]], csv_path: Path, require_labels: bool) -> list[tuple[str, float]] | None:
    labels: list[tuple[str, float]] = []
    for row in footer_rows:
        if not _row_has_two_values(row):
            continue
        name = row[0].strip()
        value_text = row[1].strip()
        try:
            value = float(value_text)
        except ValueError as error:
            raise ParseError("invalid_label_value", f"invalid label value in {csv_path.name}: {value_text}") from error
        if not math.isfinite(value) or value <= 0:
            raise ParseError("invalid_label_value", f"label values must be finite and positive: {value_text}")
        labels.append((name, value))
    if not labels:
        return [] if not require_labels else None
    return labels


def _build_composition_vector(labels: list[tuple[str, float]], csv_path: Path) -> tuple[list[float], dict[str, float]]:
    merged = Counter()
    for raw_name, value in labels:
        if not math.isfinite(value) or value <= 0:
            raise ParseError("invalid_label_value", f"label values must be finite and positive: {raw_name}={value}")
        normalized_name = normalize_fiber_name(raw_name)
        if normalized_name is None:
            raise ParseError("unknown_fiber_label", f"unknown fiber label: {raw_name}")
        merged[normalized_name] += value

    num_components = len(merged)
    if num_components < 1 or num_components > 4:
        raise ParseError("invalid_label_count", f"expected 1-4 components, got {num_components}")

    total = sum(merged.values())
    if math.fabs(total - LABEL_SUM_TARGET) > LABEL_SUM_TOLERANCE:
        raise ParseError("invalid_label_sum", f"label sum {total:.4f} is outside {LABEL_SUM_TARGET} +/- {LABEL_SUM_TOLERANCE}")

    scale = LABEL_SUM_TARGET / total if total else 1.0
    normalized_map = {name: round(value * scale, 8) for name, value in merged.items()}
    vector = [0.0] * len(FIBER_CLASSES)
    for name, value in normalized_map.items():
        vector[FIBER_TO_INDEX[name]] = value
    return vector, normalized_map


def _parse_filename_labels(label_segment: str, csv_path: Path) -> dict[str, float]:
    tokens = [token.strip() for token in label_segment.split(",") if token.strip()]
    if not tokens:
        return {}
    if len(tokens) % 2 != 0:
        raise ParseError("filename_label_parse_error", f"filename label segment is not name/value pairs: {csv_path.name}")
    merged = Counter()
    for index in range(0, len(tokens), 2):
        raw_name = tokens[index]
        normalized_name = normalize_fiber_name(raw_name)
        if normalized_name is None:
            return {}
        try:
            value = float(tokens[index + 1])
        except ValueError:
            raise ParseError("filename_label_parse_error", f"filename label value is invalid in {csv_path.name}")
        if not math.isfinite(value) or value <= 0:
            raise ParseError(
                "filename_label_parse_error",
                f"filename label values must be finite and positive in {csv_path.name}",
            )
        merged[normalized_name] += value
    total = sum(merged.values())
    if total <= 0:
        return {}
    scale = LABEL_SUM_TARGET / total
    return {name: round(value * scale, 8) for name, value in merged.items()}


def _labels_match(left: dict[str, float], right: dict[str, float], tolerance: float = 0.5) -> bool:
    if set(left) != set(right):
        return False
    for name in left:
        if math.fabs(left[name] - right[name]) > tolerance:
            return False
    return True


def _linear_interpolate(xs: list[float], ys: list[float], targets: list[float]) -> list[float]:
    if len(xs) != len(ys):
        raise ParseError("invalid_scan_data", "wavelength and absorbance lengths differ")
    if not xs:
        raise ParseError("invalid_scan_data", "scan data is empty")

    result: list[float] = []
    left_index = 0
    for target in targets:
        if target <= xs[0]:
            result.append(ys[0])
            continue
        if target >= xs[-1]:
            result.append(ys[-1])
            continue
        while left_index + 1 < len(xs) and xs[left_index + 1] < target:
            left_index += 1
        x0 = xs[left_index]
        x1 = xs[left_index + 1]
        y0 = ys[left_index]
        y1 = ys[left_index + 1]
        if x1 == x0:
            result.append(y0)
            continue
        ratio = (target - x0) / (x1 - x0)
        result.append(y0 + ratio * (y1 - y0))
    return result


def _is_monotonic_increasing(values: list[float]) -> bool:
    return all(left < right for left, right in zip(values, values[1:]))


def _looks_like_numeric_row(row: list[str]) -> bool:
    try:
        for cell in row:
            float(cell.strip())
        return True
    except ValueError:
        return False


def _row_has_two_values(row: list[str]) -> bool:
    return len(row) >= 2 and row[0].strip() and row[1].strip()


def _reject(path: Path, reason: str, details: str) -> dict[str, Any]:
    return {
        "file_path": str(path.resolve()),
        "parse_status": "rejected",
        "reason": reason,
        "details": details,
    }
