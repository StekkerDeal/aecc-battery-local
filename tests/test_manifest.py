"""Guards on manifest.json and hacs.json.

The semver check is what keeps the beta flow working: the manifest must stay
plain X.Y.Z - pre-release suffixes (-beta.N) live only in git tags, because
HACS and hassfest reject them in the manifest.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(path: str) -> dict:
    return json.loads((REPO_ROOT / path).read_text(encoding="utf-8"))


def test_manifest_version_is_plain_semver() -> None:
    manifest = _load("custom_components/aecc_battery/manifest.json")
    parts = manifest["version"].split(".")
    assert len(parts) == 3, f"Version must be X.Y.Z, got '{manifest['version']}'"
    assert all(p.isdigit() for p in parts), f"No pre-release suffix in the manifest, got '{manifest['version']}'"


def test_manifest_required_fields() -> None:
    manifest = _load("custom_components/aecc_battery/manifest.json")
    assert manifest["domain"] == "aecc_battery"
    assert manifest["name"] == "AECC Battery (Local TCP)"
    assert manifest["config_flow"] is True
    assert manifest["iot_class"] == "local_polling"
    assert manifest["integration_type"] == "hub"
    # Pure stdlib on purpose: nothing to install, nothing to break.
    assert manifest["requirements"] == []


def test_hacs_json_matches_manifest_name() -> None:
    hacs = _load("hacs.json")
    manifest = _load("custom_components/aecc_battery/manifest.json")
    assert hacs["name"] == manifest["name"]
    assert hacs["render_readme"] is True


def test_brand_icons_present() -> None:
    brand = REPO_ROOT / "custom_components/aecc_battery/brand"
    assert (brand / "icon.png").is_file()
    assert (brand / "icon@2x.png").is_file()


def test_no_legacy_icon_copies() -> None:
    """Only brand/ is read by HA and HACS; the old copies were 830 KB each."""
    assert not (REPO_ROOT / "icon.png").exists()
    assert not (REPO_ROOT / "custom_components/aecc_battery/icon.png").exists()
