from __future__ import annotations

import sys

from rankrecovery import bootstrap


def test_only_matching_site_packages_is_added(tmp_path, monkeypatch):
    other = tmp_path / ".local" / "lib" / "python3.8" / "site-packages"
    mine = bootstrap.local_site_packages(tmp_path)
    other.mkdir(parents=True)
    mine.mkdir(parents=True)
    monkeypatch.setattr(sys, "path", list(sys.path))
    bootstrap.add_paths(tmp_path)
    assert str(mine) in sys.path and str(other) not in sys.path


def test_missing_modules_detects_absent(monkeypatch):
    monkeypatch.setattr(bootstrap, "SENTINEL_MODULES", ("sys", "definitely_not_a_module_xyz"))
    assert bootstrap.missing_modules() == ["definitely_not_a_module_xyz"]


def test_install_requirements_without_file(tmp_path):
    assert bootstrap.install_requirements(tmp_path) is False
