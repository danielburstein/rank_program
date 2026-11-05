from __future__ import annotations

from rankrecovery.core.enums import DetectionStatus, LeadingIcon, RegionKind, TrailingIcon
from rankrecovery.core.parsing import RawRow, RowParser


def parser(policy):
    return RowParser(policy)


def test_tab_row_with_diamonds(policy):
    d = parser(policy).parse(RawRow(text="◇ Titan ◇ AndrewIsFire", region=RegionKind.TAB))
    assert d and d.normalised_username == "AndrewIsFire" and d.rank_observation == "TITAN"
    assert d.status == DetectionStatus.CANDIDATE and not d.flags


def test_tab_row_ping_number_dropped(policy):
    d = parser(policy).parse(RawRow(text="◇ Cosmic ◇ holcky 216", region=RegionKind.TAB))
    assert d.normalised_username == "holcky" and d.rank_observation == "IMMORTAL+"


def test_custom_prefix_with_yinyang(policy):
    d = parser(policy).parse(RawRow(text="☯ ms kitten ☯ Acemilvi", region=RegionKind.TAB, leading_icon=LeadingIcon.YINYANG))
    assert d.rank_observation == "SCUFFED" and d.custom_prefix_text == "MS KITTEN"
    assert d.yinyang_evidence and not d.needs_review


def test_custom_prefix_without_yinyang_needs_review(policy):
    d = parser(policy).parse(RawRow(text="Businessman Zeyos_R: ok", region=RegionKind.CHAT))
    assert d.rank_observation == "SCUFFED" and d.custom_prefix_text == "BUSINESSMAN"
    assert d.needs_review and any(f.code == "AMBIGUOUS_PREFIX" for f in d.flags)


def test_staff_row_trailing_icons(policy):
    p = parser(policy)
    d = p.parse(RawRow(text="◇ Mod ◇ Player4995", region=RegionKind.TAB, trailing_icon=TrailingIcon.YINYANG))
    assert d.staff_observation == "MOD" and d.trailing_icon_rank == "SCUFFED" and d.rank_observation is None
    d = p.parse(RawRow(text="◇ Sr Mod ◇ 2098ms", region=RegionKind.TAB, trailing_icon=TrailingIcon.INFINITY))
    assert d.staff_observation == "SR MOD" and d.trailing_icon_rank == "INFINITE"
    d = p.parse(RawRow(text="◇ Owner ◇ xBooga", region=RegionKind.NAMETAG))
    assert d.staff_observation == "OWNER" and d.trailing_icon_rank is None and d.rank_observation is None


def test_chat_line_rules(policy):
    p = parser(policy)
    d = p.parse(RawRow(text="◇ Immortal ◇ Yankster_08: -36", region=RegionKind.CHAT))
    assert d.normalised_username == "Yankster_08" and d.rank_observation == "IMMORTAL"
    join = p.parse(RawRow(text="[+] Oskar2", region=RegionKind.CHAT))
    assert join.status == DetectionStatus.CONTEXT_ONLY and join.rank_observation is None
    sysmsg = p.parse(RawRow(text="GamerKasp died", region=RegionKind.CHAT))
    assert sysmsg.status == DetectionStatus.CONTEXT_ONLY and sysmsg.normalised_username == "GamerKasp"
    mention = p.parse(RawRow(text="◇ ItsMob ◇ Player4995: @Baker18", region=RegionKind.CHAT))
    assert mention.normalised_username == "Player4995" and mention.rank_observation == "SCUFFED"


def test_scoreboard_label_dropped(policy):
    d = parser(policy).parse(RawRow(text="Player: ◇ Owner ◇ xBooga", region=RegionKind.SCOREBOARD))
    assert d.normalised_username == "xBooga" and d.staff_observation == "OWNER"


def test_bedrock_name(policy):
    d = parser(policy).parse(RawRow(text="∞ Infinite ∞ .Axeliuz10", region=RegionKind.TAB, leading_icon=LeadingIcon.INFINITY))
    assert d.is_bedrock and d.username_valid and d.normalised_username == ".Axeliuz10"
    assert d.rank_observation == "INFINITE"
    assert any(f.code == "BEDROCK_PLAYER" for f in d.flags)


def test_partial_username_produces_no_rank(policy):
    d = parser(policy).parse(RawRow(text="◇ Galactic ◇ Zeyos4Dan", region=RegionKind.TAB, username_cut_off=True))
    assert d.status == DetectionStatus.PARTIAL and d.rank_observation is None
    assert any(f.code == "PARTIAL_USERNAME" for f in d.flags)


def test_misread_prefix_goes_to_review(policy):
    d = parser(policy).parse(RawRow(text="◇ HER0 ◇ Player1", region=RegionKind.TAB))
    assert d.status == DetectionStatus.NEEDS_REVIEW and d.rank_observation == "HERO"
    assert any(f.code == "PROBABLE_MISREAD" for f in d.flags) and d.custom_prefix_text is None


def test_structured_fields_from_vision(policy):
    d = parser(policy).parse(RawRow(text="", region=RegionKind.TAB, prefix_text="Nova", username_text="StonerBomb", confidence=0.95))
    assert d.rank_observation == "HERO" and d.raw_prefix == "Nova"


def test_low_confidence_flag(policy):
    d = parser(policy).parse(RawRow(text="◇ Hero ◇ Someone", region=RegionKind.TAB, confidence=0.5))
    assert d.needs_review and any(f.code == "LOW_OCR_CONFIDENCE" for f in d.flags)


def test_name_without_rank(policy):
    d = parser(policy).parse(RawRow(text="Catboi278", region=RegionKind.NAMETAG))
    assert d.rank_observation is None and any(f.code == "NAME_READ_RANK_NOT_FOUND" for f in d.flags)


def test_unreadable_prefix_with_yinyang_is_scuffed_candidate(policy):
    d = parser(policy).parse(RawRow(text="", region=RegionKind.TAB, prefix_text="", username_text="Ekpoa", prefix_unreadable=True, leading_icon=LeadingIcon.YINYANG))
    assert d.rank_observation == "SCUFFED" and d.needs_review


def test_label_only_scoreboard_row_is_ignored(policy):
    from rankrecovery.core.parsing import RawRow, RowParser

    d = RowParser(policy).parse(RawRow(text="Player:", region=RegionKind.SCOREBOARD, has_colon_after_name=True))
    assert d is None or d.status == DetectionStatus.CONTEXT_ONLY
