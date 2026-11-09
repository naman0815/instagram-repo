import json
import plistlib
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fetch  # noqa: E402

REPO = "alice/ipa-source"


def make_ipa(path, *, bundle="com.burbn.instagram", short="400.0", build="123456789",
             name="Instagram", min_os="15.0", binary=False):
    info = {
        "CFBundleIdentifier": bundle,
        "CFBundleShortVersionString": short,
        "CFBundleVersion": build,
        "CFBundleDisplayName": name,
        "MinimumOSVersion": min_os,
    }
    fmt = plistlib.FMT_BINARY if binary else plistlib.FMT_XML
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Payload/Instagram.app/Info.plist", plistlib.dumps(info, fmt=fmt))
        z.writestr("Payload/Instagram.app/Frameworks/Other.framework/Info.plist", b"ignored")
    return path


def meta(**kw):
    base = dict(bundleIdentifier="com.burbn.instagram", version="400.0", build="1",
                name="Instagram", minOSVersion="15.0")
    base.update(kw)
    return base


def add(source, **kw):
    m = kw.pop("m", meta())
    args = dict(repo=REPO, tag=fetch.make_tag(m["version"], m["build"]),
                filename=f"Instagram-{m['version']}.ipa", size=100,
                date="2026-11-09", description="")
    args.update(kw)
    return fetch.add_version(source, m, **args)


# ---- metadata extraction ------------------------------------------------
@pytest.mark.parametrize("binary", [False, True])
def test_extract_metadata(tmp_path, binary):
    ipa = make_ipa(tmp_path / "x.ipa", binary=binary)
    m = fetch.extract_metadata(ipa)
    assert m == {
        "bundleIdentifier": "com.burbn.instagram",
        "version": "400.0",
        "build": "123456789",
        "name": "Instagram",
        "minOSVersion": "15.0",
    }


def test_extract_metadata_uses_tweaked_bundle_id(tmp_path):
    ipa = make_ipa(tmp_path / "x.ipa", bundle="com.example.rocket")
    assert fetch.extract_metadata(ipa)["bundleIdentifier"] == "com.example.rocket"


def test_extract_metadata_without_plist(tmp_path):
    p = tmp_path / "bad.ipa"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("readme.txt", "hi")
    with pytest.raises(ValueError):
        fetch.extract_metadata(p)


# ---- source.json updates ------------------------------------------------
def test_first_ipa_creates_app_entry():
    src = fetch.new_source()
    assert add(src, description="notes") is True
    app = src["apps"][0]
    assert app["bundleIdentifier"] == "com.burbn.instagram"
    v = app["versions"][0]
    assert v["version"] == "400.0" and v["localizedDescription"] == "notes"
    assert v["downloadURL"] == (
        f"https://github.com/{REPO}/releases/download/v400.0-1/Instagram-400.0.ipa")


def test_new_version_inserted_at_front_and_legacy_fields_track_latest():
    src = fetch.new_source()
    add(src, m=meta(version="400.0", build="1"), date="2026-11-01", size=10)
    add(src, m=meta(version="401.0", build="2"), date="2026-11-09", size=20)
    app = src["apps"][0]
    assert [v["version"] for v in app["versions"]] == ["401.0", "400.0"]
    assert app["version"] == "401.0"
    assert app["versionDate"] == "2026-11-09"
    assert app["size"] == 20
    assert app["downloadURL"] == app["versions"][0]["downloadURL"]


def test_duplicate_version_is_skipped():
    src = fetch.new_source()
    assert add(src) is True
    snapshot = json.dumps(src, sort_keys=True)
    assert add(src) is False
    assert json.dumps(src, sort_keys=True) == snapshot


def test_same_version_different_bundle_is_not_duplicate():
    src = fetch.new_source()
    add(src)
    assert add(src, m=meta(bundleIdentifier="com.example.rocket")) is True
    assert len(src["apps"]) == 2


def test_fill_placeholders():
    out = fetch.fill_placeholders(fetch.new_source(), REPO)
    assert out["identifier"] == "com.alice.ipa-source"
    assert out["website"] == "https://github.com/alice/ipa-source"
    assert "<" not in json.dumps(out)


def test_save_json_format(tmp_path):
    p = tmp_path / "s.json"
    fetch.save_json(p, {"a": [1]})
    assert p.read_text() == '{\n  "a": [\n    1\n  ]\n}\n'


# ---- naming -------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("Instagram 400.0 (final).ipa", "Instagram-400.0-final.ipa"),
    ("../../etc/passwd.ipa", "passwd.ipa"),
    ("café #1?.ipa", "caf-1.ipa"),
    ("   ", "app"),
])
def test_sanitize_filename(raw, expected):
    assert fetch.sanitize_filename(raw) == expected


def test_clean_name_tag_and_url():
    assert fetch.clean_ipa_name("Instagram", "400.0") == "Instagram-400.0.ipa"
    assert fetch.make_tag("400.0", "123") == "v400.0-123"
    assert fetch.make_tag("400.0", "") == "v400.0"
    assert fetch.download_url(REPO, "v1", "a b.ipa").endswith("/v1/a%20b.ipa")


def test_is_ipa_name():
    assert fetch.is_ipa_name("A.IPA") and fetch.is_ipa_name("a.ipa")
    assert not fetch.is_ipa_name("a.zip") and not fetch.is_ipa_name(None)
