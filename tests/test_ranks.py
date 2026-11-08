from __future__ import annotations

import pytest

from rankrecovery.core.enums import PrefixKind
from rankrecovery.core.ranks import PolicyError, RankPolicy, default_policy


def test_default_order_and_priorities(policy):
    names = policy.rank_names()
    assert names == ["DEFAULT", "HERO", "LEGEND", "TITAN", "GOD", "IMMORTAL", "IMMORTAL+", "GALACTIC", "INFINITE", "SCUFFED"]
    assert policy.priority("IMMORTAL+") > policy.priority("IMMORTAL")
    assert policy.priority("SCUFFED") == 10


@pytest.mark.parametrize(
    "raw,canonical,kind",
    [
        ("Member", "DEFAULT", PrefixKind.SEASONAL_ALIAS),
        ("NOVA", "HERO", PrefixKind.SEASONAL_ALIAS),
        ("Global", "HERO", PrefixKind.SEASONAL_ALIAS),
        ("Solar", "IMMORTAL", PrefixKind.SEASONAL_ALIAS),
        ("cosmic", "IMMORTAL+", PrefixKind.SEASONAL_ALIAS),
        ("◇ Titan ◇", "TITAN", PrefixKind.PLAYER_RANK),
        ("§6Immortal+", "IMMORTAL+", PrefixKind.PLAYER_RANK),
        ("∞ Infinite ∞", "INFINITE", PrefixKind.PLAYER_RANK),
        ("☯ Scuffed ☯", "SCUFFED", PrefixKind.PLAYER_RANK),
    ],
)
def test_aliases_and_decorations(policy, raw, canonical, kind):
    m = policy.match_prefix(raw)
    assert m.kind == kind
    assert m.canonical == canonical
    assert m.raw_text == raw  # raw text preserved


def test_immortal_plus_is_distinct(policy):
    assert policy.match_prefix("Immortal").canonical == "IMMORTAL"
    assert policy.match_prefix("Immortal+").canonical == "IMMORTAL+"
    # a misread that drops/keeps the plus must not cross the boundary
    m = policy.match_prefix("Imm0rtal")
    assert m.kind == PrefixKind.PROBABLE_MISREAD and m.misread_of == "IMMORTAL"
    m = policy.match_prefix("Imm0rtal+")
    assert m.kind == PrefixKind.PROBABLE_MISREAD and m.misread_of == "IMMORTAL+"


def test_admin_plus_is_distinct(policy):
    assert policy.match_prefix("Admin").canonical == "ADMIN"
    assert policy.match_prefix("Admin+").canonical == "ADMIN+"
    assert policy.match_prefix("Adm1n+").misread_of == "ADMIN+"


@pytest.mark.parametrize("raw,canonical", [("Sr Mod", "SR MOD"), ("SrMod", "SR MOD"), ("HEADMOD", "HEAD MOD"), ("Head Mod", "HEAD MOD"), ("CoOwner", "CO OWNER"), ("Co Owner", "CO OWNER"), ("Developer", "DEV"), ("Owner", "OWNER"), ("Helper", "HELPER")])
def test_staff_equivalents(policy, raw, canonical):
    m = policy.match_prefix(raw)
    assert m.kind == PrefixKind.STAFF
    assert m.canonical == canonical


def test_misread_goes_to_review_not_custom(policy):
    m = policy.match_prefix("HER0")
    assert m.kind == PrefixKind.PROBABLE_MISREAD
    assert m.misread_of == "HERO"
    m = policy.match_prefix("Ga1actic")
    assert m.kind == PrefixKind.PROBABLE_MISREAD and m.misread_of == "GALACTIC"
    m = policy.match_prefix("Scuffe")
    assert m.kind == PrefixKind.PROBABLE_MISREAD and m.misread_of == "SCUFFED"


def test_custom_prefix_becomes_scuffed_candidate(policy):
    for raw in ("ItsMob", "Businessman", "ms kitten", "Ouroboros"):
        m = policy.match_prefix(raw)
        assert m.kind == PrefixKind.CUSTOM, raw
        assert m.canonical == "SCUFFED"


def test_chat_text_is_not_custom_prefix(policy):
    assert policy.match_prefix("First person to work out (25 + -43) gets").kind == PrefixKind.UNREADABLE
    assert policy.match_prefix("http://x").kind == PrefixKind.UNREADABLE


def test_ambiguous_alias_rejected():
    with pytest.raises(PolicyError):
        RankPolicy.from_config([{"name": "HERO", "aliases": ["NOVA"]}, {"name": "LEGEND", "aliases": ["NOVA"]}], [])
    with pytest.raises(PolicyError):
        RankPolicy.from_config([{"name": "HERO", "aliases": ["MOD"]}, {"name": "SCUFFED"}], [{"name": "MOD"}])


def test_policy_roundtrip_and_fingerprint():
    p = default_policy()
    ranks, staff = p.to_config()
    q = RankPolicy.from_config(ranks, staff)
    assert q.fingerprint() == p.fingerprint()
    ranks[1]["aliases"].append("STAR")
    assert RankPolicy.from_config(ranks, staff).fingerprint() != p.fingerprint()


def test_short_staff_misreads_go_to_review(policy):
    m = policy.match_prefix("Nod")
    assert m.kind == PrefixKind.PROBABLE_MISREAD and m.misread_of == "MOD"
    m = policy.match_prefix("Ouner")
    assert m.kind == PrefixKind.PROBABLE_MISREAD and m.misread_of == "OWNER"
    assert policy.match_prefix("GOO").misread_of == "GOD"
    # equally close to two known prefixes -> review without a guess
    amb = policy.match_prefix("HOD")  # one edit from MOD and GOD
    assert amb.kind == PrefixKind.PROBABLE_MISREAD and amb.misread_of is None
