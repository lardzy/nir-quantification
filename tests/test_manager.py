from __future__ import annotations

import tempfile
import time
import unittest
import gzip
from pathlib import Path

from fastapi.testclient import TestClient

from nir_quantification.manager.app import create_app
from nir_quantification.manager.config import ManagerSettings
from nir_quantification.manager.db import session_scope
from nir_quantification.manager.jobs import SubsetStore
from nir_quantification.manager.models import ClassAxisStat, ClassStat, Job, Spectrum
from nir_quantification.manager.service import transition_spectrum_exclusion


def make_csv_text(labels: list[tuple[str, float]], point_count: int = 228) -> str:
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
    lines.append("***End of Scan Data***")
    for name, value in labels:
        lines.append(f"{name},{value}")
    return "\n".join(lines) + "\n"


def make_fourier_csv_text(labels: list[tuple[str, float]], part_name: str = "") -> str:
    lines = []
    for index in range(1557):
        wavenumber = 3999.64 + index * 3.857
        absorbance = 0.2 + index * 0.0001
        lines.append(f"{wavenumber:.3f},{absorbance:.7f},,,")
    footer = [part_name] if part_name else [""]
    for name, value in labels:
        footer.extend([name, str(value)])
    lines.append(",".join(footer))
    return "\n".join(lines) + "\n"


def make_grating_csv_text(labels: list[tuple[str, float]], part_name: str = "") -> str:
    lines = []
    for wavelength in range(1000, 1800):
        absorbance = 0.08 + (wavelength - 1000) * 0.00015
        lines.append(f"{wavelength},{absorbance:.15f},,,,,")
    footer = [part_name] if part_name else [""]
    for name, value in labels:
        footer.extend([name, str(value)])
    lines.append(",".join(footer))
    return "\n".join(lines) + "\n"


class ManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name)
        self.import_root = self.base_dir / "imports"
        self.export_root = self.base_dir / "exports"
        self.import_root.mkdir()
        self.export_root.mkdir()

        self.settings = ManagerSettings(
            db_path=self.base_dir / "data" / "spectra.sqlite3",
            import_roots=[self.import_root],
            export_roots=[self.export_root],
            static_dir=None,
            max_workers=2,
            job_batch_size=2,
        )
        self.client = TestClient(create_app(self.settings))

    def tearDown(self) -> None:
        self.client.close()
        self.temp_dir.cleanup()

    def test_import_classification_subset_and_exclusion_flow(self) -> None:
        nested_a = self.import_root / "batch-a"
        nested_b = self.import_root / "batch-b"
        nested_c = self.import_root / "batch-c"
        nested_d = self.import_root / "batch-d"
        nested_a.mkdir()
        nested_b.mkdir()
        nested_c.mkdir()
        nested_d.mkdir()

        file_name_a = "ISC_Hadamard 1_棉,70.0,锦纶,30.0_ABC123_20240101_120000_1.csv"
        file_name_b = "ISC_Hadamard 1_锦纶,20.0,棉,80.0_DEF456_20240101_120100_1.csv"
        file_name_c = "ISC_Hadamard 1_棉,65.0,锦纶,35.0_GHI789_20240101_120200_1.csv"
        file_name_d = "ISC_Hadamard 1_锦纶,10.0,棉,90.0_JKL012_20240101_120300_1.csv"
        (nested_a / file_name_a).write_text(make_csv_text([("棉", 70.0), ("锦纶", 30.0)]), encoding="utf-8")
        (nested_b / file_name_b).write_text(make_csv_text([("锦纶", 20.0), ("棉", 80.0)]), encoding="utf-8")
        (nested_c / file_name_c).write_text(make_csv_text([("棉", 65.0), ("锦纶", 35.0)]), encoding="utf-8")
        (nested_d / file_name_d).write_text(make_csv_text([("锦纶", 10.0), ("棉", 90.0)]), encoding="utf-8")
        duplicate_dir = nested_b / "duplicates"
        duplicate_dir.mkdir()
        (duplicate_dir / file_name_a).write_text(make_csv_text([("棉", 70.0), ("锦纶", 30.0)]), encoding="utf-8")

        job = self.client.post("/api/import-jobs", json={"root_path": str(self.import_root), "recursive": True}).json()
        completed = self._wait_for_job(job["id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["total_discovered"], 5)
        self.assertEqual(completed["imported_count"], 4)
        self.assertEqual(completed["skipped_count"], 1)
        self.assertEqual(completed["failed_count"], 0)

        browse = self.client.get("/api/fs/browse", params={"kind": "import", "path": str(nested_b)}).json()
        self.assertEqual(browse["parent_path"], str(self.import_root.resolve()))

        classes_payload = self.client.get("/api/classes", params={"sort": "name"}).json()
        self.assertEqual(classes_payload["meta"]["status"], "ready")
        classes = classes_payload["items"]
        self.assertEqual(len(classes), 1)
        self.assertEqual(classes[0]["class_key"], "棉|锦纶")
        self.assertEqual(classes[0]["total_count"], 4)

        class_key = classes[0]["class_key"]
        spectra_payload = self.client.get(
            "/api/spectra",
            params={"class_key": class_key, "excluded": "active", "limit": 1},
        ).json()
        self.assertEqual(spectra_payload["count"], 4)
        self.assertEqual(len(spectra_payload["items"]), 1)

        subset_payload = self.client.post(
            f"/api/classes/{class_key}/subsets",
            json={"mode": "count", "parts": 2},
        ).json()
        self.assertEqual(len(subset_payload["subsets"]), 2)
        subset_counts = [item["count"] for item in subset_payload["subsets"]]
        self.assertEqual(subset_counts, [2, 2])

        ratio_subset_payload = self.client.post(
            f"/api/classes/{class_key}/subsets",
            json={"mode": "ratio", "parts": 2},
        ).json()
        self.assertEqual(len(ratio_subset_payload["subsets"]), 2)
        ratio_subset_ids = [item["subset_id"] for item in ratio_subset_payload["subsets"]]
        ratio_subset_counts = [item["count"] for item in ratio_subset_payload["subsets"]]
        self.assertEqual(ratio_subset_counts, [2, 2])

        subset_spectra = self.client.get(
            "/api/spectra",
            params={"class_key": class_key, "excluded": "active", "subset_id": ratio_subset_ids[0], "limit": 2000},
        ).json()
        self.assertEqual(subset_spectra["count"], 2)
        self.assertEqual(len(subset_spectra["items"]), 2)

        summary_payload = self.client.get(
            "/api/spectra/summary",
            params={"class_key": class_key, "excluded": "active", "subset_id": ratio_subset_ids[0]},
        ).json()
        self.assertEqual(summary_payload["status"], "ready")
        self.assertEqual(summary_payload["source"], "subset-cache")
        self.assertEqual(summary_payload["total_count"], 2)
        self.assertEqual(summary_payload["axis_summary"][0]["count"], 2)

        spectrum_id = subset_spectra["items"][0]["id"]
        excluded = self.client.post(f"/api/spectra/{spectrum_id}/exclude").json()
        self.assertTrue(excluded["is_excluded"])

        classes_after_exclude = self.client.get("/api/classes", params={"sort": "name"}).json()["items"]
        self.assertEqual(classes_after_exclude[0]["active_count"], 3)
        self.assertEqual(classes_after_exclude[0]["excluded_count"], 1)

        recent_excluded = self.client.get("/api/excluded/recent", params={"limit": 10}).json()["items"]
        self.assertEqual(recent_excluded[0]["id"], spectrum_id)

        restored = self.client.post(f"/api/spectra/{spectrum_id}/restore").json()
        self.assertFalse(restored["is_excluded"])
        classes_after_restore = self.client.get("/api/classes", params={"sort": "name"}).json()["items"]
        self.assertEqual(classes_after_restore[0]["active_count"], 4)
        self.assertEqual(classes_after_restore[0]["excluded_count"], 0)

    def test_exclude_restore_are_idempotent_and_self_heal_missing_cache_rows(self) -> None:
        file_name = "ISC_Hadamard 1_聚酯纤维,100.0_ABC123_20240101_120000_1.csv"
        (self.import_root / file_name).write_text(make_csv_text([("聚酯纤维", 100.0)]), encoding="utf-8")

        job = self.client.post("/api/import-jobs", json={"root_path": str(self.import_root), "recursive": True}).json()
        self._wait_for_job(job["id"])

        spectra_payload = self.client.get(
            "/api/spectra",
            params={"class_key": "聚酯纤维", "excluded": "active", "limit": 2000},
        ).json()
        spectrum_id = spectra_payload["items"][0]["id"]

        with session_scope(self.client.app.state.session_factory) as session:
            session.query(ClassStat).delete()
            session.query(ClassAxisStat).delete()

        first_exclude = self.client.post(f"/api/spectra/{spectrum_id}/exclude").json()
        second_exclude = self.client.post(f"/api/spectra/{spectrum_id}/exclude").json()
        self.assertTrue(first_exclude["is_excluded"])
        self.assertTrue(second_exclude["is_excluded"])

        classes_after_exclude = self.client.get("/api/classes", params={"sort": "name"}).json()["items"]
        self.assertEqual(classes_after_exclude[0]["active_count"], 0)
        self.assertEqual(classes_after_exclude[0]["excluded_count"], 1)
        with session_scope(self.client.app.state.session_factory) as session:
            self.assertEqual(session.query(ClassStat).count(), 1)
            self.assertEqual(session.query(ClassAxisStat).count(), 1)

        first_restore = self.client.post(f"/api/spectra/{spectrum_id}/restore").json()
        second_restore = self.client.post(f"/api/spectra/{spectrum_id}/restore").json()
        self.assertFalse(first_restore["is_excluded"])
        self.assertFalse(second_restore["is_excluded"])

        classes_after_restore = self.client.get("/api/classes", params={"sort": "name"}).json()["items"]
        self.assertEqual(classes_after_restore[0]["active_count"], 1)
        self.assertEqual(classes_after_restore[0]["excluded_count"], 0)

    def test_reimport_skips_existing_file_names(self) -> None:
        file_name = "ISC_Hadamard 1_聚酯纤维,100.0_ABC123_20240101_120000_1.csv"
        (self.import_root / file_name).write_text(make_csv_text([("聚酯纤维", 100.0)]), encoding="utf-8")

        first_job = self.client.post("/api/import-jobs", json={"root_path": str(self.import_root), "recursive": True}).json()
        first_completed = self._wait_for_job(first_job["id"])
        self.assertEqual(first_completed["imported_count"], 1)
        self.assertEqual(first_completed["skipped_count"], 0)

        second_job = self.client.post("/api/import-jobs", json={"root_path": str(self.import_root), "recursive": True}).json()
        second_completed = self._wait_for_job(second_job["id"])
        self.assertEqual(second_completed["imported_count"], 0)
        self.assertEqual(second_completed["skipped_count"], 1)
        self.assertEqual(second_completed["failed_count"], 0)

    def test_export_preserves_original_csv_text(self) -> None:
        file_name = "ISC_Hadamard 1_氨纶,40.0,棉,60.0_ABC123_20240101_120000_1.csv"
        original_text = make_csv_text([("氨纶", 40.0), ("棉", 60.0)])
        (self.import_root / file_name).write_text(original_text, encoding="utf-8")

        import_job = self.client.post("/api/import-jobs", json={"root_path": str(self.import_root), "recursive": True}).json()
        self._wait_for_job(import_job["id"])

        export_job = self.client.post(
            "/api/export-jobs",
            json={"export_root": str(self.export_root), "scope": "active", "class_keys": []},
        ).json()
        completed = self._wait_for_job(export_job["id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["imported_count"], 1)
        self.assertEqual(completed["failed_count"], 0)

        exported_candidates = list((self.export_root / "active").rglob(file_name))
        self.assertEqual(len(exported_candidates), 1)
        exported_path = exported_candidates[0]
        self.assertTrue(exported_path.exists())
        self.assertEqual(exported_path.read_text(encoding="utf-8"), original_text)

    def test_export_preserves_original_csv_bytes(self) -> None:
        file_name = "ISC_Hadamard 1_棉,100.0_ABC123_20240101_120000_1.csv"
        original_bytes = ("\ufeff" + make_csv_text([("棉", 100.0)]).replace("\n", "\r\n")).encode("utf-8")
        (self.import_root / file_name).write_bytes(original_bytes)

        import_job = self.client.post(
            "/api/import-jobs",
            json={"root_path": str(self.import_root), "recursive": True},
        ).json()
        self._wait_for_job(import_job["id"])
        export_job = self.client.post(
            "/api/export-jobs",
            json={"export_root": str(self.export_root), "scope": "active", "class_keys": []},
        ).json()
        self._wait_for_job(export_job["id"])

        exported_path = next((self.export_root / "active").rglob(file_name))
        self.assertEqual(exported_path.read_bytes(), original_bytes)
        with session_scope(self.client.app.state.session_factory) as session:
            stored = session.query(Spectrum).filter_by(file_name=file_name).one()
            self.assertEqual(gzip.decompress(stored.raw_csv_gzip), original_bytes)
            self.assertIsNotNone(stored.content_sha256)

    def test_same_file_name_with_different_content_is_not_silently_accepted(self) -> None:
        file_name = "ISC_Hadamard 1_棉,100.0_ABC123_20240101_120000_1.csv"
        path = self.import_root / file_name
        path.write_text(make_csv_text([("棉", 100.0)]), encoding="utf-8")
        first_job = self.client.post(
            "/api/import-jobs",
            json={"root_path": str(self.import_root), "recursive": True},
        ).json()
        self._wait_for_job(first_job["id"])

        path.write_text(
            make_csv_text([("棉", 100.0)]).replace("0.100000", "0.900000", 1),
            encoding="utf-8",
        )
        second_job = self.client.post(
            "/api/import-jobs",
            json={"root_path": str(self.import_root), "recursive": True},
        ).json()
        completed = self._wait_for_job(second_job["id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["failed_count"], 1)
        self.assertIn("file name conflict", completed["log_text"])

    def test_stale_concurrent_exclusion_only_adjusts_counters_once(self) -> None:
        file_name = "ISC_Hadamard 1_棉,100.0_ABC123_20240101_120000_1.csv"
        (self.import_root / file_name).write_text(make_csv_text([("棉", 100.0)]), encoding="utf-8")
        job = self.client.post(
            "/api/import-jobs",
            json={"root_path": str(self.import_root), "recursive": True},
        ).json()
        self._wait_for_job(job["id"])

        session_factory = self.client.app.state.session_factory
        first_session = session_factory()
        second_session = session_factory()
        try:
            spectrum_id = first_session.query(Spectrum.id).scalar()
            assert spectrum_id is not None
            first_session.get(Spectrum, spectrum_id)
            second_session.get(Spectrum, spectrum_id)

            first_spectrum, first_changed = transition_spectrum_exclusion(
                first_session,
                spectrum_id,
                excluded=True,
            )
            first_session.commit()
            second_spectrum, second_changed = transition_spectrum_exclusion(
                second_session,
                spectrum_id,
                excluded=True,
            )
            second_session.commit()

            self.assertTrue(first_changed)
            self.assertFalse(second_changed)
            assert first_spectrum is not None and second_spectrum is not None
            self.assertTrue(first_spectrum.is_excluded)
            self.assertTrue(second_spectrum.is_excluded)
        finally:
            first_session.close()
            second_session.close()

        with session_scope(session_factory) as session:
            class_stat = session.get(ClassStat, "棉")
            assert class_stat is not None
            self.assertEqual(class_stat.total_count, 1)
            self.assertEqual(class_stat.active_count, 0)
            self.assertEqual(class_stat.excluded_count, 1)

    def test_spectra_response_respects_point_budget(self) -> None:
        for index in range(2):
            file_name = f"ISC_Hadamard 1_棉,100.0_ABC12{index}_20240101_12000{index}_1.csv"
            (self.import_root / file_name).write_text(make_csv_text([("棉", 100.0)]), encoding="utf-8")
        job = self.client.post(
            "/api/import-jobs",
            json={"root_path": str(self.import_root), "recursive": True},
        ).json()
        self._wait_for_job(job["id"])

        self.client.app.state.settings.max_preview_points = 300
        payload = self.client.get(
            "/api/spectra",
            params={"class_key": "棉", "excluded": "active", "limit": 2000},
        ).json()
        self.assertEqual(payload["count"], 2)
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(payload["returned_point_count"], 228)
        self.assertEqual(payload["point_budget"], 300)
        self.assertTrue(payload["truncated_by_point_budget"])

    def test_startup_recovery_marks_interrupted_jobs_failed(self) -> None:
        with session_scope(self.client.app.state.session_factory) as session:
            interrupted = Job(
                type="import",
                status="running",
                params_json="{}",
                stats_json="{}",
                progress_message="Working",
            )
            session.add(interrupted)
            session.flush()
            job_id = interrupted.id

        recovered = self.client.app.state.job_manager.recover_interrupted_jobs()
        self.assertEqual(recovered, 1)
        with session_scope(self.client.app.state.session_factory) as session:
            job = session.get(Job, job_id)
            assert job is not None
            self.assertEqual(job.status, "failed")
            self.assertIsNotNone(job.finished_at)
            self.assertIn("previous application shutdown", job.progress_message)

    def test_subset_store_evicts_least_recently_used_entries(self) -> None:
        store = SubsetStore(max_entries=1, ttl_seconds=3600)
        store.put("first", {"spectrum_ids": [1]})
        store.put("second", {"spectrum_ids": [2]})
        self.assertIsNone(store.get("first"))
        self.assertIsNotNone(store.get("second"))

    def test_imports_fourier_and_grating_formats_with_axis_summary(self) -> None:
        fourier_name = "样品编号 230340227  2025-09-09 083855 GMT+0800.csv"
        grating_name = "SupNIR-3100230122253A01_20240924163908.csv"
        labels = [("棉", 71.0), ("聚酯纤维", 29.0)]
        (self.import_root / fourier_name).write_text(make_fourier_csv_text(labels, part_name="A"), encoding="utf-8")
        (self.import_root / grating_name).write_text(make_grating_csv_text(labels, part_name=""), encoding="utf-8")

        job = self.client.post("/api/import-jobs", json={"root_path": str(self.import_root), "recursive": True}).json()
        completed = self._wait_for_job(job["id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["imported_count"], 2)
        self.assertEqual(completed["failed_count"], 0)

        classes = self.client.get("/api/classes", params={"sort": "name"}).json()["items"]
        self.assertEqual(len(classes), 1)
        self.assertEqual(classes[0]["class_key"], "棉|聚酯纤维")

        summary_payload = self.client.get(
            "/api/spectra/summary",
            params={"class_key": classes[0]["class_key"], "excluded": "active"},
        ).json()
        self.assertEqual(summary_payload["status"], "ready")
        self.assertEqual(summary_payload["total_count"], 2)

        spectra_payload = self.client.get(
            "/api/spectra",
            params={"class_key": classes[0]["class_key"], "excluded": "active", "limit": 2000},
        ).json()
        self.assertEqual(spectra_payload["count"], 2)
        axis_summary = sorted(spectra_payload["axis_summary"], key=lambda item: item["axis_kind"])
        self.assertEqual(
            axis_summary,
            [
                {"axis_kind": "wavelength", "axis_unit": "nm", "count": 1},
                {"axis_kind": "wavenumber", "axis_unit": "cm^-1", "count": 1},
            ],
        )

        wavelength_payload = self.client.get(
            "/api/spectra",
            params={
                "class_key": classes[0]["class_key"],
                "excluded": "active",
                "axis_kind": "wavelength",
                "limit": 2000,
            },
        ).json()
        self.assertEqual(wavelength_payload["count"], 1)
        self.assertEqual(wavelength_payload["items"][0]["axis_kind"], "wavelength")
        self.assertEqual(wavelength_payload["items"][0]["metadata"]["sample_id"], "230122253")
        self.assertEqual(wavelength_payload["items"][0]["metadata"]["acquisition_date"], "2024-09-24")
        self.assertEqual(wavelength_payload["items"][0]["metadata"]["acquisition_time"], "16:39:08")

        wavenumber_payload = self.client.get(
            "/api/spectra",
            params={
                "class_key": classes[0]["class_key"],
                "excluded": "active",
                "axis_kind": "wavenumber",
                "limit": 2000,
            },
        ).json()
        self.assertEqual(wavenumber_payload["count"], 1)
        self.assertEqual(wavenumber_payload["items"][0]["axis_kind"], "wavenumber")
        self.assertEqual(wavenumber_payload["items"][0]["metadata"]["sample_id"], "230340227")
        self.assertEqual(wavenumber_payload["items"][0]["metadata"]["acquisition_date"], "2025-09-09")
        self.assertEqual(wavenumber_payload["items"][0]["metadata"]["acquisition_time"], "08:38:55")
        self.assertEqual(wavenumber_payload["items"][0]["metadata"]["part_name"], "A")

    def test_imports_new_formats_when_filename_metadata_does_not_match(self) -> None:
        fourier_name = "未知傅里叶文件.csv"
        grating_name = "unknown-grating-format.csv"
        labels = [("棉", 100.0)]
        (self.import_root / fourier_name).write_text(make_fourier_csv_text(labels), encoding="utf-8")
        (self.import_root / grating_name).write_text(make_grating_csv_text(labels, part_name="前片"), encoding="utf-8")

        job = self.client.post("/api/import-jobs", json={"root_path": str(self.import_root), "recursive": True}).json()
        completed = self._wait_for_job(job["id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["imported_count"], 2)
        self.assertEqual(completed["failed_count"], 0)

        payload = self.client.get(
            "/api/spectra",
            params={"class_key": "棉", "excluded": "active", "limit": 2000},
        ).json()
        self.assertEqual(payload["count"], 2)
        metadata_by_file = {item["file_name"]: item["metadata"] for item in payload["items"]}
        self.assertIsNone(metadata_by_file[fourier_name]["sample_id"])
        self.assertIsNone(metadata_by_file[fourier_name]["acquisition_date"])
        self.assertIsNone(metadata_by_file[fourier_name]["acquisition_time"])
        self.assertIsNone(metadata_by_file[grating_name]["sample_id"])
        self.assertIsNone(metadata_by_file[grating_name]["acquisition_date"])
        self.assertIsNone(metadata_by_file[grating_name]["acquisition_time"])
        self.assertEqual(metadata_by_file[grating_name]["part_name"], "前片")

    def test_rejects_invalid_new_format_footer_rows(self) -> None:
        invalid_name = "样品编号 230340227  2025-09-09 083855 GMT+0800.csv"
        invalid_text = make_fourier_csv_text([("棉", 60.0)])
        invalid_text = invalid_text.rsplit("\n", 2)[0] + "\n,棉,60.0,未知纤维,40.0\n"
        (self.import_root / invalid_name).write_text(invalid_text, encoding="utf-8")

        job = self.client.post("/api/import-jobs", json={"root_path": str(self.import_root), "recursive": True}).json()
        completed = self._wait_for_job(job["id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["imported_count"], 0)
        self.assertEqual(completed["failed_count"], 1)
        self.assertTrue(completed["params"]["validate_labels"])

    def test_import_label_validation_can_be_disabled_for_all_formats(self) -> None:
        labels = [("未知纤维", 30.0), ("棉", 50.0)]
        portable_name = "ISC_Hadamard 1_未知纤维,30.0,棉,50.0_RELAX1_20240101_120000_1.csv"
        fourier_name = "样品编号 RELAX2  2025-09-09 083855 GMT+0800.csv"
        grating_name = "SupNIR-3100230122253A03_20240924163908.csv"
        (self.import_root / portable_name).write_text(make_csv_text(labels), encoding="utf-8")
        (self.import_root / fourier_name).write_text(make_fourier_csv_text(labels), encoding="utf-8")
        (self.import_root / grating_name).write_text(make_grating_csv_text(labels), encoding="utf-8")

        job = self.client.post(
            "/api/import-jobs",
            json={
                "root_path": str(self.import_root),
                "recursive": True,
                "validate_labels": False,
            },
        ).json()
        completed = self._wait_for_job(job["id"])

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["imported_count"], 3)
        self.assertEqual(completed["failed_count"], 0)
        self.assertFalse(completed["params"]["validate_labels"])

        classes = self.client.get("/api/classes", params={"sort": "name"}).json()["items"]
        self.assertEqual(len(classes), 1)
        self.assertEqual(classes[0]["total_count"], 3)
        spectra = self.client.get(
            "/api/spectra",
            params={"class_key": classes[0]["class_key"], "excluded": "active", "limit": 2000},
        ).json()["items"]
        self.assertEqual(len(spectra), 3)
        for spectrum in spectra:
            self.assertEqual(
                {item["name"]: item["value"] for item in spectrum["labels"]},
                {"未知纤维": 30.0, "棉": 50.0},
            )
            self.assertEqual(sum(item["value"] for item in spectrum["labels"]), 80.0)

    def test_mixed_axis_subsets_preserve_both_axis_types(self) -> None:
        labels = [("棉", 71.0), ("聚酯纤维", 29.0)]
        for index in range(3):
            file_name = f"SupNIR-3100230122253A{index + 1:02d}_2024092416390{index}.csv"
            (self.import_root / file_name).write_text(make_grating_csv_text(labels), encoding="utf-8")
        for index in range(5):
            file_name = f"样品编号 23034022{index}  2025-09-09 08385{index} GMT+0800.csv"
            (self.import_root / file_name).write_text(make_fourier_csv_text(labels, part_name="A"), encoding="utf-8")

        job = self.client.post("/api/import-jobs", json={"root_path": str(self.import_root), "recursive": True}).json()
        completed = self._wait_for_job(job["id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["imported_count"], 8)

        class_key = "棉|聚酯纤维"
        subset_payload = self.client.post(
            f"/api/classes/{class_key}/subsets",
            json={"mode": "count", "parts": 2},
        ).json()
        self.assertEqual([item["count"] for item in subset_payload["subsets"]], [2, 2, 2, 2])

        summary_payloads = [
            self.client.get(
                "/api/spectra/summary",
                params={"class_key": class_key, "excluded": "active", "subset_id": subset["subset_id"]},
            ).json()
            for subset in subset_payload["subsets"]
        ]
        self.assertEqual(sum(payload["total_count"] for payload in summary_payloads), 8)
        self.assertTrue(
            any(
                {item["axis_kind"] for item in payload["axis_summary"]} == {"wavelength", "wavenumber"}
                for payload in summary_payloads
            )
        )

    def test_rebuilds_class_stats_on_startup_when_cache_is_missing(self) -> None:
        file_name = "ISC_Hadamard 1_聚酯纤维,100.0_ABC123_20240101_120000_1.csv"
        (self.import_root / file_name).write_text(make_csv_text([("聚酯纤维", 100.0)]), encoding="utf-8")

        job = self.client.post("/api/import-jobs", json={"root_path": str(self.import_root), "recursive": True}).json()
        self._wait_for_job(job["id"])

        with session_scope(self.client.app.state.session_factory) as session:
            session.query(ClassStat).delete()
            session.query(ClassAxisStat).delete()

        restart_client = TestClient(create_app(self.settings))
        try:
            first_payload = restart_client.get("/api/classes").json()
            self.assertIn(first_payload["meta"]["status"], {"building", "ready"})

            deadline = time.time() + 5.0
            while time.time() < deadline:
                payload = restart_client.get("/api/classes").json()
                if payload["items"]:
                    self.assertEqual(payload["meta"]["status"], "ready")
                    self.assertEqual(payload["items"][0]["class_key"], "聚酯纤维")
                    with session_scope(restart_client.app.state.session_factory) as session:
                        self.assertGreater(session.query(ClassAxisStat).count(), 0)
                        index_names = {
                            row[1]
                            for row in session.connection().exec_driver_sql("PRAGMA index_list('spectra')").all()
                        }
                        self.assertIn("ix_spectra_class_excluded_axis_file", index_names)
                        self.assertIn("ix_spectra_class_excluded_file", index_names)
                    return
                time.sleep(0.05)
        finally:
            restart_client.close()

        self.fail("class stats were not rebuilt before timeout")

    def _wait_for_job(self, job_id: int, timeout_seconds: float = 10.0) -> dict:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            payload = self.client.get(f"/api/jobs/{job_id}").json()
            if payload["status"] in {"completed", "failed"}:
                return payload
            time.sleep(0.05)
        self.fail(f"job {job_id} did not finish before timeout")


if __name__ == "__main__":
    unittest.main()
