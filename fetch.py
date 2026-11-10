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
from urllib.parse import quote, unquote, urlsplit

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


def filename_version(filename, pattern):
    """The text of the named group `version` if `pattern` has one and matches."""
    if not pattern:
        return None
    m = re.search(pattern, filename or "")
    if not m or "version" not in m.re.groupindex:
        return None
    return m.group("version") or None


def apply_filename_version(meta, filename, pattern):
    """If `pattern` has a named group `version` that matches `filename`, use it
    as the release version (e.g. the tweak's own version) instead of the
    Info.plist one, which is the host app's version and can repeat across
    tweak releases. The original is kept as meta['hostVersion']."""
    version = filename_version(filename, pattern)
    if not version:
        return meta
    out = dict(meta)
    out["hostVersion"] = meta["version"]
    out["version"] = version
    return out


def clean_caption(text, drop_regex=None):
    """Tidy a Telegram caption for use as a changelog: drop lines matching
    `drop_regex` (e.g. donation links) and collapse extra blank lines."""
    lines = [ln.rstrip() for ln in (text or "").replace("\r", "").split("\n")]
    if drop_regex:
        lines = [ln for ln in lines if not re.search(drop_regex, ln)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def merge_description(existing, caption):
    """Add `caption` under the first paragraph of `existing` unless it is
    already there. Returns the (possibly unchanged) description."""
    existing = existing or ""
    if not caption or caption in existing:
        return existing
    head = existing.split("\n\n")[0].strip()
    return f"{head}\n\n{caption}" if head else caption


def version_exists(source, version):
    return any(v.get("version") == version
               for app in source.get("apps", []) for v in app.get("versions", []))


def refresh_description(source, version, caption):
    """Fill in a missing changelog on already-published versions.
    Returns the list of version entries that changed (empty if none)."""
    changed = []
    for app in source.get("apps", []):
        for v in app.get("versions", []):
            if v.get("version") == version:
                new = merge_description(v.get("localizedDescription", ""), caption)
                if new != v.get("localizedDescription"):
                    v["localizedDescription"] = new
                    changed.append(v)
    return changed


def tag_from_url(url):
    """Release tag out of a .../releases/download/<tag>/<file> URL."""
    m = re.search(r"/releases/download/([^/]+)/", url or "")
    return unquote(m.group(1)) if m else None


def new_source():
    return {
        "name": "Sparkle for Instagram",
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


def add_version(source, meta, *, repo, tag, filename, size, date, description="", url=None):
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
        "downloadURL": url or download_url(repo, tag, filename),
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
# Cloudflare R2 (optional): faster downloads than GitHub release assets
# --------------------------------------------------------------------------
R2_KEYS = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET", "R2_PUBLIC_URL")


def split_url(url):
    """(tag, filename) from a .../<tag>/<filename> download URL (GitHub or R2)."""
    parts = urlsplit(url).path.rstrip("/").split("/")
    return unquote(parts[-2]), unquote(parts[-1])


def r2_url(base, tag, filename):
    return f"{base.rstrip('/')}/{quote(tag, safe='')}/{quote(filename, safe='')}"


def r2_key(tag, filename):
    return f"{tag}/{filename}"


def is_r2_url(url, base):
    return (url or "").startswith(base.rstrip("/") + "/")


def plan_r2_sync(source, base, keep):
    """Newest `keep` versions of each app should live on R2; older ones fall back
    to their GitHub release asset so the bucket stays small.
    Returns (to_upload, to_prune) lists of version entries."""
    to_upload, to_prune = [], []
    for app in source.get("apps", []):
        for i, v in enumerate(app.get("versions", [])):
            on_r2 = is_r2_url(v.get("downloadURL"), base)
            if i < keep and not on_r2:
                to_upload.append(v)
            elif i >= keep and on_r2:
                to_prune.append(v)
    return to_upload, to_prune


def r2_config():
    """R2 settings from the environment, or None if R2 is not (fully) configured."""
    cfg = {k: os.environ.get(k, "").strip() for k in R2_KEYS}
    return cfg if all(cfg.values()) else None


def r2_client(cfg):
    import boto3
    from botocore.config import Config
    return boto3.client(
        "s3",
        endpoint_url=f"https://{cfg['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=cfg["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=cfg["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(signature_version="s3v4",
                      request_checksum_calculation="when_required",
                      response_checksum_validation="when_required"),
    )


def r2_upload(cfg, path, key):
    r2_client(cfg).upload_file(str(path), cfg["R2_BUCKET"], key,
                               ExtraArgs={"ContentType": "application/octet-stream"})


def r2_delete(cfg, key):
    r2_client(cfg).delete_object(Bucket=cfg["R2_BUCKET"], Key=key)


def sync_r2(source, cfg, repo, keep):
    """Move the newest versions to R2 (copying from their GitHub release) and
    push older ones back to GitHub, deleting them from the bucket."""
    base = cfg["R2_PUBLIC_URL"]
    to_upload, to_prune = plan_r2_sync(source, base, keep)
    for v in to_upload:
        tag, fname = split_url(v["downloadURL"])
        DOWNLOAD_DIR.mkdir(exist_ok=True)
        local = DOWNLOAD_DIR / fname
        try:
            log(f"Copying {tag}/{fname} to R2")
            gh("release", "download", tag, "--repo", repo, "--pattern", fname,
               "--dir", str(DOWNLOAD_DIR), "--clobber")
            r2_upload(cfg, local, r2_key(tag, fname))
            v["downloadURL"] = r2_url(base, tag, fname)
        except Exception as e:  # keep the working GitHub URL
            log(f"Could not copy {tag}/{fname} to R2: {e!r}")
        finally:
            local.unlink(missing_ok=True)
    for v in to_prune:
        tag, fname = split_url(v["downloadURL"])
        try:
            r2_delete(cfg, r2_key(tag, fname))
            v["downloadURL"] = download_url(repo, tag, fname)
            log(f"Pruned {tag}/{fname} from R2 (now served from GitHub)")
        except Exception as e:
            log(f"Could not prune {tag}/{fname} from R2: {e!r}")
    for app in source.get("apps", []):
        if app.get("versions"):
            sync_legacy_fields(app)
    return bool(to_upload or to_prune)


# --------------------------------------------------------------------------
# Main flow
# --------------------------------------------------------------------------
def log(msg):
    print(msg, flush=True)


async def find_caption(client, channel, msg):
    """Caption text for an IPA post. Telegram albums keep the caption on just
    one of the files, so look at neighbouring messages in the same album."""
    if (msg.message or "").strip():
        return msg.message
    gid = getattr(msg, "grouped_id", None)
    if not gid:
        return ""
    try:
        ids = list(range(max(1, msg.id - 10), msg.id + 11))
        for m in await client.get_messages(channel, ids=ids):
            if m and getattr(m, "grouped_id", None) == gid and (m.message or "").strip():
                return m.message
    except Exception as e:  # a missing changelog must not block publishing
        log(f"Could not look up album caption for #{msg.id}: {e!r}")
    return ""


SCAN_LIMIT = 300  # how many recent messages to look through for the newest IPA


async def candidates(client, channel, last_id, pattern):
    """Yield the messages to handle.

    Default: only the newest post that has a matching IPA, on every run. Anything
    already published is skipped without downloading, so this is cheap.
    BACKFILL=1: every message after the saved state, oldest first (whole history
    if the state is 0)."""
    if os.environ.get("BACKFILL"):
        async for msg in client.iter_messages(channel, min_id=last_id, reverse=True):
            if msg.id > last_id:
                yield msg
        return
    async for msg in client.iter_messages(channel, limit=SCAN_LIMIT):  # newest first
        name = message_filename(msg)
        if matches_filter(name, pattern):
            log(f"Newest matching IPA is #{msg.id}: {name}")
            yield msg
            return
        if is_ipa_name(name):
            log(f"Ignoring #{msg.id}: {name} (does not match IPA_FILENAME_REGEX)")
    log(f"No matching IPA in the last {SCAN_LIMIT} messages")


async def run(dry_run):
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]
    session = os.environ["TG_SESSION"]
    channel = os.environ["TG_CHANNEL"].strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "<user>/<repo>")
    pattern = os.environ.get("IPA_FILENAME_REGEX", "").strip() or None
    drop = os.environ.get("CAPTION_DROP_REGEX", "").strip() or None
    r2 = r2_config()
    keep = int(os.environ.get("R2_KEEP", "").strip() or 5)
    log("Cloudflare R2: " + ("enabled" if r2 else "not configured, using GitHub release URLs"))
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

            caption = clean_caption(await find_caption(client, channel, msg), drop)
            if not caption:
                log(f"No caption found for #{msg.id}")

            # Already published (by the tweak version in the filename)? Skip the
            # big download, only fill in a missing changelog.
            fv = filename_version(filename, pattern)
            if fv and version_exists(source, fv):
                changed = refresh_description(source, fv, caption)
                if changed:
                    save_json(SOURCE_PATH, source)
                    log(f"Updated changelog for {fv}")
                    for v in changed:  # keep the GitHub release notes in sync too
                        tag = tag_from_url(v.get("downloadURL"))
                        try:
                            if tag:
                                gh("release", "edit", tag, "--repo", repo,
                                   "--notes", v["localizedDescription"])
                        except subprocess.CalledProcessError as e:
                            log(f"Could not update release notes for {tag}: {e.stderr}")
                log(f"Skip #{msg.id}: {fv} already published")
                last_id = msg.id
                state["last_message_id"] = last_id
                save_json(STATE_PATH, state)
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

            url = None
            if r2:
                try:
                    r2_upload(r2, clean, r2_key(tag, clean.name))
                    url = r2_url(r2["R2_PUBLIC_URL"], tag, clean.name)
                except Exception as e:  # fall back to the GitHub asset
                    log(f"R2 upload failed for {tag}: {e!r}; using the GitHub URL")
            add_version(
                source, meta, repo=repo, tag=tag, filename=clean.name,
                size=clean.stat().st_size,
                date=msg.date.astimezone(timezone.utc).strftime("%Y-%m-%d"),
                description=caption, url=url,
            )
            clean.unlink(missing_ok=True)
            last_id = msg.id
            # Persist after every IPA so partial progress survives a later failure.
            save_json(SOURCE_PATH, source)
            state["last_message_id"] = last_id
            save_json(STATE_PATH, state)
            log(f"Published {tag}")

    if not dry_run:
        if r2:
            sync_r2(source, r2, repo, keep)
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
