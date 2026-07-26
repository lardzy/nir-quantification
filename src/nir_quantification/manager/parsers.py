from __future__ import annotations

import csv
import gzip
import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..constants import LABEL_SUM_TARGET, LABEL_SUM_TOLERANCE, FIBER_CLASSES, normalize_fiber_name
from ..parser import parse_csv_file


FOURIER_FILENAME_PATTERN = re.compile(
    r"^样品编号\s+(?P<sample_id>\S+)\s+(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<time>\d{6})\s+GMT[+-]\d{4}$"
)
GRATING_SAMPLE_PATTERN = re.compile(r"SupNIR-3100(?P<sample_id>\d{2}[A-Za-z0-9]\d{6})")
GRATING_DATETIME_PATTERN = re.compile(r"_(?P<date>\d{8})(?P<time>\d{6})$")


@dataclass(slots=True)
class ParsedComponent:
    name: str
    value: float


@dataclass(slots=True)
class ParsedSpectrum:
    file_name: str
    source_path: str
    raw_csv_gzip: bytes
    content_sha256: str
    metadata: dict
    axis_kind: str
    axis_unit: str
    point_count: int
    x_values: list[float]
    y_values: list[float]
    labels: list[ParsedComponent]


class SpectrumParser(Protocol):
    def can_parse(self, path: Path) -> bool: ...

    def parse(self, path: Path) -> ParsedSpectrum: ...


class NIRCsvParser:
    def can_parse(self, path: Path) -> bool:
        if path.suffix.lower() != ".csv":
            return False
        try:
            with path.open("rb") as handle:
                prefix = handle.read(64 * 1024)
            return b"***Scan Data***" in prefix
        except OSError:
            return False

    def parse(self, path: Path) -> ParsedSpectrum:
        raw_bytes, _raw_text = _read_raw_csv(path)
        record, rejection = parse_csv_file(path, require_labels=True)
        if rejection is not None or record is None:
            reason = rejection["details"] if rejection is not None else "unknown parse error"
            raise ValueError(reason)

        labels = [
            ParsedComponent(name=FIBER_CLASSES[index], value=value)
            for index, value in enumerate(record["composition_14"])
            if value > 0
        ]
        metadata = {
            "fabric_id": record["fabric_id"],
            "date": record["date"],
            "time": record["time"],
            "repeat": record["repeat"],
            "device_serial": record["device_serial"],
            "scan_config": record["scan_config"],
            "warnings": record["warnings"],
            "label_source": record["label_source"],
            "acquisition_date": _format_date(record["date"]),
            "acquisition_time": _format_time(record["time"]),
            "source_format": "portable",
            "parser_name": "nir_csv_v1",
        }
        return ParsedSpectrum(
            file_name=path.name,
            source_path=str(path.resolve()),
            raw_csv_gzip=gzip.compress(raw_bytes),
            content_sha256=hashlib.sha256(raw_bytes).hexdigest(),
            metadata=metadata,
            axis_kind="wavelength",
            axis_unit="nm",
            point_count=len(record["raw_wavelengths"]),
            x_values=record["raw_wavelengths"],
            y_values=record["absorbance"],
            labels=labels,
        )


class FourierCsvParser:
    def can_parse(self, path: Path) -> bool:
        if path.suffix.lower() != ".csv":
            return False
        try:
            rows = _read_csv_rows(path)
        except OSError:
            return False
        return _detect_xy_tail_axis_kind(rows) == "wavenumber"

    def parse(self, path: Path) -> ParsedSpectrum:
        raw_bytes, raw_text = _read_raw_csv(path)
        rows = _read_csv_rows_from_text(raw_text)
        x_values, y_values, labels, part_name = _parse_xy_tail_csv(rows, path)
        sample_id, acquisition_date, acquisition_time = _parse_fourier_filename_metadata(path)
        metadata = {
            "sample_id": sample_id,
            "acquisition_date": acquisition_date,
            "acquisition_time": acquisition_time,
            "part_name": part_name,
            "source_format": "fourier",
            "parser_name": "fourier_csv_v1",
        }
        return ParsedSpectrum(
            file_name=path.name,
            source_path=str(path.resolve()),
            raw_csv_gzip=gzip.compress(raw_bytes),
            content_sha256=hashlib.sha256(raw_bytes).hexdigest(),
            metadata=metadata,
            axis_kind="wavenumber",
            axis_unit="cm^-1",
            point_count=len(x_values),
            x_values=x_values,
            y_values=y_values,
            labels=labels,
        )


