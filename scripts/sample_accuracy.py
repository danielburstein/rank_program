#!/usr/bin/env python
"""Run extraction over the sample screenshots and score it against expected.csv.

Usage:
    python scripts/sample_accuracy.py --samples samples --extractor auto|vision|ocr [--api-key-env ANTHROPIC_API_KEY] [--json out.json]

The samples are never imported into any database; this is a pure accuracy check.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rankrecovery.config.defaults import DEFAULT_SETTINGS  # noqa: E402
from rankrecovery.core.enums import DetectionStatus  # noqa: E402
from rankrecovery.core.parsing import ParsedDetection, RowParser  # noqa: E402
from rankrecovery.core.ranks import default_policy  # noqa: E402
from rankrecovery.extraction.base import ExtractorUnavailable  # noqa: E402
from rankrecovery.extraction.factory import build_extractor, resolve_extractor_type  # noqa: E402
from rankrecovery.extraction.pipeline import run_extraction  # noqa: E402

UNCERTAIN_SPELLINGS = {"DMAN012", "Sooms", "TumiFIN", "A9A", "Lucked0ut", "Larbeny", "Kallsum", "ElectMun", "Noolof", "Rad1cql", "Bpucci", "holcky"}


@dataclass
class ExpectedRow:
    image: str
    username: str
    prefix_text: str
    rank: str

    @property
    def is_staff(self) -> bool:
        return self.rank.upper() == "STAFF"

    @property
    def staff_with_icon_rank(self) -> bool:
        """Staff prefix in prefix_text but an ordinary rank in the rank column (trailing icon)."""
        return (not self.is_staff) and self.prefix_text.strip().upper().replace(" ", "") in {"SRMOD", "HELPER", "MOD", "HEADMOD", "ADMIN", "ADMIN+", "OWNER", "COOWNER", "DEV", "TRAINEE"}


@dataclass
class ImageReport:
    image: str
    expected: int = 0
    matched: int = 0
    missed: list[dict] = field(default_factory=list)
    wrong: list[dict] = field(default_factory=list)
    extra: list[dict] = field(default_factory=list)
    partial_or_unreadable: int = 0
    seconds: float = 0.0
    error: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return (self.matched / self.expected * 100.0) if self.expected else 0.0


def load_expected(path: Path) -> list[ExpectedRow]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        return [ExpectedRow(r["image_filename"].strip(), r["username"].strip(), r["prefix_text"].strip(), r["rank"].strip()) for r in reader]


def describe(d: ParsedDetection) -> str:
    parts = [f"prefix={d.raw_prefix!r}"]
    if d.rank_observation:
        parts.append(f"rank={d.rank_observation}")
    if d.staff_observation:
        parts.append(f"staff={d.staff_observation}")
    if d.trailing_icon_rank:
        parts.append(f"trailing={d.trailing_icon_rank}")
    parts.append(f"status={d.status.value}")
    parts.append(f"conf={d.confidence:.2f}")
    return " ".join(parts)


def detection_matches(expected: ExpectedRow, d: ParsedDetection, policy) -> bool:
    if expected.is_staff:
        return d.staff_observation is not None
    canonical = policy.canonical_rank(expected.rank) or expected.rank.upper()
    if expected.staff_with_icon_rank:
        return d.staff_observation is not None and d.trailing_icon_rank == canonical
    return d.rank_observation == canonical or d.trailing_icon_rank == canonical


def score_image(name: str, expected_rows: list[ExpectedRow], detections: list[ParsedDetection], policy) -> ImageReport:
    report = ImageReport(image=name, expected=len(expected_rows))
    by_name: dict[str, list[ParsedDetection]] = {}
    usable: list[ParsedDetection] = []
    for d in detections:
        if d.status in (DetectionStatus.PARTIAL, DetectionStatus.UNREADABLE):
            report.partial_or_unreadable += 1
            continue
        usable.append(d)
        by_name.setdefault(d.normalised_username.lower(), []).append(d)
    claimed: set[int] = set()
    for exp in expected_rows:
        candidates = by_name.get(exp.username.lower(), [])
        hit = next((d for d in candidates if detection_matches(exp, d, policy)), None)
        if hit is not None:
            report.matched += 1
            claimed.add(id(hit))
            continue
        if candidates:
            report.wrong.append({"username": exp.username, "expected": f"{exp.prefix_text} -> {exp.rank}", "read": "; ".join(describe(d) for d in candidates), "uncertain_spelling": exp.username in UNCERTAIN_SPELLINGS})
            for d in candidates:
                claimed.add(id(d))
        else:
            report.missed.append({"username": exp.username, "expected": f"{exp.prefix_text} -> {exp.rank}", "uncertain_spelling": exp.username in UNCERTAIN_SPELLINGS})
    expected_names = {e.username.lower() for e in expected_rows}
    for d in usable:
        if id(d) in claimed or d.normalised_username.lower() in expected_names:
            continue
        if d.status == DetectionStatus.CONTEXT_ONLY:
            continue
        report.extra.append({"username": d.normalised_username, "read": describe(d)})
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--samples", default="samples", help="folder containing screenshots/ and expected.csv")
    parser.add_argument("--extractor", default="auto", choices=["auto", "vision", "ocr"])
    parser.add_argument("--api-key-env", default="ANTHROPIC_API_KEY")
    parser.add_argument("--json", default=None, help="write the full report to this JSON file")
    parser.add_argument("--model", default=None, help="override the vision model id")
    args = parser.parse_args(argv)

    samples = Path(args.samples)
    expected = load_expected(samples / "expected.csv")
    images = sorted({e.image for e in expected})
    settings = dict(DEFAULT_SETTINGS)
    settings["extraction.default_extractor"] = args.extractor
    if args.model:
        settings["extraction.vision_model"] = args.model
    api_key = os.environ.get(args.api_key_env) or None
    chosen = resolve_extractor_type(settings, api_key)
    if args.extractor == "vision" and not api_key:
        print(f"No API key in ${args.api_key_env}; cannot run the vision extractor.")
        return 0
    try:
        extractor = build_extractor(settings, api_key, extractor_type=chosen)
    except ExtractorUnavailable as exc:
        print(f"Extractor unavailable: {exc}")
        return 0
    policy = default_policy()
    row_parser = RowParser(policy)
    print(f"Extractor: {chosen.value} ({extractor.settings.engine} {extractor.settings.model})".rstrip())

    reports: list[ImageReport] = []
    for name in images:
        path = samples / "screenshots" / name
        exp_rows = [e for e in expected if e.image == name]
        if not path.exists():
            reports.append(ImageReport(image=name, expected=len(exp_rows), error="file not found"))
            continue
        started = time.monotonic()
        result = run_extraction(path.read_bytes(), extractor, row_parser, filename=name)
        report = score_image(name, exp_rows, result.detections, policy)
        report.seconds = round(time.monotonic() - started, 1)
        report.error = result.output.error
        report.warnings = result.output.warnings[:5]
        reports.append(report)

    total_expected = sum(r.expected for r in reports)
    total_matched = sum(r.matched for r in reports)
    print()
    print(f"{'image':<24}{'expected':>9}{'matched':>9}{'missed':>8}{'wrong':>7}{'extra':>7}{'partial':>9}{'acc%':>7}{'secs':>7}")
    for r in reports:
        print(f"{r.image:<24}{r.expected:>9}{r.matched:>9}{len(r.missed):>8}{len(r.wrong):>7}{len(r.extra):>7}{r.partial_or_unreadable:>9}{r.accuracy:>7.1f}{r.seconds:>7.1f}")
    overall = (total_matched / total_expected * 100.0) if total_expected else 0.0
    print(f"{'TOTAL':<24}{total_expected:>9}{total_matched:>9}{sum(len(r.missed) for r in reports):>8}{sum(len(r.wrong) for r in reports):>7}{sum(len(r.extra) for r in reports):>7}{sum(r.partial_or_unreadable for r in reports):>9}{overall:>7.1f}")
    print()
    for r in reports:
        if r.error:
            print(f"[{r.image}] ERROR: {r.error}")
        for w in r.warnings:
            print(f"[{r.image}] warning: {w}")
        for m in r.missed:
            flag = " (uncertain spelling in expected.csv)" if m["uncertain_spelling"] else ""
            print(f"[{r.image}] MISSED  {m['username']}: expected {m['expected']}{flag}")
        for w in r.wrong:
            flag = " (uncertain spelling in expected.csv)" if w["uncertain_spelling"] else ""
            print(f"[{r.image}] WRONG   {w['username']}: expected {w['expected']} | read {w['read']}{flag}")
        for e in r.extra:
            print(f"[{r.image}] EXTRA   {e['username']}: {e['read']}")
    uncertain_present = sorted({e.username for e in expected if e.username in UNCERTAIN_SPELLINGS})
    print()
    print("Expected names with uncertain spellings (from the kit README): " + ", ".join(uncertain_present))
    print(f"Overall: {total_matched}/{total_expected} rows matched ({overall:.1f}%)")
    if args.json:
        Path(args.json).write_text(json.dumps({"extractor": chosen.value, "overall_percent": overall, "images": [asdict(r) for r in reports]}, indent=2), encoding="utf-8")
        print(f"Report written to {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
