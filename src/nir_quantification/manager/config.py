from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _split_paths(raw: str | None) -> list[Path]:
    if not raw:
        return []
    return [Path(part).expanduser().resolve() for part in raw.split(os.pathsep) if part.strip()]


@dataclass(slots=True)
class ManagerSettings:
    db_path: Path
    import_roots: list[Path]
    export_roots: list[Path]
    static_dir: Path | None
    max_workers: int
    job_batch_size: int
    max_concurrent_jobs: int = 1
    subset_cache_max_entries: int = 256
    subset_cache_ttl_seconds: int = 3600
    max_preview_points: int = 500_000

    def __post_init__(self) -> None:
        self.db_path = self.db_path.expanduser().resolve()
        self.import_roots = [path.expanduser().resolve() for path in self.import_roots]
        self.export_roots = [path.expanduser().resolve() for path in self.export_roots]
        if self.static_dir is not None:
            self.static_dir = self.static_dir.expanduser().resolve()

    @classmethod
    def from_env(cls, base_dir: Path | None = None) -> "ManagerSettings":
        base_dir = (base_dir or Path.cwd()).resolve()
        db_path = Path(os.environ.get("NIRQ_DB_PATH", str(base_dir / "data" / "spectra.sqlite3"))).expanduser().resolve()
        import_roots = _split_paths(os.environ.get("IMPORT_ROOTS")) or [base_dir]
        export_roots = _split_paths(os.environ.get("EXPORT_ROOTS")) or [base_dir / "exports"]
        static_env = os.environ.get("NIRQ_STATIC_DIR")
        static_dir = Path(static_env).expanduser().resolve() if static_env else (base_dir / "frontend" / "dist")
        max_workers = max(2, min(8, os.cpu_count() or 4))
        return cls(
            db_path=db_path,
            import_roots=import_roots,
            export_roots=export_roots,
            static_dir=static_dir if static_dir.exists() else None,
            max_workers=max_workers,
            job_batch_size=50,
            max_concurrent_jobs=max(1, int(os.environ.get("NIRQ_MAX_CONCURRENT_JOBS", "1"))),
            subset_cache_max_entries=max(1, int(os.environ.get("NIRQ_SUBSET_CACHE_MAX_ENTRIES", "256"))),
            subset_cache_ttl_seconds=max(60, int(os.environ.get("NIRQ_SUBSET_CACHE_TTL_SECONDS", "3600"))),
            max_preview_points=max(10_000, int(os.environ.get("NIRQ_MAX_PREVIEW_POINTS", "500000"))),
        )
