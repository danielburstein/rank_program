from __future__ import annotations

from rankrecovery.config.defaults import DEFAULT_RANK_MAPPING
from rankrecovery.core.conversion import FlagFilterPolicy, RankMapping, decide_conversion
from rankrecovery.core.export import ExportCandidate, build_export


def mapping():
    return RankMapping.from_config(DEFAULT_RANK_MAPPING)


def test_default_mapping_and_aliases(policy):
    m = mapping()
    assert m.new_rank_for("HERO", policy) == "thug"
    assert m.new_rank_for("NOVA", policy) == "thug"
    assert m.new_rank_for("SOLAR", policy) == "bandit"
    assert m.new_rank_for("COSMIC", policy) == "rogue"
    assert m.new_rank_for("SCUFFED", policy) == "Warlord"
    assert m.new_rank_for("DEFAULT", policy) is None
    assert m.new_rank_for("MEMBER", policy) is None


def test_flag_filter_blocks_by_default(policy):
    d = decide_conversion(effective_old_rank="GOD", verification_ok=True, active_flags=["DATE_UNKNOWN"], resolved_flags=[], mapping=mapping(), policy=policy, flag_policy=FlagFilterPolicy())
    assert not d.included and "DATE_UNKNOWN" in d.reasons[0]
    d = decide_conversion(effective_old_rank="GOD", verification_ok=True, active_flags=["DATE_UNKNOWN"], resolved_flags=[], mapping=mapping(), policy=policy, flag_policy=FlagFilterPolicy(frozenset({"DATE_UNKNOWN"})))
    assert d.included and d.new_rank == "bandit"


def test_flag_inclusion_cannot_bypass_verification(policy):
    d = decide_conversion(effective_old_rank=None, verification_ok=False, active_flags=["VERIFICATION_INSUFFICIENT_SOURCES"], resolved_flags=[], mapping=mapping(), policy=policy, flag_policy=FlagFilterPolicy(frozenset({"VERIFICATION_INSUFFICIENT_SOURCES"})))
    assert not d.included


def test_resolved_flags_policy(policy):
    d = decide_conversion(effective_old_rank="HERO", verification_ok=True, active_flags=[], resolved_flags=["OUT_OF_DATE"], mapping=mapping(), policy=policy, flag_policy=FlagFilterPolicy())
    assert not d.included
    d = decide_conversion(effective_old_rank="HERO", verification_ok=True, active_flags=[], resolved_flags=["OUT_OF_DATE"], mapping=mapping(), policy=policy, flag_policy=FlagFilterPolicy(allow_resolved_flags=True))
    assert d.included


def test_export_dedupes_and_skips():
    u = "069a79f4-44e9-4726-a5be-fca90e38aaf5"
    cands = [
        ExportCandidate(1, u, "SCUFFED", "Warlord", "verified", True),
        ExportCandidate(2, "069A79F444E94726A5BEFCA90E38AAF5", "HERO", "thug", "verified", True),  # same uuid undashed
        ExportCandidate(3, None, "HERO", "thug", "unresolved", True),
        ExportCandidate(4, "not-a-uuid", "HERO", "thug", "verified", True),
        ExportCandidate(5, "11111111-2222-3333-4444-555555555555", "HERO", "thug", "verified", False, ["blocked by flags: DATE_UNKNOWN"]),
        ExportCandidate(6, "11111111-2222-3333-4444-666666666666", "DEFAULT", None, "verified", True),
    ]
    result = build_export(cands)
    assert result.commands == [f"lp user {u} parent set Warlord"]
    reasons = {r.player_id: r.skip_reason for r in result.skipped}
    assert "duplicate" in reasons[2] and "identity" in reasons[3] and "invalid" in reasons[4]
    assert "DATE_UNKNOWN" in reasons[5] and "no mapped" in reasons[6]
    assert result.text().endswith("\n") and "/" not in result.commands[0] and '"' not in result.commands[0]
