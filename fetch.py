#!/usr/bin/env python3
"""Download new Instagram IPAs from a Telegram channel, publish them as GitHub
Release assets and keep an AltStore-style source.json up to date."""
import argparse
import asyncio
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent
SOURCE_PATH = ROOT / "source.json"
STATE_PATH = ROOT / "state.json"
DOWNLOAD_DIR = ROOT / "downloads"

DEFAULT_DEVELOPER = "Meta"
DEFAULT_TINT = "#E1306C"
PLIST_RE = re.compile(r"^Payload/[^/]+\.app/Info\.plist$")


# --------------------------------------------------------------------------
# Pure logic (covered by tests/test_logic.py)
# --------------------------------------------------------------------------
def load_json(path, default):
    path = Path(path)
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def sanitize_filename(name):
    """Make a string safe for use as a release asset / URL filename."""
    name = os.path.basename(name).strip().replace(" ", "-")
    name = re.sub(r"[^A-Za-z0-9._-]", "", name)
    name = re.sub(r"-{2,}", "-", name).strip("-.")
    return name or "app"


def clean_ipa_name(app_name, version):
    return f"{sanitize_filename(app_name)}-{sanitize_filename(version)}.ipa"


def make_tag(version, build):
    tag = f"v{version}-{build}" if build else f"v{version}"
    return re.sub(r"[^A-Za-z0-9._-]", "", tag.replace(" ", "-"))


def download_url(repo, tag, filename):
    return (
        f"https://github.com/{repo}/releases/download/"
        f"{quote(tag, safe='')}/{quote(filename, safe='')}"
    )


def extract_metadata(ipa_path):
    """Read Payload/*.app/Info.plist from an IPA and return the fields we need."""
    with zipfile.ZipFile(ipa_path) as z:
        plists = [n for n in z.namelist() if PLIST_RE.match(n)]
        if not plists:
            raise ValueError(f"{ipa_path}: no Payload/*.app/Info.plist found")
        info = plistlib.loads(z.read(plists[0]))
    return {
        "bundleIdentifier": info["CFBundleIdentifier"],
        "version": str(info.get("CFBundleShortVersionString") or info["CFBundleVersion"]),
        "build": str(info.get("CFBundleVersion", "")),
        "name": info.get("CFBundleDisplayName") or info.get("CFBundleName") or "App",
        "minOSVersion": str(info.get("MinimumOSVersion", "")),
    }


def apply_filename_version(meta, filename, pattern):
    """If `pattern` has a named group `version` that matches `filename`, use it
    as the release version (e.g. the tweak's own version) instead of the
    Info.plist one, which is the host app's version and can repeat across
    tweak releases. The original is kept as meta['hostVersion']."""
    if not pattern:
        return meta
    m = re.search(pattern, filename or "")
    if not m or "version" not in m.re.groupindex or not m.group("version"):
        return meta
    out = dict(meta)
    out["hostVersion"] = meta["version"]
    out["version"] = m.group("version")
    return out


def new_source():
    return {
        "name": "My IPA Source",
        "identifier": "com.<user>.ipa-source",
        "subtitle": "Auto-updated from Telegram",
        "description": "Automatically updated IPAs.",
        "iconURL": "https://raw.githubusercontent.com/<user>/<repo>/main/icon.png",
        "website": "https://github.com/<user>/<repo>",
        "tintColor": DEFAULT_TINT,
        "apps": [],
        "news": [],
    }


def fill_placeholders(obj, repo):
    """Replace <user>/<repo> placeholders (recursively) using 'owner/name'."""
    user, _, name = repo.partition("/")
    if isinstance(obj, str):
        return obj.replace("<user>", user).replace("<repo>", name)
    if isinstance(obj, list):
        return [fill_placeholders(x, repo) for x in obj]
    if isinstance(obj, dict):
        return {k: fill_placeholders(v, repo) for k, v in obj.items()}
    return obj


def find_app(source, bundle_id):
    for app in source.get("apps", []):
        if app.get("bundleIdentifier") == bundle_id:
            return app
    return None


def is_duplicate(source, bundle_id, version):
    app = find_app(source, bundle_id)
    return bool(app) and any(v.get("version") == version for v in app.get("versions", []))


def add_version(source, meta, *, repo, tag, filename, size, date, description=""):
    """Insert a version at the front of the app's list (creating the app if
    needed) and sync the legacy top-level fields. Returns False on duplicate."""
    if is_duplicate(source, meta["bundleIdentifier"], meta["version"]):
        return False
    app = find_app(source, meta["bundleIdentifier"])
    if app is None:
        app = {
            "name": meta["name"],
            "bundleIdentifier": meta["bundleIdentifier"],
            "developerName": DEFAULT_DEVELOPER,
            "subtitle": f"Latest {meta['name']}",
            "localizedDescription": f"Auto-updated {meta['name']} IPA.",
            "iconURL": f"https://raw.githubusercontent.com/{repo}/main/instagram.png",
            "tintColor": DEFAULT_TINT,
            "screenshotURLs": [],
            "versions": [],
        }
        source.setdefault("apps", []).append(app)

    entry = {
        "version": meta["version"],
        "buildVersion": meta["build"],
        "date": date,
        "localizedDescription": description,
        "downloadURL": download_url(repo, tag, filename),
        "size": size,
        "minOSVersion": meta["minOSVersion"],
    }
    app.setdefault("versions", []).insert(0, entry)
    sync_legacy_fields(app)
    return True


