from __future__ import annotations

import unittest

import torch

from nir_quantification.constants import FIBER_CLASSES, FIXED_GRID_SIZE, FIXED_WAVELENGTHS
from nir_quantification.metrics import evaluate_split
from nir_quantification.predict import _validate_bundle
from nir_quantification.train import _compute_feature_stats, _validate_split_definition


class TrainingIntegrityTests(unittest.TestCase):
    def test_rejects_split_files_that_record_a_build_error(self) -> None:
        records = [{"fabric_id": "sample-1"}]
        with self.assertRaisesRegex(ValueError, "split definition is invalid"):
            _validate_split_definition(
                records,
                {"error": "coverage is impossible", "assignments": {}},
            )

    def test_presence_metrics_use_the_served_top_four_decision(self) -> None:
        truth = [1, 1, 1, 1] + [0] * 10
        record = {
            "file_path": "/tmp/sample.csv",
            "fabric_id": "sample",
            "num_components": 4,
            "dominant_fiber": "氨纶",
            "composition_14": [25.0, 25.0, 25.0, 25.0] + [0.0] * 10,
            "present_14": truth,
        }
        presence = [[0.99, 0.98, 0.97, 0.96, 0.95] + [0.01] * 9]
        composition = [[0.2] * 5 + [0.0] * 9]
        metrics, _, class_rows = evaluate_split(
            [record],
            presence,
            composition,
            threshold=0.5,
            split_name="test",
        )
        self.assertEqual(sum(row["support"] for row in class_rows), 4)
        self.assertAlmostEqual(metrics["presence_micro_f1"], 1.0)
        self.assertAlmostEqual(metrics["presence_macro_f1"], 1.0)

    def test_single_training_sample_produces_finite_feature_statistics(self) -> None:
        mean, std = _compute_feature_stats([{"fixed_absorbance": [0.5] * 228}])
        self.assertTrue(torch.isfinite(mean).all())
        self.assertTrue(torch.isfinite(std).all())
        self.assertTrue(torch.all(std > 0))

    def test_metrics_reject_prediction_length_mismatches(self) -> None:
        record = {
            "file_path": "/tmp/sample.csv",
            "fabric_id": "sample",
            "num_components": 1,
            "dominant_fiber": "氨纶",
            "composition_14": [100.0] + [0.0] * 13,
            "present_14": [1] + [0] * 13,
        }
        with self.assertRaisesRegex(ValueError, "sample count"):
            evaluate_split([record], [], [], threshold=0.5, split_name="test")

    def test_model_bundle_rejects_non_finite_wavelengths(self) -> None:
        bundle = {
            "schema_version": 1,
            "model_name": "InceptionQuantModel",
            "model_state_dict": {},
            "threshold": 0.5,
            "feature_mean": [0.0] * FIXED_GRID_SIZE,
            "feature_std": [1.0] * FIXED_GRID_SIZE,
            "fiber_classes": FIBER_CLASSES,
            "fixed_wavelengths": [float("nan"), *FIXED_WAVELENGTHS[1:]],
            "feature_grid_size": FIXED_GRID_SIZE,
            "preprocessing": {"name": "per_wavelength_standardization", "version": 1},
        }
        with self.assertRaisesRegex(ValueError, "wavelength grid"):
            _validate_bundle(bundle)


if __name__ == "__main__":
    unittest.main()
