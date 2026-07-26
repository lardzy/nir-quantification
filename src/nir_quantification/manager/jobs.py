from __future__ import annotations

import gzip
import hashlib
import os
import tempfile
import threading
import time
import traceback
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import sessionmaker

from .config import ManagerSettings
from .db import decode_json, encode_json, session_scope
from .models import ClassAxisStat, ClassStat, Job, Spectrum
from .parsers import ParserRegistry
from .service import build_classification, job_to_dict, recompute_class_stats, upsert_spectrum_from_parsed, utcnow


MAX_JOB_LOG_CHARS = 200_000


class SubsetStore:
    def __init__(self, max_entries: int = 256, ttl_seconds: int = 3600) -> None:
        self._lock = threading.Lock()
        self._subsets: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._spectrum_index: dict[int, set[str]] = {}
        self._max_entries = max(1, max_entries)
        self._ttl_seconds = max(60, ttl_seconds)

    def put(self, subset_id: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self._cleanup_locked()
            self._remove_locked(subset_id)
            payload["_last_access_at"] = time.monotonic()
            self._subsets[subset_id] = payload
            for spectrum_id in payload.get("spectrum_ids", []):
                self._spectrum_index.setdefault(int(spectrum_id), set()).add(subset_id)
            while len(self._subsets) > self._max_entries:
                oldest_id = next(iter(self._subsets))
                self._remove_locked(oldest_id)

    def get(self, subset_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._cleanup_locked()
            payload = self._subsets.get(subset_id)
            if payload is not None:
                payload["_last_access_at"] = time.monotonic()
                self._subsets.move_to_end(subset_id)
            return payload

    def get_summary(self, subset_id: str, excluded: str) -> dict[str, Any] | None:
        with self._lock:
            self._cleanup_locked()
            payload = self._subsets.get(subset_id)
            if payload is None:
                return None
            payload["_last_access_at"] = time.monotonic()
            self._subsets.move_to_end(subset_id)
            axis_counts = payload["axis_summary_by_filter"].get(excluded, {})
            items = [
                {
                    "axis_kind": axis_kind,
                    "axis_unit": axis_unit,
                    "count": count,
                }
                for (axis_kind, axis_unit), count in sorted(axis_counts.items())
                if count > 0
            ]
            return {
                "total_count": int(payload["count_by_filter"].get(excluded, 0)),
                "axis_summary": items,
                "source": "subset-cache",
            }

    def adjust_for_exclusion(self, spectrum: Spectrum, excluded: bool) -> None:
        with self._lock:
            self._cleanup_locked()
            subset_ids = list(self._spectrum_index.get(int(spectrum.id), set()))
            if not subset_ids:
                return
            axis_key = (spectrum.axis_kind, spectrum.axis_unit)
            for subset_id in subset_ids:
                payload = self._subsets.get(subset_id)
                if payload is None:
                    continue
                count_by_filter = payload.get("count_by_filter", {})
                axis_summary_by_filter = payload.get("axis_summary_by_filter", {})
                if excluded:
                    if int(count_by_filter.get("active", 0)) > 0:
                        count_by_filter["active"] = int(count_by_filter.get("active", 0)) - 1
                    count_by_filter["excluded"] = int(count_by_filter.get("excluded", 0)) + 1
                    active_axis = axis_summary_by_filter.setdefault("active", {})
                    excluded_axis = axis_summary_by_filter.setdefault("excluded", {})
                    if int(active_axis.get(axis_key, 0)) > 0:
                        active_axis[axis_key] = int(active_axis.get(axis_key, 0)) - 1
                        if active_axis[axis_key] <= 0:
                            active_axis.pop(axis_key, None)
                    excluded_axis[axis_key] = int(excluded_axis.get(axis_key, 0)) + 1
                else:
                    if int(count_by_filter.get("excluded", 0)) > 0:
                        count_by_filter["excluded"] = int(count_by_filter.get("excluded", 0)) - 1
                    count_by_filter["active"] = int(count_by_filter.get("active", 0)) + 1
                    active_axis = axis_summary_by_filter.setdefault("active", {})
                    excluded_axis = axis_summary_by_filter.setdefault("excluded", {})
                    active_axis[axis_key] = int(active_axis.get(axis_key, 0)) + 1
                    if int(excluded_axis.get(axis_key, 0)) > 0:
                        excluded_axis[axis_key] = int(excluded_axis.get(axis_key, 0)) - 1
                        if excluded_axis[axis_key] <= 0:
                            excluded_axis.pop(axis_key, None)

    def _cleanup_locked(self) -> None:
        cutoff = time.monotonic() - self._ttl_seconds
        expired_ids = [
            subset_id
            for subset_id, payload in self._subsets.items()
            if float(payload.get("_last_access_at", 0.0)) < cutoff
        ]
        for subset_id in expired_ids:
            self._remove_locked(subset_id)

    def _remove_locked(self, subset_id: str) -> None:
        payload = self._subsets.pop(subset_id, None)
        if payload is None:
            return
        for spectrum_id in payload.get("spectrum_ids", []):
            indexed_subsets = self._spectrum_index.get(int(spectrum_id))
            if indexed_subsets is None:
                continue
            indexed_subsets.discard(subset_id)
            if not indexed_subsets:
                self._spectrum_index.pop(int(spectrum_id), None)


class JobManager:
    def __init__(self, settings: ManagerSettings, session_factory: sessionmaker, parser_registry: ParserRegistry) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.parser_registry = parser_registry
        self.subsets = SubsetStore(
            max_entries=settings.subset_cache_max_entries,
            ttl_seconds=settings.subset_cache_ttl_seconds,
        )
        self._future_lock = threading.Lock()
        self._futures = set()
        self._executor = ThreadPoolExecutor(
            max_workers=settings.max_concurrent_jobs,
            thread_name_prefix="nirq-job",
        )
        self._closed = False
        self.recover_interrupted_jobs()

    def recover_interrupted_jobs(self) -> int:
        finished_at = utcnow()
        with session_scope(self.session_factory) as session:
            result = session.execute(
                update(Job)
                .where(Job.status.in_(("pending", "running")))
                .values(
                    status="failed",
                    finished_at=finished_at,
                    updated_at=finished_at,
                    progress_message="Interrupted by previous application shutdown",
                )
            )
            return int(getattr(result, "rowcount", 0) or 0)

    def shutdown(self, wait: bool = True) -> None:
        with self._future_lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=False)

    def ensure_class_stats(self) -> None:
        with session_scope(self.session_factory) as session:
            spectra_exists = session.scalar(select(Spectrum.id).limit(1)) is not None
            stats_exists = session.scalar(select(ClassStat.class_key).limit(1)) is not None
            axis_stats_exists = session.scalar(select(ClassAxisStat.class_key).limit(1)) is not None
            running_job = session.scalar(
                select(Job)
                .where(Job.type == "maintenance", Job.status.in_(("pending", "running")))
                .order_by(Job.created_at.desc())
                .limit(1)
            )
        if spectra_exists and (not stats_exists or not axis_stats_exists) and running_job is None:
            self.create_class_stats_rebuild_job(reason="startup")

    def create_import_job(self, root_path: Path, recursive: bool = True) -> dict[str, Any]:
        with session_scope(self.session_factory) as session:
            job = Job(
                type="import",
                status="pending",
                params_json=encode_json({"root_path": str(root_path), "recursive": recursive}),
                stats_json=encode_json({}),
                progress_message="Queued",
            )
            session.add(job)
            session.flush()
            job_id = job.id
        self._spawn(target=self._run_import_job, job_id=job_id)
        return self.get_job(job_id)

    def create_export_job(self, export_root: Path, scope: str, class_keys: list[str] | None = None) -> dict[str, Any]:
        params = {"export_root": str(export_root), "scope": scope, "class_keys": class_keys or []}
        with session_scope(self.session_factory) as session:
            job = Job(
                type="export",
                status="pending",
                params_json=encode_json(params),
                stats_json=encode_json({}),
                progress_message="Queued",
            )
            session.add(job)
            session.flush()
            job_id = job.id
        self._spawn(target=self._run_export_job, job_id=job_id)
        return self.get_job(job_id)

    def create_class_stats_rebuild_job(self, reason: str = "startup") -> dict[str, Any]:
        with session_scope(self.session_factory) as session:
            job = Job(
                type="maintenance",
                status="pending",
                params_json=encode_json({"task": "class_stats_rebuild", "reason": reason}),
                stats_json=encode_json({}),
                progress_message="Queued",
            )
            session.add(job)
            session.flush()
            job_id = job.id
        self._spawn(target=self._run_class_stats_rebuild_job, job_id=job_id)
        return self.get_job(job_id)

    def get_job(self, job_id: int) -> dict[str, Any]:
        with session_scope(self.session_factory) as session:
            job = session.get(Job, job_id)
            if job is None:
                raise KeyError(f"job {job_id} not found")
            return job_to_dict(job)

    def create_subset(
        self,
        class_key: str,
        mode: str,
        parts: int | None,
        ratios: list[float] | None,
        spectra_rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        import uuid

        subsets: list[dict[str, Any]] = []
        target_sizes = self._build_subset_target_sizes(
            total_count=len(spectra_rows),
            mode=mode,
            parts=parts,
            ratios=ratios,
        )
        groups = self._distribute_rows_across_subsets(spectra_rows, target_sizes)

        for index, rows in enumerate(groups, start=1):
            if not rows:
                continue
            subset_id = uuid.uuid4().hex
            payload = self._build_subset_payload(subset_id=subset_id, class_key=class_key, index=index, spectra_rows=rows)
            self.subsets.put(subset_id, payload)
            subsets.append({"subset_id": subset_id, "index": index, "count": len(rows)})
        return {"class_key": class_key, "mode": mode, "subsets": subsets}

    def _build_subset_target_sizes(
        self,
        total_count: int,
        mode: str,
        parts: int | None,
        ratios: list[float] | None,
    ) -> list[int]:
        if total_count <= 0:
            return []
        if mode == "count":
            chunk_size = max(1, parts or 1)
            return [
                min(chunk_size, total_count - index)
                for index in range(0, total_count, chunk_size)
            ]

        raw_partition_count = parts if parts is not None else int(ratios[0]) if ratios else 1
        partition_count = max(1, raw_partition_count)
        base_size = total_count // partition_count
        remainder = total_count % partition_count
        return [
            base_size + (1 if index < remainder else 0)
            for index in range(partition_count)
            if base_size + (1 if index < remainder else 0) > 0
        ]

    def _distribute_rows_across_subsets(
        self,
        spectra_rows: list[dict[str, Any]],
        target_sizes: list[int],
    ) -> list[list[dict[str, Any]]]:
        if not spectra_rows or not target_sizes:
            return []
        if len(target_sizes) == 1:
            return [sorted(spectra_rows, key=lambda row: str(row["file_name"]))]

        grouped_rows: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in spectra_rows:
            axis_key = (str(row["axis_kind"]), str(row["axis_unit"]))
            grouped_rows.setdefault(axis_key, []).append(row)

        axis_priority = {"wavelength": 0, "wavenumber": 1}
        axis_keys = sorted(
            grouped_rows.keys(),
            key=lambda key: (
                len(grouped_rows[key]),
                axis_priority.get(key[0], 99),
                key[1],
            ),
        )

        buckets: list[list[dict[str, Any]]] = [[] for _ in target_sizes]
        remaining_capacity = list(target_sizes)
        bucket_count = len(buckets)

        for axis_key in axis_keys:
            bucket_index = 0
            axis_rows = sorted(grouped_rows[axis_key], key=lambda row: str(row["file_name"]))
            for row in axis_rows:
                start_index = bucket_index
                while remaining_capacity[bucket_index] <= 0:
                    bucket_index = (bucket_index + 1) % bucket_count
                    if bucket_index == start_index:
                        raise RuntimeError("subset capacity exhausted while distributing spectra")
                buckets[bucket_index].append(row)
                remaining_capacity[bucket_index] -= 1
                bucket_index = (bucket_index + 1) % bucket_count

        return [
            sorted(bucket, key=lambda row: str(row["file_name"]))
            for bucket in buckets
            if bucket
        ]

    def _build_subset_payload(
        self,
        subset_id: str,
        class_key: str,
        index: int,
        spectra_rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        axis_summary_by_filter: dict[str, dict[tuple[str, str], int]] = {
            "all": {},
            "active": {},
            "excluded": {},
        }
        count_by_filter = {"all": len(spectra_rows), "active": 0, "excluded": 0}
        spectrum_ids: list[int] = []
        for row in spectra_rows:
            spectrum_id = int(row["id"])
            axis_key = (str(row["axis_kind"]), str(row["axis_unit"]))
            spectrum_ids.append(spectrum_id)
            axis_summary_by_filter["all"][axis_key] = int(axis_summary_by_filter["all"].get(axis_key, 0)) + 1
            filter_key = "excluded" if bool(row["is_excluded"]) else "active"
            count_by_filter[filter_key] = int(count_by_filter.get(filter_key, 0)) + 1
            axis_summary_by_filter[filter_key][axis_key] = int(axis_summary_by_filter[filter_key].get(axis_key, 0)) + 1
        return {
            "subset_id": subset_id,
            "class_key": class_key,
            "index": index,
            "spectrum_ids": spectrum_ids,
            "count_by_filter": count_by_filter,
            "axis_summary_by_filter": axis_summary_by_filter,
        }

    def _spawn(self, target, job_id: int) -> None:
        with self._future_lock:
            if self._closed:
                raise RuntimeError("job manager is shutting down")
            future = self._executor.submit(target, job_id=job_id)
            self._futures.add(future)

        def discard_future(completed_future) -> None:
            with self._future_lock:
                self._futures.discard(completed_future)

        future.add_done_callback(discard_future)

    def _run_import_job(self, job_id: int) -> None:
        with session_scope(self.session_factory) as session:
            job = session.get(Job, job_id)
            assert job is not None
            params = decode_json(job.params_json, {})
            root_path = Path(params["root_path"])
            recursive = bool(params.get("recursive", True))
            job.status = "running"
            job.started_at = utcnow()
            job.progress_message = "Scanning files"

        try:
            paths = sorted(root_path.rglob("*.csv") if recursive else root_path.glob("*.csv"))
            unique_paths: list[Path] = []
            first_path_by_name: dict[str, Path] = {}
            duplicate_logs: list[str] = []
            duplicate_skipped = duplicate_failed = 0
            for path in paths:
                first_path = first_path_by_name.get(path.name)
                if first_path is not None:
                    try:
                        same_content = hashlib.sha256(first_path.read_bytes()).digest() == hashlib.sha256(path.read_bytes()).digest()
                    except OSError as error:
                        duplicate_failed += 1
                        duplicate_logs.append(f"[duplicate-read-error] {path}: {error}")
                        continue
                    if same_content:
                        duplicate_skipped += 1
                        duplicate_logs.append(f"[duplicate-file-name] {path.name}: identical duplicate skipped")
                    else:
                        duplicate_failed += 1
                        duplicate_logs.append(
                            f"[duplicate-file-name-conflict] {path.name}: same name has different content"
                        )
                    continue
                first_path_by_name[path.name] = path
                unique_paths.append(path)

            imported = 0
            skipped = duplicate_skipped
            failed = duplicate_failed
            processed = duplicate_skipped + duplicate_failed
            self._update_job(
                job_id,
                total_discovered=len(paths),
                processed_count=processed,
                skipped_count=skipped,
                progress_message=f"Found {len(paths)} CSV files",
            )
            for log_message in duplicate_logs:
                self._append_log(job_id, log_message)

            with ThreadPoolExecutor(max_workers=self.settings.max_workers) as executor:
                chunk_size = max(self.settings.job_batch_size, self.settings.max_workers * 2)
                for start in range(0, len(unique_paths), chunk_size):
                    chunk = unique_paths[start : start + chunk_size]
                    future_map = {executor.submit(self.parser_registry.parse, path): path for path in chunk}
                    batch = []
                    for future in as_completed(future_map):
                        path = future_map.pop(future)
                        try:
                            batch.append(future.result())
                        except Exception as error:
                            failed += 1
                            processed += 1
                            self._append_log(job_id, f"[import-error] {path.name}: {error}")

                    batch_imported, batch_skipped, batch_failed = self._flush_import_batch(job_id, batch)
                    imported += batch_imported
                    skipped += batch_skipped
                    failed += batch_failed
                    processed += len(batch)
                    self._update_job(
                        job_id,
                        processed_count=processed,
                        imported_count=imported,
                        skipped_count=skipped,
                        failed_count=failed,
                        progress_message=f"Imported {imported}, skipped {skipped}, failed {failed}",
                    )

            self._finish_job(
                job_id,
                status="completed",
                processed_count=processed,
                imported_count=imported,
                skipped_count=skipped,
                failed_count=failed,
                progress_message=f"Completed import: {imported} imported, {skipped} skipped, {failed} failed",
            )
        except Exception as error:  # pragma: no cover
            self._append_log(job_id, traceback.format_exc())
            self._finish_job(job_id, status="failed", progress_message=str(error))

    def _flush_import_batch(self, job_id: int, batch) -> tuple[int, int, int]:
        imported = skipped = failed = 0
        if not batch:
            return imported, skipped, failed
        affected_class_keys: set[str] = set()
        error_messages: list[str] = []
        with session_scope(self.session_factory) as session:
            for parsed in batch:
                try:
                    with session.begin_nested():
                        result = upsert_spectrum_from_parsed(session, parsed)
                except Exception as error:
                    failed += 1
                    error_messages.append(f"[import-conflict] {parsed.file_name}: {error}")
                    continue
                if result == "imported":
                    imported += 1
                    affected_class_keys.add(build_classification(parsed.labels)[0])
                else:
                    skipped += 1
            if affected_class_keys:
                recompute_class_stats(session, sorted(affected_class_keys))
        for message in error_messages:
            self._append_log(job_id, message)
        return imported, skipped, failed

    def _run_export_job(self, job_id: int) -> None:
        with session_scope(self.session_factory) as session:
            job = session.get(Job, job_id)
            assert job is not None
            params = decode_json(job.params_json, {})
            export_root = Path(params["export_root"])
            scope = params["scope"]
            class_keys = params.get("class_keys") or []
            job.status = "running"
            job.started_at = utcnow()
            job.progress_message = "Preparing export"

            filters = []
            if scope == "active":
                filters.append(Spectrum.is_excluded.is_(False))
            elif scope == "excluded":
                filters.append(Spectrum.is_excluded.is_(True))
            if class_keys:
                filters.append(Spectrum.class_key.in_(class_keys))
            total_count = int(session.scalar(select(func.count(Spectrum.id)).where(*filters)) or 0)
        export_root.mkdir(parents=True, exist_ok=True)
        self._update_job(job_id, total_discovered=total_count, progress_message=f"Exporting {total_count} files")

        written = failed = 0
        try:
            with ThreadPoolExecutor(max_workers=self.settings.max_workers) as executor:
                last_id = 0
                while True:
                    with session_scope(self.session_factory) as session:
                        rows = session.execute(
                            select(
                                Spectrum.id,
                                Spectrum.file_name,
                                Spectrum.class_display_name,
                                Spectrum.raw_csv_gzip,
                            )
                            .where(Spectrum.id > last_id, *filters)
                            .order_by(Spectrum.id.asc())
                            .limit(self.settings.job_batch_size)
                        ).all()
                        payload = [
                            {
                                "id": int(row.id),
                                "file_name": row.file_name,
                                "class_display_name": row.class_display_name,
                                "raw_csv_gzip": row.raw_csv_gzip,
                            }
                            for row in rows
                        ]
                    if not payload:
                        break
                    last_id = int(payload[-1]["id"])
                    future_map = {
                        executor.submit(self._write_export_file, export_root, scope, item): item["file_name"]
                        for item in payload
                    }
                    for future in as_completed(future_map):
                        file_name = future_map.pop(future)
                        try:
                            future.result()
                            written += 1
                        except Exception as error:
                            failed += 1
                            self._append_log(job_id, f"[export-error] {file_name}: {error}")
                    self._update_job(
                        job_id,
                        processed_count=written + failed,
                        imported_count=written,
                        failed_count=failed,
                        progress_message=f"Exported {written}, failed {failed}",
                    )
            self._finish_job(
                job_id,
                status="completed",
                processed_count=written + failed,
                imported_count=written,
                failed_count=failed,
                progress_message=f"Completed export: {written} written, {failed} failed",
            )
        except Exception as error:  # pragma: no cover
            self._append_log(job_id, traceback.format_exc())
            self._finish_job(job_id, status="failed", progress_message=str(error))

    def _run_class_stats_rebuild_job(self, job_id: int) -> None:
        with session_scope(self.session_factory) as session:
            job = session.get(Job, job_id)
            assert job is not None
            total_classes = int(session.scalar(select(func.count(func.distinct(Spectrum.class_key)))) or 0)
            job.status = "running"
            job.started_at = utcnow()
            job.total_discovered = total_classes
            job.progress_message = "正在初始化分类索引"

        try:
            with session_scope(self.session_factory) as session:
                rebuilt = recompute_class_stats(session)
            self._finish_job(
                job_id,
                status="completed",
                processed_count=rebuilt,
                imported_count=rebuilt,
                progress_message=f"分类索引初始化完成，共 {rebuilt} 个分类",
            )
        except Exception as error:  # pragma: no cover
            self._append_log(job_id, traceback.format_exc())
            self._finish_job(job_id, status="failed", progress_message=str(error))

    def _write_export_file(self, export_root: Path, scope: str, item: dict[str, Any]) -> None:
        target_dir = export_root / scope / _safe_dir_name(item["class_display_name"] or "未分类")
        target_dir.mkdir(parents=True, exist_ok=True)
        raw_bytes = gzip.decompress(item["raw_csv_gzip"])
        target_path = target_dir / item["file_name"]
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=target_dir,
                prefix=f".{target_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                handle.write(raw_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, target_path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _update_job(self, job_id: int, **fields) -> None:
        with session_scope(self.session_factory) as session:
            job = session.get(Job, job_id)
            if job is None:
                return
            for key, value in fields.items():
                setattr(job, key, value)

    def _append_log(self, job_id: int, message: str) -> None:
        with session_scope(self.session_factory) as session:
            job = session.get(Job, job_id)
            if job is None:
                return
            existing = job.log_text or ""
            job.log_text = f"{existing}{message}\n"[-MAX_JOB_LOG_CHARS:]

    def _finish_job(self, job_id: int, status: str, progress_message: str, **fields) -> None:
        with session_scope(self.session_factory) as session:
            job = session.get(Job, job_id)
            if job is None:
                return
            job.status = status
            job.finished_at = utcnow()
            job.progress_message = progress_message
            for key, value in fields.items():
                setattr(job, key, value)


def _safe_dir_name(name: str) -> str:
    return name.replace("/", "_").replace("\\", "_").strip() or "未分类"
