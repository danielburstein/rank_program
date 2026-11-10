"""Application context: one object that wires the database, settings and policies."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import date
from typing import TYPE_CHECKING, Any

from rankrecovery.core.conversion import FlagFilterPolicy, RankMapping
from rankrecovery.core.dates import DatePolicy
from rankrecovery.core.evidence import VerificationPolicy
from rankrecovery.core.ranks import RankPolicy
from rankrecovery.db.engine import Database
from rankrecovery.db.settings_store import SettingsStore

if TYPE_CHECKING:
    from rankrecovery.extraction.base import TextExtractor
    from rankrecovery.jobs.runner import JobRunner

log = logging.getLogger(__name__)


class AppContext:
    def __init__(self, db: Database, settings: SettingsStore | None = None, *, extractor_factory: Callable[[AppContext, str | None], TextExtractor] | None = None):
        self.db = db
        self.settings = settings or SettingsStore(db)
        self._extractor_factory = extractor_factory
        upgrade = getattr(self.settings, "apply_upgrades", None)
        if upgrade is not None:
            try:
                for key in upgrade():
                    log.warning("settings upgrade: %s is now %r (the stored value could no longer work)", key, self.settings.get(key))
            except Exception:  # noqa: BLE001 - a missing table before migrations must not stop start-up
                log.debug("settings upgrade skipped", exc_info=True)
        self.job_runner: JobRunner | None = None

    # ----------------------------------------------------------------- policies
    def policy(self) -> RankPolicy:
        s = self.settings
        version = s.current_policy_version("rank_policy")
        return RankPolicy.from_config(
            s.get("ranks.player_ranks"),
            s.get("ranks.staff_prefixes"),
            scuffed_rank=s.get("ranks.scuffed_rank", "SCUFFED"),
            allow_custom_prefix=bool(s.get("ranks.allow_custom_prefix", True)),
            version=version.version if version else 0,
        )

    def verification_policy(self) -> VerificationPolicy:
        s = self.settings
        return VerificationPolicy(
            require_independent_submitters=bool(s.get("verification.require_two_submitters_above_immortal", False)),
            threshold_rank=str(s.get("verification.threshold_rank", "IMMORTAL")),
            required_sources_above_threshold=int(s.get("verification.required_sources_above_threshold", 2)),
            per_rank_thresholds={str(k).upper(): int(v) for k, v in (s.get("verification.per_rank_thresholds") or {}).items()},
            csv_exempt_from_verification=bool(s.get("csv.treat_as_confirmed_exempt_from_verification", True)),
            exclude_out_of_date=bool(s.get("dates.exclude_out_of_date_from_eligibility", True)),
            min_confidence=float(s.get("extraction.min_confidence", 0.6)),
            unknown_submitter_counts_as_one_source=bool(s.get("verification.unknown_submitter_counts_as_one_source", True)),
            weak_ai_indicator_makes_ineligible=bool(s.get("ai.filename_indicator_makes_ineligible", False)),
        )

    def verification_settings_snapshot(self) -> dict[str, Any]:
        v = self.verification_policy()
        return {
            "require_independent_submitters": v.require_independent_submitters,
            "threshold_rank": v.threshold_rank,
            "required_sources_above_threshold": v.required_sources_above_threshold,
            "per_rank_thresholds": v.per_rank_thresholds,
            "csv_exempt_from_verification": v.csv_exempt_from_verification,
            "exclude_out_of_date": v.exclude_out_of_date,
            "min_confidence": v.min_confidence,
        }

    def mapping(self) -> RankMapping:
        version = self.settings.current_policy_version("mapping")
        return RankMapping.from_config(self.settings.get("ranks.mapping") or {}, version=version.version if version else 0)

    def flag_policy(self) -> FlagFilterPolicy:
        allowed = self.settings.get("conversion.allowed_flags") or []
        return FlagFilterPolicy(frozenset(str(a) for a in allowed), bool(self.settings.get("conversion.allow_resolved_flags", False)))

    def date_policy(self) -> DatePolicy:
        raw = str(self.settings.get("dates.cutoff", "2026-01-01"))
        try:
            cutoff = date.fromisoformat(raw)
        except ValueError:
            log.warning("invalid dates.cutoff %r; using 2026-01-01", raw)
            cutoff = date(2026, 1, 1)
        return DatePolicy(
            cutoff=cutoff,
            trust_filename_pattern=bool(self.settings.get("dates.trust_filename_pattern", True)),
            trust_file_modified_time=bool(self.settings.get("dates.trust_file_modified_time", False)),
        )

    # ----------------------------------------------------------------- secrets
    def anthropic_api_key(self) -> str | None:
        env = os.environ.get("ANTHROPIC_API_KEY")
        if env:
            return env
        value = self.settings.get("vision.anthropic_api_key")
        return str(value) if value else None

    def vision_available(self) -> bool:
        return bool(self.anthropic_api_key())

    # ---------------------------------------------------------------- factories
    def build_extractor(self, override: str | None = None) -> TextExtractor:
        if self._extractor_factory is not None:
            return self._extractor_factory(self, override)
        from rankrecovery.core.enums import ExtractorType
        from rankrecovery.extraction.factory import build_extractor

        forced = ExtractorType(override) if override and override != "auto" else None
        return build_extractor(self.settings.get_all(include_secrets=True), self.anthropic_api_key(), extractor_type=forced)