class GratingCsvParser:
    def can_parse(self, path: Path) -> bool:
        if path.suffix.lower() != ".csv":
            return False
        try:
            rows = _read_csv_rows(path)
        except OSError:
            return False
        return _detect_xy_tail_axis_kind(rows) == "wavelength"

    def parse(self, path: Path) -> ParsedSpectrum:
        raw_bytes, raw_text = _read_raw_csv(path)
        rows = _read_csv_rows_from_text(raw_text)
        x_values, y_values, labels, part_name = _parse_xy_tail_csv(rows, path)
        sample_id, acquisition_date, acquisition_time = _parse_grating_filename_metadata(path)
        metadata = {
            "sample_id": sample_id,
            "acquisition_date": acquisition_date,
            "acquisition_time": acquisition_time,
            "part_name": part_name,
            "source_format": "grating",
            "parser_name": "grating_csv_v1",
        }
        return ParsedSpectrum(
            file_name=path.name,
            source_path=str(path.resolve()),
            raw_csv_gzip=gzip.compress(raw_bytes),
            content_sha256=hashlib.sha256(raw_bytes).hexdigest(),
            metadata=metadata,
            axis_kind="wavelength",
            axis_unit="nm",
            point_count=len(x_values),
            x_values=x_values,
            y_values=y_values,
            labels=labels,
        )


class ParserRegistry:
    def __init__(self, parsers: list[SpectrumParser] | None = None) -> None:
        self.parsers = parsers or [NIRCsvParser(), FourierCsvParser(), GratingCsvParser()]

    def parse(self, path: Path) -> ParsedSpectrum:
        for parser in self.parsers:
            if parser.can_parse(path):
                return parser.parse(path)
        raise ValueError(f"no parser found for {path.name}")


def _read_csv_rows(path: Path) -> list[list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.reader(handle))


def _read_raw_csv(path: Path) -> tuple[bytes, str]:
    raw_bytes = path.read_bytes()
    try:
        raw_text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError(f"{path.name}: CSV must be UTF-8 encoded") from error
    return raw_bytes, raw_text


def _read_csv_rows_from_text(raw_text: str) -> list[list[str]]:
    return list(csv.reader(raw_text.splitlines()))


def _detect_xy_tail_axis_kind(rows: list[list[str]]) -> str | None:
    numeric_x_values = _extract_numeric_x_values(rows)
    if not numeric_x_values:
        return None
    return "wavenumber" if max(numeric_x_values) >= 2500 else "wavelength"


def _extract_numeric_x_values(rows: list[list[str]]) -> list[float]:
    last_non_empty_index = None
    for index in range(len(rows) - 1, -1, -1):
        if any(cell.strip() for cell in rows[index]):
            last_non_empty_index = index
            break
    if last_non_empty_index is None:
        return []

    x_values: list[float] = []
    for row in rows[:last_non_empty_index]:
        if len(row) < 2:
            continue
        first = row[0].strip()
        second = row[1].strip()
        if not first or not second:
            continue
        try:
            x_values.append(float(first))
            float(second)
        except ValueError:
            continue
    return x_values


