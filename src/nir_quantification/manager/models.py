from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Spectrum(Base):
    __tablename__ = "spectra"
    __table_args__ = (
        Index("ix_spectra_class_excluded_axis_file", "class_key", "is_excluded", "axis_kind", "file_name"),
        Index("ix_spectra_excluded_recent", "is_excluded", "excluded_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    file_name: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    source_path_last_seen: Mapped[str] = mapped_column(Text)
    raw_csv_gzip: Mapped[bytes] = mapped_column(LargeBinary)
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    metadata_json: Mapped[str] = mapped_column(Text)
    axis_kind: Mapped[str] = mapped_column(String(64))
    axis_unit: Mapped[str] = mapped_column(String(64))
    point_count: Mapped[int] = mapped_column(Integer)
    x_values_json: Mapped[str] = mapped_column(Text)
    y_values_json: Mapped[str] = mapped_column(Text)
    labels_json: Mapped[str] = mapped_column(Text)
    class_key: Mapped[str] = mapped_column(String(512), index=True)
    class_display_name: Mapped[str] = mapped_column(String(512), index=True)
    component_count: Mapped[int] = mapped_column(Integer, index=True)
    is_excluded: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    excluded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    components: Mapped[list["SpectrumComponent"]] = relationship(
        back_populates="spectrum",
        cascade="all, delete-orphan",
        lazy="joined",
    )


class SpectrumComponent(Base):
    __tablename__ = "spectrum_components"
    __table_args__ = (UniqueConstraint("spectrum_id", "name", name="uq_spectrum_component_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    spectrum_id: Mapped[int] = mapped_column(ForeignKey("spectra.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    value: Mapped[float] = mapped_column()

    spectrum: Mapped[Spectrum] = relationship(back_populates="components")


class ClassStat(Base):
    __tablename__ = "class_stats"

    class_key: Mapped[str] = mapped_column(String(512), primary_key=True)
    class_display_name: Mapped[str] = mapped_column(String(512), index=True)
    component_count: Mapped[int] = mapped_column(Integer, index=True)
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    active_count: Mapped[int] = mapped_column(Integer, default=0)
    excluded_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ClassAxisStat(Base):
    __tablename__ = "class_axis_stats"

    class_key: Mapped[str] = mapped_column(String(512), primary_key=True)
    axis_kind: Mapped[str] = mapped_column(String(64), primary_key=True)
    axis_unit: Mapped[str] = mapped_column(String(64), primary_key=True)
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    active_count: Mapped[int] = mapped_column(Integer, default=0)
    excluded_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    params_json: Mapped[str] = mapped_column(Text)
    stats_json: Mapped[str] = mapped_column(Text, default="{}")
    log_text: Mapped[str] = mapped_column(Text, default="")
    progress_message: Mapped[str] = mapped_column(Text, default="")
    total_discovered: Mapped[int] = mapped_column(Integer, default=0)
    processed_count: Mapped[int] = mapped_column(Integer, default=0)
    imported_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
