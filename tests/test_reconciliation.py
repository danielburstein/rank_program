from __future__ import annotations

from rankrecovery.core.enums import ReviewStatus, VerificationStatus
from rankrecovery.core.evidence import VerificationPolicy, Waiver
from rankrecovery.core.reconciliation import ReconciliationEngine


def codes(summary):
    return {f.code for f in summary.flags}


def flag_details(summary, code):
    return [f.details for f in summary.flags if f.code == code]


def cand(summary, rank):
    return next(c for c in summary.candidates if c.rank == rank)


def test_hero_legend_titan_gives_titan(engine, builder):
    b = builder
    b.screenshot("HERO", b.image(1, "alice"))
    b.screenshot("LEGEND", b.image(2, "alice"))
    b.screenshot("TITAN", b.image(3, "bob"))
    s = engine.summarise(b.ev)
    assert s.effective_rank == "TITAN"
    assert s.observed_ranks == ["HERO", "LEGEND", "TITAN"]
    assert s.highest_observed == "TITAN" and s.highest_confirmed == "TITAN"


def test_one_solar_screenshot_confirms_immortal(engine, builder):
    b = builder
    b.screenshot("IMMORTAL", b.image(1, "alice"))
    s = engine.summarise(b.ev)
    assert s.effective_rank == "IMMORTAL"
    assert cand(s, "IMMORTAL").required_sources == 1
    assert "VERIFICATION_INSUFFICIENT_SOURCES" not in codes(s)


def test_scuffed_single_submitter_falls_back_to_immortal(engine, builder):
    b = builder
    b.screenshot("HERO", b.image(1, "alice"))
    for i in range(2, 5):
        b.screenshot("SCUFFED", b.image(i, "alice"), custom="ItsMob")
    s = engine.summarise(b.ev)
    assert s.effective_rank == "IMMORTAL"
    assert s.highest_observed == "SCUFFED"
    c = cand(s, "SCUFFED")
    assert c.status == VerificationStatus.PROVISIONAL and c.progress == "1/2"
    assert "SCUFFED 1/2 sources" in flag_details(s, "VERIFICATION_INSUFFICIENT_SOURCES")
    assert s.custom_prefixes == ["ItsMob"]


def test_two_submitters_confirm_scuffed(engine, builder):
    b = builder
    b.screenshot("SCUFFED", b.image(1, "alice"))
    b.screenshot("SCUFFED", b.image(2, "bob"))
    s = engine.summarise(b.ev)
    assert s.effective_rank == "SCUFFED" and cand(s, "SCUFFED").confirmed


def test_five_images_one_submitter_count_once(engine, builder):
    b = builder
    for i in range(5):
        b.screenshot("INFINITE", b.image(i, "alice"))
    s = engine.summarise(b.ev)
    assert cand(s, "INFINITE").counted_sources == 1 and s.effective_rank == "IMMORTAL"


def test_own_screenshot_counts_once(engine, builder):
    b = builder
    b.ev.usernames = ["alice"]
    b.screenshot("GALACTIC", b.image(1, "alice"))
    s = engine.summarise(b.ev)
    assert cand(s, "GALACTIC").progress == "1/2" and s.effective_rank == "IMMORTAL"


def test_duplicate_image_under_two_submitters_still_one_source(engine, builder):
    b = builder
    b.screenshot("SCUFFED", b.image(1, "alice", group="sha:same"))
    b.screenshot("SCUFFED", b.image(2, "bob", group="sha:same"))
    s = engine.summarise(b.ev)
    c = cand(s, "SCUFFED")
    assert c.counted_sources == 1 and c.status == VerificationStatus.PROVISIONAL
    assert s.effective_rank == "IMMORTAL"


def test_duplicate_plus_unique_from_same_submitter_is_one(engine, builder):
    b = builder
    b.screenshot("SCUFFED", b.image(1, "alice", group="sha:same"))
    b.screenshot("SCUFFED", b.image(2, "bob", group="sha:same"))
    b.screenshot("SCUFFED", b.image(3, "bob"))
    s = engine.summarise(b.ev)
    assert cand(s, "SCUFFED").counted_sources == 1


def test_unknown_submitters_count_as_one(engine, builder):
    b = builder
    b.screenshot("SCUFFED", b.image(1, None))
    b.screenshot("SCUFFED", b.image(2, None))
    s = engine.summarise(b.ev)
    assert cand(s, "SCUFFED").counted_sources == 1 and "SUBMITTER_UNKNOWN" in codes(s)


def test_unknown_plus_known_submitter_confirms(engine, builder):
    b = builder
    b.screenshot("SCUFFED", b.image(1, None))
    b.screenshot("SCUFFED", b.image(2, "bob"))
    s = engine.summarise(b.ev)
    assert cand(s, "SCUFFED").counted_sources == 2 and s.effective_rank == "SCUFFED"


