from __future__ import annotations

import gzip
import hashlib
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Select, case, delete, func, select, update
from sqlalchemy.orm import Session

from .db import decode_json, encode_json
from .models import ClassAxisStat, ClassStat, Job, Spectrum, SpectrumComponent
from .parsers import ParsedComponent, ParsedSpectrum

SPECTRUM_DETAIL_COLUMNS = (
    Spectrum.id,
    Spectrum.file_name,
    Spectrum.source_path_last_seen,
    Spectrum.content_sha256,
    Spectrum.metadata_json,
    Spectrum.axis_kind,
    Spectrum.axis_unit,
    Spectrum.point_count,
    Spectrum.x_values_json,
    Spectrum.y_values_json,
    Spectrum.labels_json,
    Spectrum.class_key,
    Spectrum.class_display_name,
    Spectrum.component_count,
    Spectrum.is_excluded,
    Spectrum.excluded_at,
    Spectrum.created_at,
    Spectrum.updated_at,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def build_classification(labels: Iterable[ParsedComponent]) -> tuple[str, str, int]:
    names = sorted(component.name for component in labels)
    class_key = "|".join(names)
    class_display_name = "、".join(names)
    return class_key, class_display_name, len(names)


def upsert_spectrum_from_parsed(session: Session, parsed: ParsedSpectrum) -> str:
    existing = session.scalar(select(Spectrum).where(Spectrum.file_name == parsed.file_name))
    if existing is not None:
        existing_raw = gzip.decompress(existing.raw_csv_gzip)
        existing_digest = existing.content_sha256 or hashlib.sha256(existing_raw).hexdigest()
        if existing_digest != parsed.content_sha256:
            existing_normalized = _normalized_csv_digest(existing_raw)
            parsed_raw = gzip.decompress(parsed.raw_csv_gzip)
            if existing_normalized != _normalized_csv_digest(parsed_raw):
                raise ValueError(
                    f"file name conflict: {parsed.file_name} already exists with different content"
                )
            existing.raw_csv_gzip = parsed.raw_csv_gzip
        existing.content_sha256 = parsed.content_sha256
        existing.source_path_last_seen = parsed.source_path
        existing.updated_at = utcnow()
        return "skipped"

    class_key, class_display_name, component_count = build_classification(parsed.labels)
    spectrum = Spectrum(
        file_name=parsed.file_name,
        source_path_last_seen=parsed.source_path,
        raw_csv_gzip=parsed.raw_csv_gzip,
        content_sha256=parsed.content_sha256,
        metadata_json=encode_json(parsed.metadata),
        axis_kind=parsed.axis_kind,
        axis_unit=parsed.axis_unit,
        point_count=parsed.point_count,
        x_values_json=encode_json(parsed.x_values),
        y_values_json=encode_json(parsed.y_values),
        labels_json=encode_json([{"name": label.name, "value": label.value} for label in parsed.labels]),
        class_key=class_key,
        class_display_name=class_display_name,
        component_count=component_count,
    )
    spectrum.components = [SpectrumComponent(name=label.name, value=label.value) for label in parsed.labels]
    session.add(spectrum)
    return "imported"


def spectrum_to_dict(spectrum: Spectrum) -> dict[str, Any]:
    return {
        "id": spectrum.id,
        "file_name": spectrum.file_name,
        "source_path_last_seen": spectrum.source_path_last_seen,
        "content_sha256": spectrum.content_sha256,
        "metadata": decode_json(spectrum.metadata_json, {}),
        "axis_kind": spectrum.axis_kind,
        "axis_unit": spectrum.axis_unit,
        "point_count": spectrum.point_count,
        "x_values": decode_json(spectrum.x_values_json, []),
        "y_values": decode_json(spectrum.y_values_json, []),
        "labels": decode_json(spectrum.labels_json, []),
        "class_key": spectrum.class_key,
        "class_display_name": spectrum.class_display_name,
        "component_count": spectrum.component_count,
        "is_excluded": spectrum.is_excluded,
        "excluded_at": spectrum.excluded_at.isoformat() if spectrum.excluded_at else None,
        "created_at": spectrum.created_at.isoformat() if spectrum.created_at else None,
        "updated_at": spectrum.updated_at.isoformat() if spectrum.updated_at else None,
    }


def spectrum_row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "file_name": row.file_name,
        "source_path_last_seen": row.source_path_last_seen,
        "content_sha256": row.content_sha256,
        "metadata": decode_json(row.metadata_json, {}),
        "axis_kind": row.axis_kind,
        "axis_unit": row.axis_unit,
        "point_count": row.point_count,
        "x_values": decode_json(row.x_values_json, []),
        "y_values": decode_json(row.y_values_json, []),
        "labels": decode_json(row.labels_json, []),
        "class_key": row.class_key,
        "class_display_name": row.class_display_name,
        "component_count": row.component_count,
        "is_excluded": row.is_excluded,
        "excluded_at": row.excluded_at.isoformat() if row.excluded_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def class_stats_status(session: Session) -> dict[str, str | None]:
    stats_exists = session.scalar(select(ClassStat.class_key).limit(1)) is not None
    axis_stats_exists = session.scalar(select(ClassAxisStat.class_key).limit(1)) is not None
    spectra_exists = session.scalar(select(Spectrum.id).limit(1)) is not None
    if (stats_exists and axis_stats_exists) or not spectra_exists:
        return {"status": "ready", "progress_message": None}

    latest_job = session.scalar(
        select(Job)
        .where(Job.type == "maintenance")
        .order_by(Job.created_at.desc())
        .limit(1)
    )
    if latest_job is not None and latest_job.status in {"pending", "running"}:
        return {
            "status": "building",
            "progress_message": latest_job.progress_message or "正在初始化分类索引",
        }
    return {"status": "building", "progress_message": "正在初始化分类索引"}


def list_classes(session: Session, sort_by: str = "count") -> list[dict[str, Any]]:
    stmt = select(
        ClassStat.class_key,
        ClassStat.class_display_name,
        ClassStat.component_count,
        ClassStat.total_count,
        ClassStat.active_count,
        ClassStat.excluded_count,
    )
    rows = session.execute(stmt).all()
    items = [
        {
            "class_key": row.class_key,
            "class_display_name": row.class_display_name,
            "component_count": row.component_count,
            "total_count": int(row.total_count or 0),
            "active_count": int(row.active_count or 0),
            "excluded_count": int(row.excluded_count or 0),
        }
        for row in rows
    ]

    if sort_by == "component_count":
        items.sort(key=lambda item: (item["component_count"], -item["total_count"], item["class_display_name"]))
    elif sort_by == "name":
        items.sort(key=lambda item: item["class_display_name"])
    else:
        items.sort(key=lambda item: (-item["total_count"], item["class_display_name"]))
    return items


def recompute_class_stats(session: Session, class_keys: Sequence[str] | None = None) -> int:
    session.flush()
    keys = sorted({key for key in class_keys or [] if key})
    class_stmt = (
        select(
            Spectrum.class_key,
            Spectrum.class_display_name,
            Spectrum.component_count,
            func.count(Spectrum.id).label("total_count"),
            func.sum(case((Spectrum.is_excluded.is_(False), 1), else_=0)).label("active_count"),
            func.sum(case((Spectrum.is_excluded.is_(True), 1), else_=0)).label("excluded_count"),
        )
        .group_by(Spectrum.class_key, Spectrum.class_display_name, Spectrum.component_count)
    )
    axis_stmt = (
        select(
            Spectrum.class_key,
            Spectrum.axis_kind,
            Spectrum.axis_unit,
            func.count(Spectrum.id).label("total_count"),
            func.sum(case((Spectrum.is_excluded.is_(False), 1), else_=0)).label("active_count"),
            func.sum(case((Spectrum.is_excluded.is_(True), 1), else_=0)).label("excluded_count"),
        )
        .group_by(Spectrum.class_key, Spectrum.axis_kind, Spectrum.axis_unit)
    )
    if keys:
        class_stmt = class_stmt.where(Spectrum.class_key.in_(keys))
        axis_stmt = axis_stmt.where(Spectrum.class_key.in_(keys))

    if keys:
        session.execute(delete(ClassStat).where(ClassStat.class_key.in_(keys)))
        session.execute(delete(ClassAxisStat).where(ClassAxisStat.class_key.in_(keys)))
    else:
        session.execute(delete(ClassStat))
        session.execute(delete(ClassAxisStat))

    rows = session.execute(class_stmt).all()
    axis_rows = session.execute(axis_stmt).all()

    if rows:
        session.add_all(
            [
                ClassStat(
                    class_key=row.class_key,
                    class_display_name=row.class_display_name,
                    component_count=row.component_count,
                    total_count=int(row.total_count or 0),
                    active_count=int(row.active_count or 0),
                    excluded_count=int(row.excluded_count or 0),
                    updated_at=utcnow(),
                )
                for row in rows
            ]
        )
    if axis_rows:
        session.add_all(
            [
                ClassAxisStat(
                    class_key=row.class_key,
                    axis_kind=row.axis_kind,
                    axis_unit=row.axis_unit,
                    total_count=int(row.total_count or 0),
                    active_count=int(row.active_count or 0),
                    excluded_count=int(row.excluded_count or 0),
                    updated_at=utcnow(),
                )
                for row in axis_rows
            ]
        )
    return len(rows)


def adjust_class_stats_for_exclusion(session: Session, spectrum: Spectrum, excluded: bool) -> dict[str, bool]:
    """Atomically adjust cached class counters for a single spectrum exclusion/restoration."""
    delta = 1 if excluded else -1
    updated_at = utcnow()
    class_result = session.execute(
        update(ClassStat)
        .where(ClassStat.class_key == spectrum.class_key)
        .values(
            active_count=ClassStat.active_count - delta,
            excluded_count=ClassStat.excluded_count + delta,
            updated_at=updated_at,
        )
    )
    axis_result = session.execute(
        update(ClassAxisStat)
        .where(
            ClassAxisStat.class_key == spectrum.class_key,
            ClassAxisStat.axis_kind == spectrum.axis_kind,
            ClassAxisStat.axis_unit == spectrum.axis_unit,
        )
        .values(
            active_count=ClassAxisStat.active_count - delta,
            excluded_count=ClassAxisStat.excluded_count + delta,
            updated_at=updated_at,
        )
    )
    class_updated = bool(getattr(class_result, "rowcount", 0))
    axis_updated = bool(getattr(axis_result, "rowcount", 0))
    if not class_updated or not axis_updated:
        recompute_class_stats(session, [spectrum.class_key])
        return {
            "class_updated": class_updated,
            "axis_updated": axis_updated,
            "recomputed": True,
        }
    return {
        "class_updated": class_updated,
        "axis_updated": axis_updated,
        "recomputed": False,
    }


def transition_spectrum_exclusion(
    session: Session,
    spectrum_id: int,
    *,
    excluded: bool,
) -> tuple[Spectrum | None, bool]:
    """Apply one exclusion state transition with a database-level compare-and-set."""
    changed_at = utcnow()
    result = session.execute(
        update(Spectrum)
        .where(
            Spectrum.id == spectrum_id,
            Spectrum.is_excluded.is_(not excluded),
        )
        .values(
            is_excluded=excluded,
            excluded_at=changed_at if excluded else None,
            updated_at=changed_at,
        )
    )
    changed = bool(getattr(result, "rowcount", 0))

    # A caller may already have loaded a stale identity into this session. Always
    # expire it so an idempotent request returns the committed database state.
    session.expire_all()
    spectrum = session.get(Spectrum, spectrum_id)
    if spectrum is not None and changed:
        adjust_class_stats_for_exclusion(session, spectrum, excluded=excluded)
    return spectrum, changed


def spectra_summary(
    session: Session,
    class_key: str | None,
    excluded: str,
    component_count: int | None,
    axis_kind: str | None = None,
    subset_spectrum_ids: list[int] | None = None,
) -> dict[str, Any]:
    cached_total_count = _cached_total_count(session, class_key, excluded, component_count, axis_kind, subset_spectrum_ids)
    total_count = cached_total_count
    if total_count is None:
        count_stmt = select(func.count(Spectrum.id))
        count_stmt = _apply_spectrum_filters(count_stmt, class_key, excluded, component_count, axis_kind, subset_spectrum_ids)
        total_count = int(session.scalar(count_stmt) or 0)
    return {
        "total_count": total_count,
        "axis_summary": axis_summary_for_query(session, class_key, excluded, component_count, subset_spectrum_ids),
    }


def axis_summary_for_query(
    session: Session,
    class_key: str | None,
    excluded: str,
    component_count: int | None,
    subset_spectrum_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    if class_key and component_count is None and subset_spectrum_ids is None:
        cached_rows = session.execute(
            select(
                ClassAxisStat.axis_kind,
                ClassAxisStat.axis_unit,
                ClassAxisStat.total_count,
                ClassAxisStat.active_count,
                ClassAxisStat.excluded_count,
            )
            .where(ClassAxisStat.class_key == class_key)
            .order_by(ClassAxisStat.axis_kind.asc(), ClassAxisStat.axis_unit.asc())
        ).all()
        if cached_rows:
            items = []
            for row in cached_rows:
                if excluded == "active":
                    count = int(row.active_count or 0)
                elif excluded == "excluded":
                    count = int(row.excluded_count or 0)
                else:
                    count = int(row.total_count or 0)
                if count <= 0:
                    continue
                items.append(
                    {
                        "axis_kind": row.axis_kind,
                        "axis_unit": row.axis_unit,
                        "count": count,
                    }
                )
            return items

    stmt = select(
        Spectrum.axis_kind,
        Spectrum.axis_unit,
        func.count(Spectrum.id).label("count"),
    )
    stmt = _apply_spectrum_filters(stmt, class_key, excluded, component_count, None, subset_spectrum_ids)
    stmt = stmt.group_by(Spectrum.axis_kind, Spectrum.axis_unit).order_by(Spectrum.axis_kind.asc(), Spectrum.axis_unit.asc())
    rows = session.execute(stmt).all()
    return [
        {
            "axis_kind": row.axis_kind,
            "axis_unit": row.axis_unit,
            "count": int(row.count or 0),
        }
        for row in rows
    ]


def fetch_spectra(
    session: Session,
    class_key: str | None,
    excluded: str,
    component_count: int | None,
    axis_kind: str | None = None,
    subset_spectrum_ids: list[int] | None = None,
    limit: int = 500,
    max_total_points: int | None = None,
) -> tuple[list[dict[str, Any]], bool, int]:
    items: list[dict[str, Any]] = []
    total_points = 0
    after_file_name: str | None = None
    truncated_by_point_budget = False

    while len(items) < limit:
        stmt = select(*SPECTRUM_DETAIL_COLUMNS)
        stmt = _apply_spectrum_filters(stmt, class_key, excluded, component_count, axis_kind, subset_spectrum_ids)
        if after_file_name is not None:
            stmt = stmt.where(Spectrum.file_name > after_file_name)
        batch_limit = min(100, limit - len(items))
        rows = session.execute(
            stmt.order_by(Spectrum.file_name.asc()).limit(batch_limit)
        ).all()
        if not rows:
            break

        for row in rows:
            row_points = int(row.point_count or 0)
            if (
                max_total_points is not None
                and items
                and total_points + row_points > max_total_points
            ):
                truncated_by_point_budget = True
                break
            items.append(spectrum_row_to_dict(row))
            total_points += row_points
            after_file_name = row.file_name
        if truncated_by_point_budget or len(rows) < batch_limit:
            break

    return items, truncated_by_point_budget, total_points


def fetch_recent_excluded(session: Session, limit: int = 50) -> list[dict[str, Any]]:
    stmt = (
        select(*SPECTRUM_DETAIL_COLUMNS)
        .where(Spectrum.is_excluded.is_(True))
        .order_by(Spectrum.excluded_at.desc(), Spectrum.file_name.asc())
        .limit(limit)
    )
    rows = session.execute(stmt).all()
    return [spectrum_row_to_dict(row) for row in rows]


def spectrum_query(
    session: Session,
    class_key: str | None,
    excluded: str,
    component_count: int | None,
    axis_kind: str | None = None,
    subset_spectrum_ids: list[int] | None = None,
) -> Select[tuple[Spectrum]]:
    stmt = select(Spectrum)
    stmt = _apply_spectrum_filters(stmt, class_key, excluded, component_count, axis_kind, subset_spectrum_ids)
    return stmt.order_by(Spectrum.file_name.asc())


def job_to_dict(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "type": job.type,
        "status": job.status,
        "params": decode_json(job.params_json, {}),
        "stats": decode_json(job.stats_json, {}),
        "log_text": job.log_text,
        "progress_message": job.progress_message,
        "total_discovered": job.total_discovered,
        "processed_count": job.processed_count,
        "imported_count": job.imported_count,
        "skipped_count": job.skipped_count,
        "failed_count": job.failed_count,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }


def _apply_spectrum_filters(
    stmt: Select,
    class_key: str | None,
    excluded: str,
    component_count: int | None,
    axis_kind: str | None,
    subset_spectrum_ids: list[int] | None,
) -> Select:
    if class_key:
        stmt = stmt.where(Spectrum.class_key == class_key)
    if excluded == "active":
        stmt = stmt.where(Spectrum.is_excluded.is_(False))
    elif excluded == "excluded":
        stmt = stmt.where(Spectrum.is_excluded.is_(True))
    if component_count is not None:
        stmt = stmt.where(Spectrum.component_count == component_count)
    if axis_kind:
        stmt = stmt.where(Spectrum.axis_kind == axis_kind)
    if subset_spectrum_ids:
        stmt = stmt.where(Spectrum.id.in_(subset_spectrum_ids))
    return stmt


def _cached_total_count(
    session: Session,
    class_key: str | None,
    excluded: str,
    component_count: int | None,
    axis_kind: str | None,
    subset_spectrum_ids: list[int] | None,
) -> int | None:
    if not class_key or component_count is not None or subset_spectrum_ids is not None:
        return None
    if axis_kind is not None:
        rows = session.execute(
            select(
                ClassAxisStat.total_count,
                ClassAxisStat.active_count,
                ClassAxisStat.excluded_count,
            ).where(ClassAxisStat.class_key == class_key, ClassAxisStat.axis_kind == axis_kind)
        ).all()
        if not rows:
            return None
        if excluded == "active":
            return sum(int(row.active_count or 0) for row in rows)
        if excluded == "excluded":
            return sum(int(row.excluded_count or 0) for row in rows)
        return sum(int(row.total_count or 0) for row in rows)

    row = session.get(ClassStat, class_key)
    if row is None:
        return None
    if excluded == "active":
        return int(row.active_count or 0)
    if excluded == "excluded":
        return int(row.excluded_count or 0)
    return int(row.total_count or 0)


def _normalized_csv_digest(raw_bytes: bytes) -> str:
    text = raw_bytes.decode("utf-8-sig")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
