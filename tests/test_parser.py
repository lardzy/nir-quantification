from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from nir_quantification.parser import parse_csv_file


def make_csv_text(labels: list[tuple[str, float]] | None = None, include_end: bool = True, point_count: int = 228) -> str:
    lines = [
        "***Scan Config Information***,,,,,,,***Reference Scan Information***,,,,,,,,",
        "Scan Config Name:,Hadamard 1,,,,,,Scan Config Name:,Built-in Factory Reference,,,,,,",
        "Serial Number:,SN123,,,,,,,,,,,,,",
        "***Scan Data***,,,,,,,,,,,,,,,",
        "Wavelength (nm),Absorbance (AU),Reference Signal (unitless),Sample Signal (unitless)",
    ]
    for index in range(point_count):
        wavelength = 900.0 + index * (800.0 / (point_count - 1)) if point_count > 1 else 900.0
        absorbance = 0.1 + index * 0.001
        lines.append(f"{wavelength:.6f},{absorbance:.6f},{1000 + index:.1f},{900 + index}")
    if include_end:
        lines.append("***End of Scan Data***")
    if labels:
        for name, value in labels:
            lines.append(f"{name},{value}")
    return "\n".join(lines) + "\n"


class ParserTests(unittest.TestCase):
    def test_parses_valid_labeled_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "ISC_Hadamard 1_聚酯纤维,60.0,腈纶,40.0_ABC123_20240101_120000_1.csv"
            path.write_text(make_csv_text(labels=[("聚酯纤维", 60.0), ("腈纶", 40.0)]), encoding="utf-8")
            record, rejection = parse_csv_file(path, require_labels=True)
            self.assertIsNone(rejection)
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record["fabric_id"], "ABC123")
            self.assertEqual(record["num_components"], 2)
            self.assertEqual(len(record["raw_wavelengths"]), 228)
            self.assertEqual(len(record["fixed_absorbance"]), 228)
            self.assertAlmostEqual(sum(record["composition_14"]), 100.0, places=4)

    def test_merges_duplicate_footer_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "ISC_Hadamard 1_聚酯纤维,60.0_ABC123_20240101_120000_1.csv"
            path.write_text(make_csv_text(labels=[("聚酯纤维", 30.0), ("聚酯纤维", 70.0)]), encoding="utf-8")
            record, rejection = parse_csv_file(path, require_labels=True)
            self.assertIsNone(rejection)
            assert record is not None
            self.assertEqual(record["num_components"], 1)
            self.assertAlmostEqual(sum(record["composition_14"]), 100.0, places=4)

    def test_rejects_unknown_fiber(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "ISC_Hadamard 1_未知纤维,100.0_ABC123_20240101_120000_1.csv"
            path.write_text(make_csv_text(labels=[("未知纤维", 100.0)]), encoding="utf-8")
            record, rejection = parse_csv_file(path, require_labels=True)
            self.assertIsNone(record)
            assert rejection is not None
            self.assertEqual(rejection["reason"], "unknown_fiber_label")

    def test_rejects_bad_label_sum(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "ISC_Hadamard 1_聚酯纤维,70.0,腈纶,20.0_ABC123_20240101_120000_1.csv"
            path.write_text(make_csv_text(labels=[("聚酯纤维", 70.0), ("腈纶", 20.0)]), encoding="utf-8")
            record, rejection = parse_csv_file(path, require_labels=True)
            self.assertIsNone(record)
            assert rejection is not None
            self.assertEqual(rejection["reason"], "invalid_label_sum")

    def test_rejects_missing_end_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "ISC_Hadamard 1_聚酯纤维,100.0_ABC123_20240101_120000_1.csv"
            path.write_text(make_csv_text(labels=[("聚酯纤维", 100.0)], include_end=False), encoding="utf-8")
            record, rejection = parse_csv_file(path, require_labels=True)
            self.assertIsNone(record)
            assert rejection is not None
            self.assertEqual(rejection["reason"], "missing_end_of_scan")

    def test_allows_unlabeled_prediction_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "ISC_Hadamard 1__ABC123_20240101_120000_1.csv"
            path.write_text(make_csv_text(labels=None), encoding="utf-8")
            record, rejection = parse_csv_file(path, require_labels=False)
            self.assertIsNone(rejection)
            assert record is not None
            self.assertEqual(record["parse_status"], "ok_unlabeled")

    def test_rejects_non_finite_scan_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "ISC_Hadamard 1_棉,100.0_ABC123_20240101_120000_1.csv"
            content = make_csv_text(labels=[("棉", 100.0)]).replace(",0.100000,", ",nan,", 1)
            path.write_text(content, encoding="utf-8")
            record, rejection = parse_csv_file(path, require_labels=True)
            self.assertIsNone(record)
            assert rejection is not None
            self.assertEqual(rejection["reason"], "invalid_scan_data")

    def test_rejects_scans_that_do_not_cover_the_model_grid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "ISC_Hadamard 1_棉,100.0_ABC123_20240101_120000_1.csv"
            content = make_csv_text(labels=[("棉", 100.0)])
            content = "\n".join(
                (
                    f"{float(line.split(',', 1)[0]) + 50:.6f},{line.split(',', 1)[1]}"
                    if line and line[0].isdigit() and len(line.split(",", 1)) == 2
                    else line
                )
                for line in content.splitlines()
            )
            path.write_text(content, encoding="utf-8")
            record, rejection = parse_csv_file(path, require_labels=True)
            self.assertIsNone(record)
            assert rejection is not None
            self.assertEqual(rejection["reason"], "insufficient_wavelength_coverage")


if __name__ == "__main__":
    unittest.main()