def test_galactic_two_sources_infinite_one(engine, builder):
    b = builder
    b.screenshot("GALACTIC", b.image(1, "alice"))
    b.screenshot("GALACTIC", b.image(2, "bob"))
    b.screenshot("INFINITE", b.image(3, "carol"))
    s = engine.summarise(b.ev)
    assert s.effective_rank == "GALACTIC"
    assert cand(s, "INFINITE").status == VerificationStatus.PROVISIONAL


def test_cosmic_single_source_is_immortal_plus_provisional(engine, builder):
    b = builder
    b.screenshot("IMMORTAL+", b.image(1, "alice"))
    s = engine.summarise(b.ev)
    assert cand(s, "IMMORTAL+").progress == "1/2" and s.effective_rank == "IMMORTAL"


def test_csv_scuffed_confirms_without_second_source(engine, builder):
    b = builder
    b.screenshot("SCUFFED", b.image(1, "alice"))
    b.csv("SCUFFED")
    s = engine.summarise(b.ev)
    assert s.effective_rank == "SCUFFED" and "csv" in cand(s, "SCUFFED").confirmed_by
    assert s.csv_records == 1


def test_csv_legend_vs_screenshot_god(engine, builder):
    b = builder
    b.csv("LEGEND")
    b.screenshot("GOD", b.image(1, "alice"))
    assert engine.summarise(b.ev).effective_rank == "GOD"


def test_csv_infinite_vs_screenshots_galactic(engine, builder):
    b = builder
    b.csv("INFINITE")
    b.screenshot("GALACTIC", b.image(1, "alice"))
    b.screenshot("GALACTIC", b.image(2, "bob"))
    assert engine.summarise(b.ev).effective_rank == "INFINITE"


def test_csv_hero_single_infinite_is_immortal(engine, builder):
    b = builder
    b.csv("HERO")
    b.screenshot("INFINITE", b.image(1, "alice"))
    s = engine.summarise(b.ev)
    assert s.effective_rank == "IMMORTAL" and cand(s, "INFINITE").progress == "1/2"


def test_csv_later_lower_rank_does_not_downgrade(engine, builder):
    b = builder
    b.csv("SCUFFED")
    b.csv("HERO")
    assert engine.summarise(b.ev).effective_rank == "SCUFFED"


def test_csv_not_exempt_counts_as_one_source(policy, builder):
    engine = ReconciliationEngine(policy, VerificationPolicy(csv_exempt_from_verification=False))
    b = builder
    b.csv("SCUFFED")
    s = engine.summarise(b.ev)
    assert cand(s, "SCUFFED").progress == "1/2" and s.effective_rank == "IMMORTAL"
    b.screenshot("SCUFFED", b.image(1, "alice"))
    assert engine.summarise(b.ev).effective_rank == "SCUFFED"


def test_ai_flagged_higher_rank(engine, builder):
    b = builder
    b.screenshot("HERO", b.image(1, "alice"))
    b.screenshot("SCUFFED", b.image(2, "bob", ai=True))
    s = engine.summarise(b.ev)
    assert s.effective_rank == "HERO"
    assert "SCUFFED" in flag_details(s, "AI_DETECTED_HIGHER_RANK")
    assert "SCUFFED" in s.observed_ranks  # observation retained
    c = cand(s, "SCUFFED")
    assert c.counted_sources == 0 and c.fallback_rank is None and c.progress == "0/2"
    assert any("AI indicator" in r for r in c.evidence[0].reasons)
    assert s.ai_rank_images == 1 and s.clean_rank_images == 1


def test_ai_for_all_rank_images(engine, builder):
    b = builder
    b.screenshot("HERO", b.image(1, "alice", ai=True))
    s = engine.summarise(b.ev)
    assert s.effective_rank is None and "AI_DETECTED_FOR_ALL_RANK_IMAGES" in codes(s)


def test_clean_and_ai_same_rank_both_kept(engine, builder):
    b = builder
    b.screenshot("GALACTIC", b.image(1, "alice"))
    b.screenshot("GALACTIC", b.image(2, "bob", ai=True))
    s = engine.summarise(b.ev)
    c = cand(s, "GALACTIC")
    assert c.counted_sources == 1 and len(c.evidence) == 2


def test_out_of_date_excluded_by_default(policy, builder):
    engine = ReconciliationEngine(policy, VerificationPolicy())
    b = builder
    b.screenshot("GOD", b.image(1, "alice", out_of_date=True))
    s = engine.summarise(b.ev)
    assert s.effective_rank is None and "OUT_OF_DATE" in codes(s)
    engine2 = ReconciliationEngine(policy, VerificationPolicy(exclude_out_of_date=False))
    assert engine2.summarise(b.ev).effective_rank == "GOD"


def test_date_unknown_is_informational(engine, builder):
    b = builder
    b.screenshot("GOD", b.image(1, "alice", date_unknown=True))
    s = engine.summarise(b.ev)
    assert s.effective_rank == "GOD" and "DATE_UNKNOWN" in codes(s)


