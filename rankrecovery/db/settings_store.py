"""Persistent, versioned settings backed by the ``settings`` table."""

from __future__ import annotations

import copy
import json
import logging
from typing import Any

from sqlalchemy import select

from rankrecovery.config.defaults import DEFAULT_SETTINGS, SECRET_SETTINGS
from rankrecovery.db.engine import Database
from rankrecovery.db.models import PolicyVersion, Setting, SettingHistory

log = logging.getLogger(__name__)

POLICY_KEYS = {
    "rank_policy": ("ranks.player_ranks", "ranks.staff_prefixes", "ranks.scuffed_rank", "ranks.allow_custom_prefix"),
    "mapping": ("ranks.mapping",),
    "verification": (
        "verification.require_two_submitters_above_immortal",
        "verification.threshold_rank",
        "verification.required_sources_above_threshold",
        "verification.per_rank_thresholds",
        "verification.unknown_submitter_counts_as_one_source",
        "csv.treat_as_confirmed_exempt_from_verification",
        "dates.cutoff",
        "dates.exclude_out_of_date_from_eligibility",
        "dates.trust_filename_pattern",
        "dates.trust_file_modified_time",
        "extraction.min_confidence",
        "ai.filename_indicator_makes_ineligible",
    ),
}


class SettingsStore:
    def __init__(self, db: Database, defaults: dict[str, Any] | None = None):
        self.db = db
        self.defaults = copy.deepcopy(defaults or DEFAULT_SETTINGS)

    # ------------------------------------------------------------------ read
    def get(self, key: str, default: Any = None) -> Any:
        with self.db.read_session() as s:
            row = s.get(Setting, key)
            if row is not None:
                return copy.deepcopy(row.value)
        if key in self.defaults:
            return copy.deepcopy(self.defaults[key])
        return default

    def get_all(self, *, include_secrets: bool = False) -> dict[str, Any]:
        merged = copy.deepcopy(self.defaults)
        with self.db.read_session() as s:
            for row in s.execute(select(Setting)).scalars():
                merged[row.key] = copy.deepcopy(row.value)
        if not include_secrets:
            for key in SECRET_SETTINGS:
                if key in merged:
                    merged[key] = "***" if merged[key] else ""
        return merged

    def is_configured(self, key: str) -> bool:
        value = self.get(key)
        return bool(value)

    # ----------------------------------------------------------------- write
    def set(self, key: str, value: Any, *, changed_by: str = "", reason: str = "") -> bool:
        """Set a value. Returns True when the stored value changed."""
        json.dumps(value)  # must be JSON serialisable
        with self.db.write_session() as s:
            row = s.get(Setting, key)
            old = row.value if row is not None else self.defaults.get(key)
            if row is not None and _json_equal(row.value, value):
                return False
            if row is None:
                if key in self.defaults and _json_equal(self.defaults[key], value):
                    return False
                row = Setting(key=key, value=value, version=1, updated_by=changed_by)
                s.add(row)
            else:
                row.value = value
                row.version += 1
                row.updated_by = changed_by
            s.add(
                SettingHistory(
                    key=key,
                    old_value=None if key in SECRET_SETTINGS else old,
                    new_value=None if key in SECRET_SETTINGS else value,
                    changed_by=changed_by,
                    reason=reason or ("secret updated" if key in SECRET_SETTINGS else ""),
                )
            )
            self._maybe_snapshot_policy(s, key, changed_by, reason)
        return True

    def set_many(self, values: dict[str, Any], *, changed_by: str = "", reason: str = "") -> list[str]:
        changed = []
        for key, value in values.items():
            if self.set(key, value, changed_by=changed_by, reason=reason):
                changed.append(key)
        return changed

    def reset(self, key: str, *, changed_by: str = "", reason: str = "restore defaults") -> bool:
        if key not in self.defaults:
            return False
        return self.set(key, copy.deepcopy(self.defaults[key]), changed_by=changed_by, reason=reason)

    def reset_group(self, prefix: str, *, changed_by: str = "", reason: str = "restore defaults") -> list[str]:
        return [k for k in list(self.defaults) if k.startswith(prefix) and self.reset(k, changed_by=changed_by, reason=reason)]

    def history(self, key: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        with self.db.read_session() as s:
            stmt = select(SettingHistory).order_by(SettingHistory.changed_at.desc(), SettingHistory.id.desc()).limit(limit)
            if key:
                stmt = stmt.where(SettingHistory.key == key)
            return [
                {"key": h.key, "old": h.old_value, "new": h.new_value, "changed_by": h.changed_by, "changed_at": h.changed_at.isoformat() if h.changed_at else None, "reason": h.reason}
                for h in s.execute(stmt).scalars()
            ]

    # ---------------------------------------------------------------- policy
    def _maybe_snapshot_policy(self, s: Any, key: str, changed_by: str, reason: str) -> None:
        for kind, keys in POLICY_KEYS.items():
            if key in keys:
                payload = {k: self._value_in_session(s, k) for k in keys}
                fingerprint = _fingerprint(payload)
                latest = s.execute(select(PolicyVersion).where(PolicyVersion.kind == kind).order_by(PolicyVersion.version.desc())).scalars().first()
                if latest is not None and latest.fingerprint == fingerprint:
                    return
                version = (latest.version + 1) if latest else 1
                s.add(PolicyVersion(kind=kind, version=version, fingerprint=fingerprint, payload=payload, created_by=changed_by, reason=reason or f"{key} changed"))

    def _value_in_session(self, s: Any, key: str) -> Any:
        row = s.get(Setting, key)
        return copy.deepcopy(row.value) if row is not None else copy.deepcopy(self.defaults.get(key))

    def current_policy_version(self, kind: str) -> PolicyVersion | None:
        with self.db.read_session() as s:
            latest = s.execute(select(PolicyVersion).where(PolicyVersion.kind == kind).order_by(PolicyVersion.version.desc())).scalars().first()
            if latest is None:
                # seed version 1 from current values so derived results can reference it
                payload = {k: self.get(k) for k in POLICY_KEYS[kind]}
                with self.db.write_session() as ws:
                    latest = PolicyVersion(kind=kind, version=1, fingerprint=_fingerprint(payload), payload=payload, created_by="system", reason="initial")
                    ws.add(latest)
                    ws.flush()
                    ws.expunge(latest)
            else:
                s.expunge(latest)
            return latest


def _json_equal(a: Any, b: Any) -> bool:
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def _fingerprint(payload: dict[str, Any]) -> str:
    import hashlib

    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
