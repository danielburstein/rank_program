"""Enumerations shared across the domain, database and API layers."""

from __future__ import annotations

from enum import StrEnum


class SourceType(StrEnum):
    """Where a rank observation came from."""

    TAB = "tab"
    CHAT = "chat"
    NAMETAG = "nametag"
    SCOREBOARD = "scoreboard"
    SCREENSHOT_OTHER = "screenshot_other"
    TRUSTED_CSV = "trusted_csv"
    MANUAL_SCREENSHOT = "manual_screenshot"  # manual correction tied to a screenshot
    MANUAL_OVERRIDE = "manual_override"  # explicit override with no screenshot


class PrefixKind(StrEnum):
    """Classification of a detected prefix string."""

    PLAYER_RANK = "player_rank"
    SEASONAL_ALIAS = "seasonal_alias"
    STAFF = "staff"
    CUSTOM = "custom"  # SCUFFED custom prefix
    PROBABLE_MISREAD = "probable_misread"
    UNREADABLE = "unreadable"
    NONE = "none"


class DetectionStatus(StrEnum):
    CANDIDATE = "candidate"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    PARTIAL = "partial"
    UNREADABLE = "unreadable"
    CONTEXT_ONLY = "context_only"  # username seen in a non-rank context (join message, mention)
    NEEDS_REVIEW = "needs_review"


class ReviewStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    NOT_REQUIRED = "not_required"


class Credibility(StrEnum):
    """Relative credibility of an observation source."""

    AUTHORITATIVE = "authoritative"  # trusted purchase CSV
    HIGH = "high"  # TAB list, nametag
    NORMAL = "normal"  # chat line with prefix
    LOW = "low"  # scoreboard repeat
    MANUAL = "manual"


class DateStatus(StrEnum):
    OK = "ok"
    OUT_OF_DATE = "out_of_date"
    UNKNOWN = "unknown"


class DateSource(StrEnum):
    EMBEDDED_METADATA = "embedded_metadata"
    FILENAME_PATTERN = "filename_pattern"
    FILE_MODIFIED = "file_modified"
    IMPORT_TIME = "import_time"
    MANUAL = "manual"
    NONE = "none"


class AiStatus(StrEnum):
    AI_INDICATOR_FOUND = "ai_indicator_found"
    AI_FILENAME_INDICATOR = "ai_filename_indicator"  # weaker evidence
    NO_AI_INDICATOR_FOUND = "no_ai_indicator_found"
    MANUALLY_CLEARED = "manually_cleared"
    MANUALLY_FLAGGED = "manually_flagged"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class VerificationStatus(StrEnum):
    CONFIRMED = "confirmed"
    PROVISIONAL = "provisional"
    NOT_APPLICABLE = "not_applicable"
    WAIVED = "waived"


class IdentityStatus(StrEnum):
    VERIFIED = "verified"  # UUID verified via provider or trusted CSV
    MANUAL = "manual"
    UNRESOLVED = "unresolved"
    AMBIGUOUS = "ambiguous"
    LOOKUP_FAILED = "lookup_failed"
    BEDROCK_PENDING = "bedrock_pending"
    NOT_ATTEMPTED = "not_attempted"


class LookupOutcome(StrEnum):
    RESOLVED = "resolved"
    NOT_FOUND = "not_found"
    FAILED = "failed"
    SKIPPED = "skipped"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExtractorType(StrEnum):
    VISION = "vision"
    OCR = "ocr"
    MANUAL = "manual"


class RegionKind(StrEnum):
    TAB = "tab"
    CHAT = "chat"
    NAMETAG = "nametag"
    SCOREBOARD = "scoreboard"
    FULL = "full"
    UNKNOWN = "unknown"


class TrailingIcon(StrEnum):
    NONE = "none"
    YINYANG = "yinyang"
    INFINITY = "infinity"
    UNKNOWN = "unknown"


class LeadingIcon(StrEnum):
    NONE = "none"
    DIAMOND = "diamond"
    YINYANG = "yinyang"
    INFINITY = "infinity"
    OTHER = "other"


class ApiRole(StrEnum):
    ADMIN = "admin"
    STAFF = "staff"
    BOT = "bot"
    READONLY = "readonly"