def test_low_confidence_ineligible_unless_verified(engine, builder):
    b = builder
    b.screenshot("GOD", b.image(1, "alice"), confidence=0.3)
    s = engine.summarise(b.ev)
    assert s.effective_rank is None and "LOW_OCR_CONFIDENCE" in codes(s)
    b.ev.rank_observations.clear()
    b.screenshot("GOD", b.image(1, "alice"), confidence=0.3, verified=True)
    assert engine.summarise(b.ev).effective_rank == "GOD"


def test_pending_review_not_eligible(engine, builder):
    b = builder
    b.screenshot("GOD", b.image(1, "alice"), review=ReviewStatus.PENDING)
    s = engine.summarise(b.ev)
    assert s.effective_rank is None and "MANUAL_REVIEW_REQUIRED" in codes(s)


def test_staff_only(engine, builder):
    b = builder
    b.staff("MOD", b.image(1, "alice"))
    s = engine.summarise(b.ev)
    assert s.effective_rank is None and s.staff_ranks == ["MOD"] and "ONLY_STAFF_RANK_DETECTED" in codes(s)
    assert "SCUFFED" not in s.observed_ranks


def test_staff_with_yinyang_scuffed_candidate(engine, builder):
    b = builder
    img = b.image(1, "alice")
    b.staff("MOD", img, yinyang=True)
    b.screenshot("SCUFFED", img, basis="trailing_icon")
    s = engine.summarise(b.ev)
    assert "STAFF_AND_PLAYER_RANK_FOUND" in codes(s)
    assert cand(s, "SCUFFED").progress == "1/2" and s.effective_rank == "IMMORTAL"


def test_staff_and_player_rank(engine, builder):
    b = builder
    b.staff("HELPER", b.image(1, "alice"))
    b.screenshot("TITAN", b.image(2, "bob"))
    s = engine.summarise(b.ev)
    assert s.effective_rank == "TITAN" and "STAFF_AND_PLAYER_RANK_FOUND" in codes(s) and s.staff_ranks == ["HELPER"]


def test_verification_disabled_uses_highest_eligible(policy, builder):
    b = builder
    b.screenshot("SCUFFED", b.image(1, "alice"))
    on = ReconciliationEngine(policy, VerificationPolicy(require_independent_submitters=True))
    off = ReconciliationEngine(policy, VerificationPolicy(require_independent_submitters=False))
    assert on.summarise(b.ev).effective_rank == "IMMORTAL"
    assert off.summarise(b.ev).effective_rank == "SCUFFED"
    # re-enabling restores the requirement
    assert on.summarise(b.ev).effective_rank == "IMMORTAL"


def test_per_rank_threshold(policy, builder):
    engine = ReconciliationEngine(policy, VerificationPolicy(per_rank_thresholds={"SCUFFED": 3}))
    b = builder
    b.screenshot("SCUFFED", b.image(1, "a"))
    b.screenshot("SCUFFED", b.image(2, "b"))
    s = engine.summarise(b.ev)
    assert cand(s, "SCUFFED").progress == "2/3"
    # two sources satisfy INFINITE's requirement, so the fallback lands there
    assert cand(s, "SCUFFED").fallback_rank == "INFINITE" and s.effective_rank == "INFINITE"


def test_waiver_confirms_with_one_source_and_flags(engine, builder):
    b = builder
    b.screenshot("SCUFFED", b.image(1, "alice"))
    b.ev.waivers.append(Waiver("SCUFFED", "owner confirmed in Discord", "daniel"))
    s = engine.summarise(b.ev)
    assert s.effective_rank == "SCUFFED" and cand(s, "SCUFFED").status == VerificationStatus.WAIVED
    assert "VERIFICATION_WAIVED" in codes(s)


def test_manual_override(engine, builder):
    b = builder
    b.manual_override("GOD")
    s = engine.summarise(b.ev)
    assert s.effective_rank == "GOD" and s.manual_override_active and "MANUAL_OVERRIDE" in codes(s)


def test_conflicting_rank_in_one_image(engine, builder):
    b = builder
    img = b.image(1, "alice")
    b.screenshot("HERO", img)
    b.screenshot("TITAN", img)
    assert "CONFLICTING_RANK_EVIDENCE" in codes(engine.summarise(b.ev))


def test_multiple_appearances_in_one_image_count_once(engine, builder):
    b = builder
    img = b.image(1, "alice")
    b.screenshot("SCUFFED", img)
    b.screenshot("SCUFFED", img)
    assert cand(engine.summarise(b.ev), "SCUFFED").counted_sources == 1


def test_compact_rendering(engine, builder):
    b = builder
    b.screenshot("HERO", b.image(1, "alice"))
    b.screenshot("SCUFFED", b.image(2, "alice"))
    b.staff("MOD", b.image(3, "bob"))
    text = engine.summarise(b.ev).compact()
    assert "SCUFFED provisional 1/2" in text and "Effective IMMORTAL" in text and "Staff MOD" in text