def _parse_xy_tail_csv(rows: list[list[str]], path: Path) -> tuple[list[float], list[float], list[ParsedComponent], str | None]:
    last_non_empty_index = None
    for index in range(len(rows) - 1, -1, -1):
        if any(cell.strip() for cell in rows[index]):
            last_non_empty_index = index
            break
    if last_non_empty_index is None:
        raise ValueError("file does not contain any non-empty rows")

    label_row = rows[last_non_empty_index]
    labels, part_name = _parse_tail_labels(label_row, path)

    x_values: list[float] = []
    y_values: list[float] = []
    for row in rows[:last_non_empty_index]:
        if len(row) < 2:
            continue
        first = row[0].strip()
        second = row[1].strip()
        if not first or not second:
            continue
        try:
            x_value = float(first)
            y_value = float(second)
        except ValueError:
            continue
        if not math.isfinite(x_value) or not math.isfinite(y_value):
            raise ValueError(f"{path.name}: XY data contains NaN or infinite values")
        x_values.append(x_value)
        y_values.append(y_value)

    if len(x_values) < 2:
        raise ValueError(f"{path.name}: no numeric XY data rows were found")
    ordered_pairs = sorted(zip(x_values, y_values), key=lambda pair: pair[0])
    if any(left[0] >= right[0] for left, right in zip(ordered_pairs, ordered_pairs[1:])):
        raise ValueError(f"{path.name}: X axis contains duplicate or non-monotonic values")
    x_values = [pair[0] for pair in ordered_pairs]
    y_values = [pair[1] for pair in ordered_pairs]
    return x_values, y_values, labels, part_name


def _parse_tail_labels(row: list[str], path: Path) -> tuple[list[ParsedComponent], str | None]:
    if len(row) < 3:
        raise ValueError(f"{path.name}: footer label row is incomplete")

    part_name = row[0].strip() or None
    tokens = [cell.strip() for cell in row[1:] if cell.strip()]
    if not tokens:
        raise ValueError(f"{path.name}: footer label row does not contain any component/value pairs")
    if len(tokens) % 2 != 0:
        raise ValueError(f"{path.name}: footer label row is not component/value pairs")

    merged = Counter()
    for index in range(0, len(tokens), 2):
        raw_name = tokens[index]
        normalized_name = normalize_fiber_name(raw_name)
        if normalized_name is None:
            raise ValueError(f"{path.name}: unknown fiber label {raw_name}")
        try:
            value = float(tokens[index + 1])
        except ValueError as error:
            raise ValueError(f"{path.name}: invalid label value {tokens[index + 1]}") from error
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{path.name}: label values must be finite and positive")
        merged[normalized_name] += value

    if len(merged) < 1 or len(merged) > 4:
        raise ValueError(f"{path.name}: expected 1-4 components, got {len(merged)}")
    total = sum(merged.values())
    if abs(total - LABEL_SUM_TARGET) > LABEL_SUM_TOLERANCE:
        raise ValueError(
            f"{path.name}: label sum {total:.4f} is outside "
            f"{LABEL_SUM_TARGET} +/- {LABEL_SUM_TOLERANCE}"
        )
    scale = LABEL_SUM_TARGET / total
    labels = [
        ParsedComponent(name=name, value=round(value * scale, 8))
        for name, value in merged.items()
    ]
    return labels, part_name


def _parse_fourier_filename_metadata(path: Path) -> tuple[str | None, str | None, str | None]:
    matched = FOURIER_FILENAME_PATTERN.match(path.stem)
    if not matched:
        return None, None, None
    return (
        matched.group("sample_id"),
        matched.group("date"),
        _format_time(matched.group("time")),
    )


def _parse_grating_filename_metadata(path: Path) -> tuple[str | None, str | None, str | None]:
    sample_match = GRATING_SAMPLE_PATTERN.search(path.stem)
    if sample_match is None:
        return None, None, None
    sample_id = sample_match.group("sample_id")
    datetime_match = GRATING_DATETIME_PATTERN.search(path.stem)
    if datetime_match is None:
        return sample_id, None, None
    return (
        sample_id,
        _format_date(datetime_match.group("date")),
        _format_time(datetime_match.group("time")),
    )


def _format_date(value: str | None) -> str | None:
    if not value:
        return None
    if re.fullmatch(r"\d{8}", value):
        return f"{value[:4]}-{value[4:6]}-{value[6:8]}"
    return value


def _format_time(value: str | None) -> str | None:
    if not value:
        return None
    if re.fullmatch(r"\d{6}", value):
        return f"{value[:2]}:{value[2:4]}:{value[4:6]}"
    return value
