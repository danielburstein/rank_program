"""Catalogue of structured flags.

Every flag the application can raise is defined here with a default severity
and description. Flags are stored as individual rows, never as a joined string.
"""

from __future__ import annotations

from dataclasses import dataclass

from rankrecovery.core.enums import Severity


@dataclass(frozen=True, slots=True)
class FlagDefinition:
    code: str
    severity: Severity
    description: str
    blocks_export_by_default: bool = True


_DEFINITIONS: list[FlagDefinition] = [
    # Mode 1 player flags
    FlagDefinition("NAME_READ_RANK_NOT_FOUND", Severity.WARNING, "A username was read but no rank prefix was associated with it."),
    FlagDefinition("ONLY_STAFF_RANK_DETECTED", Severity.WARNING, "Only a staff prefix was detected; no ordinary player rank is known."),
    FlagDefinition("STAFF_AND_PLAYER_RANK_FOUND", Severity.INFO, "Both staff evidence and an ordinary player rank exist.", False),
    FlagDefinition("AI_DETECTED_FOR_ALL_RANK_IMAGES", Severity.ERROR, "Every rank-bearing screenshot carries an AI-generation indicator."),
    FlagDefinition("AI_DETECTED_HIGHER_RANK", Severity.WARNING, "A higher rank appears only in AI-flagged screenshots."),
    FlagDefinition("AI_INDICATOR_FOUND", Severity.WARNING, "Image metadata contains a known AI-generator indicator."),
    FlagDefinition("AI_FILENAME_INDICATOR", Severity.WARNING, "Filename matches a known AI naming pattern (weak evidence)."),
    FlagDefinition("OUT_OF_DATE", Severity.WARNING, "Reliable date evidence places the image after the configured cutoff."),
    FlagDefinition("DATE_UNKNOWN", Severity.INFO, "No reliable capture date could be determined for the image.", False),
    FlagDefinition("LOW_OCR_CONFIDENCE", Severity.WARNING, "Extraction confidence is below the configured threshold."),
    FlagDefinition("AMBIGUOUS_PREFIX", Severity.WARNING, "The prefix could not be unambiguously matched to a configured rank."),
    FlagDefinition("PROBABLE_MISREAD", Severity.WARNING, "The prefix looks like a misread of a known rank and needs review."),
    FlagDefinition("UNREADABLE_USERNAME", Severity.WARNING, "A username in the image could not be read."),
    FlagDefinition("CONFLICTING_RANK_EVIDENCE", Severity.WARNING, "Contradictory rank readings exist for the same player in one source."),
    FlagDefinition("VERIFICATION_INSUFFICIENT_SOURCES", Severity.WARNING, "A high rank lacks enough independent submitters."),
    FlagDefinition("VERIFICATION_WAIVED", Severity.WARNING, "The independent-submitter requirement was waived by an audited override."),
    FlagDefinition("SUBMITTER_UNKNOWN", Severity.INFO, "At least one image has no recorded submitter.", False),
    FlagDefinition("BEDROCK_PLAYER", Severity.INFO, "Username starts with the Bedrock/Geyser prefix; Java UUID lookup is not applicable."),
    FlagDefinition("PARTIAL_USERNAME", Severity.WARNING, "A username is cut off at the image edge and was not completed."),
    FlagDefinition("CSV_CONFLICT", Severity.WARNING, "A trusted purchase record conflicts with other trusted or verified identity data."),
    FlagDefinition("CSV_IDENTITY_CONFLICT", Severity.ERROR, "Trusted CSV rows assert conflicting username/UUID associations."),
    FlagDefinition("IDENTITY_CONFLICT", Severity.ERROR, "A lookup result conflicts with a trusted identity association."),
    FlagDefinition("MANUAL_REVIEW_REQUIRED", Severity.WARNING, "A human decision is required before this evidence can count."),
    FlagDefinition("MANUAL_OVERRIDE", Severity.INFO, "An explicit manual override determines part of this result.", False),
    FlagDefinition("IDENTITY_AMBIGUOUS", Severity.ERROR, "The identity could not be resolved to a single UUID."),
    FlagDefinition("DUPLICATE_IMAGE", Severity.INFO, "Image content duplicates another imported image.", False),
    FlagDefinition("NEAR_DUPLICATE_IMAGE", Severity.WARNING, "Image is perceptually similar to another image; review whether it is the same capture."),
    FlagDefinition("NAME_FOUND_RANK_UNREADABLE", Severity.WARNING, "Rescan found the username but the rank could not be read."),
    FlagDefinition("UNMAPPED_OLD_RANK", Severity.INFO, "The effective old rank has no new-rank mapping.", False),
    FlagDefinition("NO_RANK_EVIDENCE", Severity.INFO, "No eligible rank evidence exists for this player.", False),
    # UUID lookup failure flags
    FlagDefinition("UUID_LOOKUP_PROXY_CONNECTION_FAILED", Severity.WARNING, "Could not connect to the proxy."),
    FlagDefinition("UUID_LOOKUP_PROXY_AUTH_FAILED", Severity.WARNING, "Proxy rejected the credentials."),
    FlagDefinition("UUID_LOOKUP_PROXY_TUNNEL_FAILED", Severity.WARNING, "Proxy refused to establish the tunnel."),
    FlagDefinition("UUID_LOOKUP_CONNECTION_TIMEOUT", Severity.WARNING, "Connection timed out."),
    FlagDefinition("UUID_LOOKUP_READ_TIMEOUT", Severity.WARNING, "Read timed out."),
    FlagDefinition("UUID_LOOKUP_CONNECTION_ERROR", Severity.WARNING, "Connection error."),
    FlagDefinition("UUID_LOOKUP_DNS_ERROR", Severity.WARNING, "DNS resolution failed."),
    FlagDefinition("UUID_LOOKUP_TLS_ERROR", Severity.WARNING, "TLS handshake failed."),
    FlagDefinition("UUID_LOOKUP_RATE_LIMITED", Severity.WARNING, "Provider rate limit (HTTP 429)."),
    FlagDefinition("UUID_LOOKUP_HTTP_CLIENT_ERROR", Severity.WARNING, "Provider returned a 4xx status that is not a documented not-found."),
    FlagDefinition("UUID_LOOKUP_HTTP_SERVER_ERROR", Severity.WARNING, "Provider returned a 5xx status."),
    FlagDefinition("UUID_LOOKUP_API_UNAVAILABLE", Severity.WARNING, "Provider is unavailable."),
    FlagDefinition("UUID_LOOKUP_INVALID_RESPONSE", Severity.WARNING, "Provider response failed validation."),
    FlagDefinition("UUID_LOOKUP_INVALID_UUID", Severity.WARNING, "Provider returned a malformed UUID."),
    FlagDefinition("UUID_LOOKUP_PROVIDER_UNSUPPORTED", Severity.WARNING, "No provider supports this lookup type."),
    FlagDefinition("UUID_LOOKUP_HISTORICAL_NAME_UNRESOLVED", Severity.WARNING, "The historical username could not be resolved to a UUID."),
    FlagDefinition("UUID_LOOKUP_AMBIGUOUS_IDENTITY", Severity.ERROR, "Lookup produced conflicting candidate identities."),
    FlagDefinition("UUID_LOOKUP_RETRIES_EXHAUSTED", Severity.WARNING, "All retry attempts failed."),
    FlagDefinition("UUID_LOOKUP_NOT_FOUND", Severity.WARNING, "Provider returned a documented not-found response for the current name."),
]

FLAG_CATALOGUE: dict[str, FlagDefinition] = {d.code: d for d in _DEFINITIONS}


def flag_definition(code: str) -> FlagDefinition:
    """Return the definition for ``code``; unknown codes get a warning definition."""
    return FLAG_CATALOGUE.get(code, FlagDefinition(code, Severity.WARNING, "Unknown flag."))


def all_flag_codes() -> list[str]:
    return [d.code for d in _DEFINITIONS]


@dataclass(frozen=True, slots=True)
class FlagInstance:
    """A flag raised during reconciliation, before persistence."""

    code: str
    details: str = ""
    evidence_ref: str | None = None
    severity: Severity | None = None

    @property
    def effective_severity(self) -> Severity:
        return self.severity or flag_definition(self.code).severity

    def render(self) -> str:
        return f"{self.code}: {self.details}" if self.details else self.code