def sync_legacy_fields(app):
    latest = app["versions"][0]
    app["version"] = latest["version"]
    app["versionDate"] = latest["date"]
    app["downloadURL"] = latest["downloadURL"]
    app["size"] = latest["size"]


def is_ipa_name(name):
    return bool(name) and name.lower().endswith(".ipa")


def matches_filter(name, pattern=None):
    """True for an .ipa whose filename matches the optional regex `pattern`."""
    if not is_ipa_name(name):
        return False
    return not pattern or re.search(pattern, name) is not None


def message_filename(msg):
    return getattr(msg.file, "name", None) if msg.document else None


# --------------------------------------------------------------------------
# GitHub release upload (gh CLI)
# --------------------------------------------------------------------------
def gh(*args, check=True):
    return subprocess.run(["gh", *args], check=check, capture_output=True, text=True)


def publish_release(repo, tag, path, title, notes):
    """Create the release, or just (re)upload the asset if the tag already
    exists (e.g. a previous run died before committing source.json)."""
    exists = gh("release", "view", tag, "--repo", repo, check=False).returncode == 0
    if exists:
        gh("release", "upload", tag, str(path), "--repo", repo, "--clobber")
    else:
        gh("release", "create", tag, str(path), "--repo", repo, "--title", title, "--notes", notes)


# --------------------------------------------------------------------------
# Main flow
# --------------------------------------------------------------------------
def log(msg):
    print(msg, flush=True)


async def candidates(client, channel, last_id, pattern):
    """Yield messages to look at, oldest first.

    On the very first run (no state) only the newest matching IPA is taken, so
    a long channel history is not downloaded. Set BACKFILL=1 to override."""
    if last_id == 0 and not os.environ.get("BACKFILL"):
        log("First run: taking only the newest matching IPA (BACKFILL=1 processes all history)")
        async for msg in client.iter_messages(channel):  # newest first
            if matches_filter(message_filename(msg), pattern):
                yield msg
                return
        return
    async for msg in client.iter_messages(channel, min_id=last_id, reverse=True):
        if msg.id > last_id:
            yield msg


async def run(dry_run):
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]
    session = os.environ["TG_SESSION"]
    channel = os.environ["TG_CHANNEL"].strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "<user>/<repo>")
    pattern = os.environ.get("IPA_FILENAME_REGEX", "").strip() or None
    if re.fullmatch(r"-?\d+", channel):
        channel = int(channel)

    state = load_json(STATE_PATH, {"last_message_id": 0})
    last_id = int(state.get("last_message_id", 0))
    source = fill_placeholders(load_json(SOURCE_PATH, new_source()), repo)
    log(f"Last processed message id: {last_id}")

    async with TelegramClient(StringSession(session), api_id, api_hash) as client:
        async for msg in candidates(client, channel, last_id, pattern):
            filename = message_filename(msg)
            if not matches_filter(filename, pattern):
                last_id = msg.id  # not the file we track; skip past it
                continue

            if dry_run:
                log(f"[dry-run] would process #{msg.id}: {filename} ({msg.file.size} bytes)")
                continue

            DOWNLOAD_DIR.mkdir(exist_ok=True)
            tmp = DOWNLOAD_DIR / f"{msg.id}-{sanitize_filename(filename)}"
            try:
                log(f"Downloading #{msg.id}: {filename}")
                await client.download_media(msg, file=str(tmp))
                meta = apply_filename_version(extract_metadata(tmp), filename, pattern)
            except Exception as e:  # download or parse failure: stop, keep state
                log(f"ERROR on message #{msg.id} ({filename}): {e!r}; will retry next run")
                tmp.unlink(missing_ok=True)
                break

            if is_duplicate(source, meta["bundleIdentifier"], meta["version"]):
                log(f"Skip #{msg.id}: {meta['bundleIdentifier']} {meta['version']} already published")
                tmp.unlink(missing_ok=True)
                last_id = msg.id
                state["last_message_id"] = last_id
                save_json(STATE_PATH, state)
                continue

            clean = DOWNLOAD_DIR / clean_ipa_name(meta["name"], meta["version"])
            shutil.move(tmp, clean)
            tag = make_tag(meta["version"], meta["build"])
            caption = (msg.message or "").strip()
            host = meta.get("hostVersion")
            title = f"{meta['name']} {meta['version']} ({meta['build']})"
            if host:
                title = f"{meta['name']} {meta['version']} (IG {host})"
                caption = f"Instagram {host}\n\n{caption}".strip()
            try:
                publish_release(repo, tag, clean, title, caption or title)
            except subprocess.CalledProcessError as e:
                log(f"ERROR publishing {tag}: {e.stderr}; will retry next run")
                clean.unlink(missing_ok=True)
                break

            add_version(
                source, meta, repo=repo, tag=tag, filename=clean.name,
                size=clean.stat().st_size,
                date=msg.date.astimezone(timezone.utc).strftime("%Y-%m-%d"),
                description=caption,
            )
            clean.unlink(missing_ok=True)
            last_id = msg.id
            # Persist after every IPA so partial progress survives a later failure.
            save_json(SOURCE_PATH, source)
            state["last_message_id"] = last_id
            save_json(STATE_PATH, state)
            log(f"Published {tag}")

    if not dry_run:
        save_json(SOURCE_PATH, source)
        state["last_message_id"] = last_id
        save_json(STATE_PATH, state)
        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="list IPAs that would be processed; no download, upload or file edits")
    args = ap.parse_args()
    asyncio.run(run(args.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
