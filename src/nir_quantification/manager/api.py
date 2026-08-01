from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from .config import ManagerSettings
from .db import decode_json, session_scope
from .jobs import JobManager
from .models import Job, Spectrum
from .service import (
    class_stats_status,
    fetch_recent_excluded,
    fetch_spectra,
    job_to_dict,
    list_classes,
    spectra_summary,
    spectrum_to_dict,
    transition_spectrum_exclusion,
)


class ImportJobRequest(BaseModel):
    root_path: str
    recursive: bool = True
    validate_labels: bool = True


class ExportJobRequest(BaseModel):
    export_root: str
    scope: Literal["active", "excluded", "all"] = "active"
    class_keys: list[str] = Field(default_factory=list)


class SubsetRequest(BaseModel):
    mode: Literal["count", "ratio"]
    parts: int | None = None
    ratios: list[float] | None = None


def create_router(settings: ManagerSettings, session_factory: sessionmaker, job_manager: JobManager) -> APIRouter:
    router = APIRouter(prefix="/api")

    def get_session():
        with session_scope(session_factory) as session:
            yield session

    @router.get("/health")
    def health(session: Session = Depends(get_session)) -> dict:
        session.execute(select(1))
        return {"status": "ok", "database": "ok", "jobs": "accepting"}

    @router.get("/fs/roots")
    def roots() -> dict:
        return {
            "import_roots": [str(path) for path in settings.import_roots],
            "export_roots": [str(path) for path in settings.export_roots],
        }

    @router.get("/fs/browse")
    def browse(kind: Literal["import", "export"], path: str | None = None) -> dict:
        allowed_roots = settings.import_roots if kind == "import" else settings.export_roots
        target = _resolve_allowed_path(path, allowed_roots) if path else None
        if target is None:
            return {
                "entries": [
                    {
                        "name": root.name or str(root),
                        "path": str(root),
                        "is_dir": True,
                    }
                    for root in allowed_roots
                ]
            }
        if not target.exists():
            raise HTTPException(status_code=404, detail="path not found")
        parent_path = None
        for root in allowed_roots:
            try:
                target.relative_to(root)
                if target != root:
                    parent_path = str(target.parent.resolve())
                break
            except ValueError:
                continue
        entries = []
        for child in sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            if child.is_dir():
                entries.append({"name": child.name, "path": str(child.resolve()), "is_dir": True})
        return {"current_path": str(target), "parent_path": parent_path, "entries": entries}

    @router.post("/import-jobs")
    def create_import_job(payload: ImportJobRequest) -> dict:
        root_path = _resolve_allowed_path(payload.root_path, settings.import_roots)
        if root_path is None or not root_path.exists():
            raise HTTPException(status_code=400, detail="import root is not accessible")
        return job_manager.create_import_job(
            root_path=root_path,
            recursive=payload.recursive,
            validate_labels=payload.validate_labels,
        )

    @router.post("/export-jobs")
    def create_export_job(payload: ExportJobRequest) -> dict:
        export_root = _resolve_allowed_path(payload.export_root, settings.export_roots)
        if export_root is None:
            raise HTTPException(status_code=400, detail="export root is not accessible")
        export_root.mkdir(parents=True, exist_ok=True)
        return job_manager.create_export_job(export_root=export_root, scope=payload.scope, class_keys=payload.class_keys)

    @router.get("/jobs/{job_id}")
    def get_job(job_id: int) -> dict:
        try:
            return job_manager.get_job(job_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @router.get("/jobs/{job_id}/events")
    async def stream_job(job_id: int):
        async def event_stream():
            last_payload = None
            while True:
                try:
                    payload = job_manager.get_job(job_id)
                except KeyError:
                    yield "event: error\ndata: {}\n\n"
                    return
                serialized = json.dumps(payload, ensure_ascii=False)
                if serialized != last_payload:
                    yield f"data: {serialized}\n\n"
                    last_payload = serialized
                if payload["status"] in {"completed", "failed"}:
                    return
                await asyncio.sleep(1.0)

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @router.get("/classes")
    def classes(
        sort: Literal["count", "component_count", "name"] = "count",
        session: Session = Depends(get_session),
    ) -> dict:
        return {
            "items": list_classes(session, sort_by=sort),
            "meta": class_stats_status(session),
        }

    @router.get("/spectra/summary")
    def spectra_summary_endpoint(
        class_key: str | None = None,
        excluded: Literal["active", "excluded", "all"] = "active",
        component_count: int | None = None,
        subset_id: str | None = None,
        session: Session = Depends(get_session),
    ) -> dict:
        if subset_id:
            subset_summary = job_manager.subsets.get_summary(subset_id, excluded)
            if subset_summary is None:
                raise HTTPException(status_code=404, detail="subset not found")
            payload = subset_summary
        else:
            payload = spectra_summary(session, class_key, excluded, component_count, None, None)
        payload.update(class_stats_status(session))
        return payload

    @router.get("/spectra")
    def spectra(
        class_key: str | None = None,
        excluded: Literal["active", "excluded", "all"] = "active",
        component_count: int | None = None,
        axis_kind: Literal["wavelength", "wavenumber"] | None = None,
        subset_id: str | None = None,
        limit: Annotated[int, Query(ge=1, le=2000)] = 500,
        session: Session = Depends(get_session),
    ) -> dict:
        subset_spectrum_ids: list[int] | None = None
        if subset_id:
            subset = job_manager.subsets.get(subset_id)
            if subset is None:
                raise HTTPException(status_code=404, detail="subset not found")
            subset_spectrum_ids = list(subset["spectrum_ids"])
        summary = spectra_summary(session, class_key, excluded, component_count, axis_kind, subset_spectrum_ids)
        spectra_items, point_budget_truncated, returned_point_count = fetch_spectra(
            session,
            class_key=class_key,
            excluded=excluded,
            component_count=component_count,
            axis_kind=axis_kind,
            subset_spectrum_ids=subset_spectrum_ids,
            limit=limit,
            max_total_points=settings.max_preview_points,
        )
        all_axes_summary = summary["axis_summary"]
        if axis_kind is not None:
            all_axes_summary = spectra_summary(
                session,
                class_key,
                excluded,
                component_count,
                None,
                subset_spectrum_ids,
            )["axis_summary"]
        return {
            "items": spectra_items,
            "count": summary["total_count"],
            "limit": limit,
            "axis_summary": all_axes_summary,
            "point_budget": settings.max_preview_points,
            "returned_point_count": returned_point_count,
            "truncated_by_point_budget": point_budget_truncated,
        }

    @router.post("/spectra/{spectrum_id}/exclude")
    def exclude_spectrum(spectrum_id: int, session: Session = Depends(get_session)) -> dict:
        spectrum, changed = transition_spectrum_exclusion(session, spectrum_id, excluded=True)
        if spectrum is None:
            raise HTTPException(status_code=404, detail="spectrum not found")
        if changed:
            job_manager.subsets.adjust_for_exclusion(spectrum, excluded=True)
        return spectrum_to_dict(spectrum)

    @router.post("/spectra/{spectrum_id}/restore")
    def restore_spectrum(spectrum_id: int, session: Session = Depends(get_session)) -> dict:
        spectrum, changed = transition_spectrum_exclusion(session, spectrum_id, excluded=False)
        if spectrum is None:
            raise HTTPException(status_code=404, detail="spectrum not found")
        if changed:
            job_manager.subsets.adjust_for_exclusion(spectrum, excluded=False)
        return spectrum_to_dict(spectrum)

    @router.post("/classes/{class_key:path}/subsets")
    def subsets(class_key: str, payload: SubsetRequest, session: Session = Depends(get_session)) -> dict:
        spectra_items = session.execute(
            select(
                Spectrum.id,
                Spectrum.file_name,
                Spectrum.axis_kind,
                Spectrum.axis_unit,
                Spectrum.is_excluded,
            )
            .where(Spectrum.class_key == class_key)
            .order_by(Spectrum.file_name.asc())
        ).all()
        spectra_rows = [
            {
                "id": int(item.id),
                "file_name": item.file_name,
                "axis_kind": item.axis_kind,
                "axis_unit": item.axis_unit,
                "is_excluded": bool(item.is_excluded),
            }
            for item in spectra_items
        ]
        if not spectra_rows:
            raise HTTPException(status_code=404, detail="class has no spectra")
        return job_manager.create_subset(
            class_key=class_key,
            mode=payload.mode,
            parts=payload.parts,
            ratios=payload.ratios,
            spectra_rows=spectra_rows,
        )

    @router.get("/excluded/recent")
    def recent_excluded(limit: Annotated[int, Query(ge=1, le=200)] = 50, session: Session = Depends(get_session)) -> dict:
        return {"items": fetch_recent_excluded(session, limit=limit)}

    @router.get("/jobs")
    def list_jobs(limit: Annotated[int, Query(ge=1, le=200)] = 20, session: Session = Depends(get_session)) -> dict:
        stmt = select(Job).order_by(Job.created_at.desc()).limit(limit)
        jobs = session.scalars(stmt).all()
        return {"items": [job_to_dict(job) for job in jobs]}

    return router


def _resolve_allowed_path(raw_path: str | None, roots: list[Path]) -> Path | None:
    if raw_path is None:
        return None
    target = Path(raw_path).expanduser().resolve()
    for root in roots:
        try:
            target.relative_to(root)
            return target
        except ValueError:
            continue
    return None
