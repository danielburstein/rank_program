"""SQLAlchemy ORM models.

Raw input (images, CSV rows, detections) and derived results (summaries,
conversions, exports) live in separate tables. Derived tables are always
recomputable from retained evidence; evidence is never deleted by recomputes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


# --------------------------------------------------------------------------- people


class Submitter(Base):
    __tablename__ = "submitters"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    name_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    discord_user_id: Mapped[str | None] = mapped_column(String(32), index=True)
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"))
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    images: Mapped[list[SourceImage]] = relationship(back_populates="submitter")


class Player(Base):
    __tablename__ = "players"

    id: Mapped[int] = mapped_column(primary_key=True)
    display_name: Mapped[str] = mapped_column(String(64), index=True)
    name_key: Mapped[str] = mapped_column(String(64), index=True)
    canonical_uuid: Mapped[str | None] = mapped_column(String(36), index=True)
    current_name: Mapped[str | None] = mapped_column(String(64))
    is_bedrock: Mapped[bool] = mapped_column(Boolean, default=False)
    identity_status: Mapped[str] = mapped_column(String(32), default="not_attempted", index=True)
    identity_source: Mapped[str | None] = mapped_column(String(32))
    review_status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    merged_into_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"), index=True)
    ambiguity_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    aliases: Mapped[list[PlayerAlias]] = relationship(back_populates="player", cascade="all, delete-orphan")
    rank_observations: Mapped[list[RankObservation]] = relationship(back_populates="player")
    staff_observations: Mapped[list[StaffObservation]] = relationship(back_populates="player")
    summary: Mapped[PlayerSummary | None] = relationship(back_populates="player", uselist=False)
    conversion: Mapped[Conversion | None] = relationship(back_populates="player", uselist=False)

    @property
    def is_merged(self) -> bool:
        return self.merged_into_id is not None


class PlayerAlias(Base):
    __tablename__ = "player_aliases"
    __table_args__ = (UniqueConstraint("player_id", "username_key", name="uq_alias_player_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    username: Mapped[str] = mapped_column(String(64))
    username_key: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(32), default="extraction")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    player: Mapped[Player] = relationship(back_populates="aliases")


class DiscordLink(Base):
    __tablename__ = "discord_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    discord_user_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    discord_username: Mapped[str] = mapped_column(String(64), default="")
    username: Mapped[str] = mapped_column(String(64))
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"))
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


# --------------------------------------------------------------------------- images


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(16), default="client")  # client | bot | api | cli
    label: Mapped[str] = mapped_column(String(128), default="")
    submitter_id: Mapped[int | None] = mapped_column(ForeignKey("submitters.id"))
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    notes: Mapped[str] = mapped_column(Text, default="")
    image_count: Mapped[int] = mapped_column(Integer, default=0)


class SourceImage(Base):
    __tablename__ = "source_images"

    id: Mapped[int] = mapped_column(primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    phash: Mapped[str | None] = mapped_column(String(32), index=True)
    original_filename: Mapped[str] = mapped_column(String(255), index=True)
    original_path: Mapped[str] = mapped_column(Text, default="")
    byte_size: Mapped[int] = mapped_column(Integer, default=0)
    format: Mapped[str] = mapped_column(String(16), default="")
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    mode: Mapped[str] = mapped_column(String(16), default="")
    frame_count: Mapped[int] = mapped_column(Integer, default=1)
    content: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True)
    storage_ref: Mapped[str | None] = mapped_column(Text)
    import_batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id"), index=True)
    submitter_id: Mapped[int | None] = mapped_column(ForeignKey("submitters.id"), index=True)
    file_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    capture_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    capture_source: Mapped[str] = mapped_column(String(32), default="none")
    capture_reason: Mapped[str] = mapped_column(Text, default="")
    date_status: Mapped[str] = mapped_column(String(16), default="unknown", index=True)
    ai_status: Mapped[str] = mapped_column(String(32), default="no_ai_indicator_found", index=True)
    ai_details: Mapped[str] = mapped_column(Text, default="")
    duplicate_group: Mapped[str] = mapped_column(String(64), index=True, default="")
    availability: Mapped[str] = mapped_column(String(16), default="available")
    extracted_text: Mapped[str] = mapped_column(Text, default="")
    last_extraction_run_id: Mapped[int | None] = mapped_column(Integer)
    review_notes: Mapped[str] = mapped_column(Text, default="")

    submitter: Mapped[Submitter | None] = relationship(back_populates="images")
    metadata_entries: Mapped[list[ImageMetadata]] = relationship(back_populates="image", cascade="all, delete-orphan")
    paths: Mapped[list[ImagePath]] = relationship(back_populates="image", cascade="all, delete-orphan")
    runs: Mapped[list[ExtractionRun]] = relationship(back_populates="image")


class ImagePath(Base):
    __tablename__ = "image_paths"

    id: Mapped[int] = mapped_column(primary_key=True)
    image_id: Mapped[int] = mapped_column(ForeignKey("source_images.id"), index=True)
    path: Mapped[str] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(String(255), index=True)
    import_batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id"))
    submitter_id: Mapped[int | None] = mapped_column(ForeignKey("submitters.id"))
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    image: Mapped[SourceImage] = relationship(back_populates="paths")


class ImageMetadata(Base):
    __tablename__ = "image_metadata"

    id: Mapped[int] = mapped_column(primary_key=True)
    image_id: Mapped[int] = mapped_column(ForeignKey("source_images.id"), index=True)
    key: Mapped[str] = mapped_column(String(128), index=True)
    value: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(16), default="other")  # ai | date | other
    reason: Mapped[str] = mapped_column(Text, default="")

    image: Mapped[SourceImage] = relationship(back_populates="metadata_entries")


# --------------------------------------------------------------------------- extraction


class ExtractionRun(Base):
    __tablename__ = "extraction_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    image_id: Mapped[int] = mapped_column(ForeignKey("source_images.id"), index=True)
    extractor_type: Mapped[str] = mapped_column(String(16))
    engine: Mapped[str] = mapped_column(String(32), default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    version: Mapped[str] = mapped_column(String(32), default="")
    settings_fingerprint: Mapped[str] = mapped_column(String(32), index=True, default="")
    regions: Mapped[list[Any]] = mapped_column(JSON, default=list)
    raw_output: Mapped[str] = mapped_column(Text, default="")
    full_text: Mapped[str] = mapped_column(Text, default="")
    words: Mapped[list[Any]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="ok", index=True)
    error: Mapped[str] = mapped_column(Text, default="")
    warnings: Mapped[list[Any]] = mapped_column(JSON, default=list)
    cost: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    purpose: Mapped[str] = mapped_column(String(16), default="import")  # import | rescan | manual
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    image: Mapped[SourceImage] = relationship(back_populates="runs")
    detections: Mapped[list[Detection]] = relationship(back_populates="run")


class Detection(Base):
    __tablename__ = "detections"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("extraction_runs.id"), index=True)
    image_id: Mapped[int] = mapped_column(ForeignKey("source_images.id"), index=True)
    region: Mapped[str] = mapped_column(String(16), default="unknown")
    source_type: Mapped[str] = mapped_column(String(32), default="screenshot_other")
    credibility: Mapped[str] = mapped_column(String(16), default="normal")
    raw_text: Mapped[str] = mapped_column(Text, default="")
    raw_prefix: Mapped[str] = mapped_column(String(128), default="")
    normalised_prefix: Mapped[str] = mapped_column(String(128), default="", index=True)
    prefix_kind: Mapped[str] = mapped_column(String(32), default="none")
    raw_username: Mapped[str] = mapped_column(String(64), default="")
    normalised_username: Mapped[str] = mapped_column(String(64), default="")
    username_key: Mapped[str] = mapped_column(String(64), default="", index=True)
    username_valid: Mapped[bool] = mapped_column(Boolean, default=False)
    is_bedrock: Mapped[bool] = mapped_column(Boolean, default=False)
    partial: Mapped[bool] = mapped_column(Boolean, default=False)
    unreadable_username: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="candidate", index=True)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    bbox: Mapped[list[Any] | None] = mapped_column(JSON)
    prefix_bbox: Mapped[list[Any] | None] = mapped_column(JSON)
    username_bbox: Mapped[list[Any] | None] = mapped_column(JSON)
    leading_icon: Mapped[str] = mapped_column(String(16), default="none")
    trailing_icon: Mapped[str] = mapped_column(String(16), default="none")
    rank_canonical: Mapped[str | None] = mapped_column(String(32), index=True)
    staff_canonical: Mapped[str | None] = mapped_column(String(32), index=True)
    custom_prefix: Mapped[str | None] = mapped_column(String(128))
    trailing_icon_rank: Mapped[str | None] = mapped_column(String(32))
    yinyang_evidence: Mapped[str | None] = mapped_column(Text)
    misread_of: Mapped[str | None] = mapped_column(String(32))
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    notes: Mapped[list[Any]] = mapped_column(JSON, default=list)
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"), index=True)
    manual: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    superseded_by_id: Mapped[int | None] = mapped_column(ForeignKey("detections.id"))

    run: Mapped[ExtractionRun | None] = relationship(back_populates="detections")


# --------------------------------------------------------------------------- observations


class RankObservation(Base):
    __tablename__ = "rank_observations"

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    detection_id: Mapped[int | None] = mapped_column(ForeignKey("detections.id"), index=True)
    image_id: Mapped[int | None] = mapped_column(ForeignKey("source_images.id"), index=True)
    csv_row_id: Mapped[int | None] = mapped_column(ForeignKey("csv_rows.id"), index=True)
    manual_assertion_id: Mapped[int | None] = mapped_column(ForeignKey("manual_assertions.id"))
    source_type: Mapped[str] = mapped_column(String(32), index=True)
    basis: Mapped[str] = mapped_column(String(16), default="prefix")
    raw_text: Mapped[str] = mapped_column(String(128), default="")
    rank_canonical: Mapped[str] = mapped_column(String(32), index=True)
    rank_priority: Mapped[int] = mapped_column(Integer, default=0)
    custom_prefix: Mapped[str | None] = mapped_column(String(128))
    credibility: Mapped[str] = mapped_column(String(16), default="normal")
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    review_status: Mapped[str] = mapped_column(String(16), default="not_required", index=True)
    manually_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    verified_by: Mapped[str | None] = mapped_column(String(64))
    verified_note: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    deactivated_reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    player: Mapped[Player] = relationship(back_populates="rank_observations")


class StaffObservation(Base):
    __tablename__ = "staff_observations"

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    detection_id: Mapped[int | None] = mapped_column(ForeignKey("detections.id"), index=True)
    image_id: Mapped[int | None] = mapped_column(ForeignKey("source_images.id"), index=True)
    raw_text: Mapped[str] = mapped_column(String(128), default="")
    staff_canonical: Mapped[str] = mapped_column(String(32), index=True)
    has_yinyang: Mapped[bool] = mapped_column(Boolean, default=False)
    review_status: Mapped[str] = mapped_column(String(16), default="not_required")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    player: Mapped[Player] = relationship(back_populates="staff_observations")


# --------------------------------------------------------------------------- trusted CSV


class CsvBatch(Base):
    __tablename__ = "csv_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    delimiter: Mapped[str] = mapped_column(String(4), default=",")
    encoding: Mapped[str] = mapped_column(String(16), default="utf-8")
    header_mapping: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    valid_count: Mapped[int] = mapped_column(Integer, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(16), default="file")  # file | manual
    imported_by: Mapped[str] = mapped_column(String(64), default="")
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    notes: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    rows: Mapped[list[CsvRow]] = relationship(back_populates="batch", cascade="all, delete-orphan")


class CsvRow(Base):
    __tablename__ = "csv_rows"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("csv_batches.id"), index=True)
    row_number: Mapped[int] = mapped_column(Integer)
    raw_row: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    row_hash: Mapped[str] = mapped_column(String(64), index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    username_key: Mapped[str | None] = mapped_column(String(64), index=True)
    uuid: Mapped[str | None] = mapped_column(String(36), index=True)
    rank_raw: Mapped[str] = mapped_column(String(64), default="")
    rank_canonical: Mapped[str | None] = mapped_column(String(32), index=True)
    purchase_date: Mapped[str | None] = mapped_column(String(32))
    transaction_id: Mapped[str | None] = mapped_column(String(128))
    validation_status: Mapped[str] = mapped_column(String(16), default="valid", index=True)  # valid | invalid | duplicate
    validation_errors: Mapped[list[Any]] = mapped_column(JSON, default=list)
    duplicate_of_id: Mapped[int | None] = mapped_column(ForeignKey("csv_rows.id"))
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    deactivated_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    batch: Mapped[CsvBatch] = relationship(back_populates="rows")


# --------------------------------------------------------------------------- manual / review


class ManualAssertion(Base):
    __tablename__ = "manual_assertions"

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)  # rank | staff | uuid | alias | ai_review | date_review | verification | waiver | identity
    value: Mapped[str] = mapped_column(String(255), default="")
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    reason: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    supersedes_id: Mapped[int | None] = mapped_column(ForeignKey("manual_assertions.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class Flag(Base):
    __tablename__ = "flags"
    __table_args__ = (Index("ix_flags_entity", "entity_type", "entity_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(16))  # player | image | detection | observation | csv_row | lookup
    entity_id: Mapped[int] = mapped_column(Integer)
    code: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(16), default="warning")
    details: Mapped[str] = mapped_column(Text, default="")
    evidence_ref: Mapped[str | None] = mapped_column(String(128))
    source: Mapped[str] = mapped_column(String(16), default="auto")  # auto | manual
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[str | None] = mapped_column(String(64))
    resolution_note: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str] = mapped_column(Text, default="")


class VerificationWaiver(Base):
    __tablename__ = "verification_waivers"

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    rank_canonical: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by: Mapped[str | None] = mapped_column(String(64))


# --------------------------------------------------------------------------- derived results


class PlayerSummary(Base):
    __tablename__ = "player_summaries"

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), unique=True, index=True)
    effective_rank: Mapped[str | None] = mapped_column(String(32), index=True)
    highest_observed: Mapped[str | None] = mapped_column(String(32), index=True)
    highest_confirmed: Mapped[str | None] = mapped_column(String(32))
    observed_ranks: Mapped[list[Any]] = mapped_column(JSON, default=list)
    staff_ranks: Mapped[list[Any]] = mapped_column(JSON, default=list)
    custom_prefixes: Mapped[list[Any]] = mapped_column(JSON, default=list)
    provisional: Mapped[list[Any]] = mapped_column(JSON, default=list)
    candidates: Mapped[list[Any]] = mapped_column(JSON, default=list)
    active_flags: Mapped[list[Any]] = mapped_column(JSON, default=list)
    reasoning: Mapped[list[Any]] = mapped_column(JSON, default=list)
    clean_rank_images: Mapped[int] = mapped_column(Integer, default=0)
    distinct_submitters: Mapped[int] = mapped_column(Integer, default=0)
    ai_rank_images: Mapped[int] = mapped_column(Integer, default=0)
    csv_records: Mapped[int] = mapped_column(Integer, default=0)
    manual_override_active: Mapped[bool] = mapped_column(Boolean, default=False)
    has_provisional: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    policy_fingerprint: Mapped[str] = mapped_column(String(32), default="")
    policy_version_id: Mapped[int | None] = mapped_column(ForeignKey("policy_versions.id"))
    verification_settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    compact_text: Mapped[str] = mapped_column(Text, default="")
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    stale: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    player: Mapped[Player] = relationship(back_populates="summary")


class VerificationResult(Base):
    __tablename__ = "verification_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    rank_canonical: Mapped[str] = mapped_column(String(32), index=True)
    required_sources: Mapped[int] = mapped_column(Integer, default=1)
    counted_sources: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), index=True)
    confirmed_by: Mapped[list[Any]] = mapped_column(JSON, default=list)
    fallback_rank: Mapped[str | None] = mapped_column(String(32))
    notes: Mapped[list[Any]] = mapped_column(JSON, default=list)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    sources: Mapped[list[VerificationSource]] = relationship(back_populates="result", cascade="all, delete-orphan")


class VerificationSource(Base):
    __tablename__ = "verification_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    result_id: Mapped[int] = mapped_column(ForeignKey("verification_results.id"), index=True)
    observation_id: Mapped[int | None] = mapped_column(ForeignKey("rank_observations.id"))
    image_id: Mapped[int | None] = mapped_column(ForeignKey("source_images.id"))
    submitter_id: Mapped[int | None] = mapped_column(ForeignKey("submitters.id"))
    submitter_key: Mapped[str] = mapped_column(String(64), default="")
    submitter_name: Mapped[str] = mapped_column(String(64), default="")
    eligible: Mapped[bool] = mapped_column(Boolean, default=False)
    counted: Mapped[bool] = mapped_column(Boolean, default=False)
    source_key: Mapped[str | None] = mapped_column(String(64))
    reasons: Mapped[list[Any]] = mapped_column(JSON, default=list)

    result: Mapped[VerificationResult] = relationship(back_populates="sources")


class Conversion(Base):
    __tablename__ = "conversions"

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), unique=True, index=True)
    old_rank: Mapped[str | None] = mapped_column(String(32))
    new_rank: Mapped[str | None] = mapped_column(String(64), index=True)
    included: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    reasons: Mapped[list[Any]] = mapped_column(JSON, default=list)
    inherited_flags: Mapped[list[Any]] = mapped_column(JSON, default=list)
    excluded_higher_ranks: Mapped[list[Any]] = mapped_column(JSON, default=list)
    mapping_fingerprint: Mapped[str] = mapped_column(String(32), default="")
    mapping_version_id: Mapped[int | None] = mapped_column(ForeignKey("policy_versions.id"))
    flag_policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    input_mode: Mapped[str] = mapped_column(String(16), default="mode1")
    manual_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    stale: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    player: Mapped[Player] = relationship(back_populates="conversion")


# --------------------------------------------------------------------------- identity


class LookupAttemptRow(Base):
    __tablename__ = "lookup_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"), index=True)
    queried_name: Mapped[str] = mapped_column(String(64), index=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    attempt_no: Mapped[int] = mapped_column(Integer, default=1)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    elapsed_ms: Mapped[int] = mapped_column(Integer, default=0)
    http_status: Mapped[int | None] = mapped_column(Integer, index=True)
    outcome: Mapped[str] = mapped_column(String(16), index=True)
    failure_code: Mapped[str | None] = mapped_column(String(64), index=True)
    error_message: Mapped[str] = mapped_column(Text, default="")
    proxy_id: Mapped[str | None] = mapped_column(String(128), index=True)
    resolved_uuid: Mapped[str | None] = mapped_column(String(36))
    resolved_name: Mapped[str | None] = mapped_column(String(64))
    response_valid: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_excerpt: Mapped[str] = mapped_column(Text, default="")
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"))


class LookupCache(Base):
    __tablename__ = "lookup_cache"
    __table_args__ = (UniqueConstraint("provider", "name_key", name="uq_lookup_cache"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    name_key: Mapped[str] = mapped_column(String(64), index=True)
    uuid: Mapped[str | None] = mapped_column(String(36))
    current_name: Mapped[str | None] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(16))
    failure_code: Mapped[str | None] = mapped_column(String(64))
    is_historical: Mapped[bool] = mapped_column(Boolean, default=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IdentityResolution(Base):
    __tablename__ = "identity_resolutions"

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    uuid: Mapped[str | None] = mapped_column(String(36), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    source: Mapped[str] = mapped_column(String(32))  # mojang | geyser | csv | manual | merge | historical
    queried_name: Mapped[str | None] = mapped_column(String(64))
    current_name: Mapped[str | None] = mapped_column(String(64))
    lookup_attempt_id: Mapped[int | None] = mapped_column(ForeignKey("lookup_attempts.id"))
    csv_row_id: Mapped[int | None] = mapped_column(ForeignKey("csv_rows.id"))
    manual_assertion_id: Mapped[int | None] = mapped_column(ForeignKey("manual_assertions.id"))
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class IdentityMerge(Base):
    __tablename__ = "identity_merges"

    id: Mapped[int] = mapped_column(primary_key=True)
    survivor_player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    merged_player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --------------------------------------------------------------------------- export


class ExportBatch(Base):
    __tablename__ = "export_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    filename: Mapped[str] = mapped_column(String(255), default="")
    file_path: Mapped[str] = mapped_column(Text, default="")
    options: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    command_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    policy_fingerprint: Mapped[str] = mapped_column(String(32), default="")
    mapping_fingerprint: Mapped[str] = mapped_column(String(32), default="")
    staff_warning_acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
    stale: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    stale_reason: Mapped[str] = mapped_column(Text, default="")
    content: Mapped[str] = mapped_column(Text, default="")

    rows: Mapped[list[ExportRow]] = relationship(back_populates="batch", cascade="all, delete-orphan")


class ExportRow(Base):
    __tablename__ = "export_rows"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("export_batches.id"), index=True)
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"), index=True)
    uuid: Mapped[str | None] = mapped_column(String(36), index=True)
    old_rank: Mapped[str | None] = mapped_column(String(32))
    new_rank: Mapped[str | None] = mapped_column(String(64))
    command: Mapped[str | None] = mapped_column(Text)
    included: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    skip_reason: Mapped[str] = mapped_column(Text, default="")

    batch: Mapped[ExportBatch] = relationship(back_populates="rows")


# --------------------------------------------------------------------------- jobs / settings / audit


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    label: Mapped[str] = mapped_column(String(255), default="")
    total: Mapped[int] = mapped_column(Integer, default=0)
    done: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    progress_message: Mapped[str] = mapped_column(Text, default="")
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    pause_requested: Mapped[bool] = mapped_column(Boolean, default=False)

    errors: Mapped[list[JobError]] = relationship(back_populates="job", cascade="all, delete-orphan")


class JobError(Base):
    __tablename__ = "job_errors"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    item_ref: Mapped[str] = mapped_column(String(255), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    traceback: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    job: Mapped[Job] = relationship(back_populates="errors")


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    updated_by: Mapped[str] = mapped_column(String(64), default="")


class SettingHistory(Base):
    __tablename__ = "settings_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(128), index=True)
    old_value: Mapped[Any] = mapped_column(JSON, nullable=True)
    new_value: Mapped[Any] = mapped_column(JSON, nullable=True)
    changed_by: Mapped[str] = mapped_column(String(64), default="")
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reason: Mapped[str] = mapped_column(Text, default="")


class PolicyVersion(Base):
    __tablename__ = "policy_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)  # rank_policy | mapping | verification
    version: Mapped[int] = mapped_column(Integer)
    fingerprint: Mapped[str] = mapped_column(String(32), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    reason: Mapped[str] = mapped_column(Text, default="")


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    key_prefix: Mapped[str] = mapped_column(String(12), default="")
    role: Mapped[str] = mapped_column(String(16), default="readonly")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_entity", "entity_type", "entity_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(64), index=True)
    entity_type: Mapped[str] = mapped_column(String(32), default="")
    entity_id: Mapped[int | None] = mapped_column(Integer)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
