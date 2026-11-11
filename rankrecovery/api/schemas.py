"""Pydantic request bodies."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class Note(BaseModel):
    note: str = Field(min_length=1)


class ReviewDetection(BaseModel):
    accept: bool
    note: str = Field(min_length=1)


class BulkReview(BaseModel):
    detection_ids: list[int]
    accept: bool
    note: str = Field(min_length=1)


class VerifyObservation(BaseModel):
    verified: bool = True
    note: str = Field(min_length=1)


class RankOverride(BaseModel):
    rank: str
    reason: str = Field(min_length=1)


class StaffAssertion(BaseModel):
    staff: str
    reason: str = Field(min_length=1)


class WaiverRequest(BaseModel):
    rank: str
    reason: str = Field(min_length=1)


class ManualUuid(BaseModel):
    uuid: str | None = None
    reason: str = Field(min_length=1)


class AliasRequest(BaseModel):
    username: str
    reason: str = ""


class FlagCreate(BaseModel):
    entity_type: str
    entity_id: int
    code: str
    details: str = ""
    note: str = ""


class SubmitterUpdate(BaseModel):
    submitter: str | None
    reason: str = Field(min_length=1)


class AiReview(BaseModel):
    flagged: bool
    reason: str = Field(min_length=1)


class DateReview(BaseModel):
    capture_at: str | None = None  # ISO datetime or null for "unknown"
    reason: str = Field(min_length=1)


class JobCreate(BaseModel):
    kind: str
    params: dict[str, Any] = Field(default_factory=dict)
    label: str = ""


class ExtractRequest(BaseModel):
    image_ids: list[int] | None = None
    force: bool = False
    extractor: str | None = None  # auto | vision | ocr


class RecomputeRequest(BaseModel):
    player_ids: list[int] | None = None
    all: bool = False
    then_convert: bool = True


class LookupRequest(BaseModel):
    player_ids: list[int] | None = None
    all: bool = False
    retry_failed: bool = False
    retry_proxy_failures: bool = False
    force: bool = False
    workers: int | None = None


class MergeRequest(BaseModel):
    survivor_id: int
    merged_id: int
    reason: str = Field(min_length=1)
    allow_uuid_conflict: bool = False


class ConversionApproval(BaseModel):
    approved: bool
    reason: str = Field(min_length=1)


class ExportWrite(BaseModel):
    path: str | None = None
    staff_warning_acknowledged: bool = False
    options: dict[str, Any] = Field(default_factory=dict)


class SearchBody(BaseModel):
    text: str = ""
    exact: bool = False
    entity_types: list[str] = Field(default_factory=lambda: ["player", "image", "detection", "csv_row", "lookup"])
    rank: str | None = None
    highest_rank: str | None = None
    provisional_only: bool = False
    staff: str | None = None
    custom_prefix: str | None = None
    flag_code: str | None = None
    flag_active: bool | None = True
    submitter: str | None = None
    ai_status: str | None = None
    date_status: str | None = None
    identity_status: str | None = None
    export_included: bool | None = None
    lookup_failure_code: str | None = None
    http_status: int | None = None
    min_sources: int | None = None
    limit: int = 200
    offset: int = 0


class RescanCandidates(BaseModel):
    username: str
    mode: str = "all"
    player_id: int | None = None
    batch_ids: list[int] = Field(default_factory=list)
    submitter_names: list[str] = Field(default_factory=list)
    image_ids: list[int] = Field(default_factory=list)


class RescanRun(RescanCandidates):
    full: bool = False
    extractor: str | None = None


class ManualCorrection(BaseModel):
    image_id: int
    username: str
    prefix_text: str | None = None
    rank: str | None = None
    staff: str | None = None
    region: str = "tab"
    username_bbox: list[int] | None = None
    prefix_bbox: list[int] | None = None
    trailing_icon: str = "none"
    leading_icon: str = "none"
    note: str = Field(min_length=1)
    supersedes_detection_id: int | None = None


class SettingsUpdate(BaseModel):
    values: dict[str, Any]
    reason: str = ""


class SettingsReset(BaseModel):
    keys: list[str] = Field(default_factory=list)
    prefix: str | None = None
    confirm: bool = False


class CsvManualRow(BaseModel):
    username: str | None = None
    uuid: str | None = None
    rank: str
    purchase_date: str | None = None
    transaction_id: str | None = None
    reason: str = Field(min_length=1)


class CsvRowAction(BaseModel):
    reason: str = Field(min_length=1)


class DiscordLinkBody(BaseModel):
    discord_user_id: str
    discord_username: str = ""
    username: str
    verified: bool = False


class ClaimCreate(BaseModel):
    old_username: str
    current_username: str
    discord_user_id: str | None = None
    discord_username: str | None = None
    note: str = ""


class ClaimDecision(BaseModel):
    note: str = ""
    force: bool = False


class LinkNames(BaseModel):
    old_username: str
    current_username: str
    note: str = ""
    force: bool = False


class ReviewUuid(BaseModel):
    uuid: str = Field(min_length=32, max_length=36)
    note: str = ""


class ReviewSkip(BaseModel):
    reason: str = ""


class IngestFolderBody(BaseModel):
    submitter: str | None = None
    extract: bool = True


class DriveImportBody(BaseModel):
    url: str
    submitter: str | None = None
    ingest: bool = True


class ApiKeyCreate(BaseModel):
    name: str
    role: str = "readonly"


class RestoreBody(BaseModel):
    backup_file: str
    confirm: bool = False


class ReviewStatusBody(BaseModel):
    status: str
    note: str = ""
