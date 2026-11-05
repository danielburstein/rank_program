from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
def test_sample_accuracy_script_runs_with_ocr(tmp_path):
    out = tmp_path / "report.json"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "sample_accuracy.py"), "--samples", str(ROOT / "samples"), "--extractor", "ocr", "--json", str(out)],
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert proc.returncode == 0, proc.stderr
    assert "TOTAL" in proc.stdout and "Overall:" in proc.stdout
    assert out.exists()


def test_scoring_logic():
    sys.path.insert(0, str(ROOT))
    from rankrecovery.core.enums import RegionKind
    from rankrecovery.core.parsing import RawRow, RowParser
    from rankrecovery.core.ranks import default_policy
    from scripts.sample_accuracy import ExpectedRow, score_image

    policy = default_policy()
    parser = RowParser(policy)
    dets = [
        parser.parse(RawRow(text="", region=RegionKind.TAB, prefix_text="Titan", username_text="AndrewIsFire")),
        parser.parse(RawRow(text="", region=RegionKind.TAB, prefix_text="Sr Mod", username_text="2098ms", trailing_icon="infinity")),  # type: ignore[arg-type]
        parser.parse(RawRow(text="", region=RegionKind.TAB, prefix_text="Hero", username_text="Oskar2")),
        parser.parse(RawRow(text="", region=RegionKind.TAB, prefix_text="Galactic", username_text="Zeyos4Dan", username_cut_off=True)),
    ]
    expected = [
        ExpectedRow("img", "AndrewIsFire", "Titan", "TITAN"),
        ExpectedRow("img", "2098ms", "Sr Mod", "INFINITE"),
        ExpectedRow("img", "Oskar2", "Legend", "LEGEND"),
        ExpectedRow("img", "Missing", "Hero", "HERO"),
    ]
    report = score_image("img", expected, [d for d in dets if d], policy)
    assert report.matched == 2 and len(report.wrong) == 1 and len(report.missed) == 1 and report.partial_or_unreadable == 1
