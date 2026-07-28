#!/usr/bin/env python3
"""
episodeskiparr — companion for autobrr and packgrabbarr.

Given a grabbed season-pack release, looks up which episodes already exist
in Sonarr, finds the matching torrent in qBittorrent, and sets file priority
to 0 ("do not download") on the ones already owned so only the missing
episodes get pulled down.

Unlike seasonpackarr, this matches by episode number rather than hardlinks
or seeding state, so it works with mixed release groups and alongside
Decluttarr removing torrents after import.

See README for the /api/skip request/response shape.
"""

import os
import re
import sys
import time
import logging

from flask import Flask, request, jsonify
import qbittorrentapi
import requests

# --------------------------------------------------------------------------- #
# Configuration (all via environment variables)
# --------------------------------------------------------------------------- #
QB_HOST   = os.environ.get("QB_HOST", "qbittorrent")
QB_PORT   = int(os.environ.get("QB_PORT", "8080"))
QB_USER   = os.environ.get("QB_USER", "")
QB_PASS   = os.environ.get("QB_PASS", "")
if bool(QB_USER) != bool(QB_PASS):
    raise SystemExit(
        "QB_USER and QB_PASS must both be set, or both left unset to rely on "
        "qBittorrent's IP-whitelist auth bypass"
    )

SONARR_URL     = os.environ.get("SONARR_URL", "http://sonarr:8989").rstrip("/")
SONARR_API_KEY = os.environ.get("SONARR_API_KEY", "")

QB_CATEGORY = os.environ.get("QB_CATEGORY", "tv-sonarr")

# How long to wait for the torrent to appear in qB after autobrr pushes it.
POLL_TIMEOUT  = int(os.environ.get("POLL_TIMEOUT", "60"))   # seconds
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "2")) # seconds

# Optional shared secret. If set, requests must send ?token=... matching it.
API_TOKEN = os.environ.get("API_TOKEN", "")

# Test-only: pretend these episode numbers already exist (comma-separated).
FAKE_HAVE_EPISODES = os.environ.get("FAKE_HAVE_EPISODES", "")

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("episodeskiparr")

app = Flask(__name__)


# --------------------------------------------------------------------------- #
# Parsing the release / torrent name
# --------------------------------------------------------------------------- #
def parse_season(name: str):
    """Extract the season number from a release name (RuTracker or scene style). Returns int or None."""
    # S5E1-7 / S05E01-08 / S5 / S05
    m = re.search(r"[Ss](\d{1,2})(?:[EeЕе]\d|\b)", name)
    if m:
        return int(m.group(1))
    m = re.search(r"[Ss](\d{1,2})\b", name)
    if m:
        return int(m.group(1))
    # Russian "Сезон: 5"
    m = re.search(r"[Сс]езон[:\s]+(\d{1,2})", name)
    if m:
        return int(m.group(1))
    return None


def parse_title_candidates(name: str):
    """Return candidate show titles parsed out of a RuTracker- or scene-style release name, most specific first."""
    # Cut everything from the season marker onward
    cut = re.split(r"[Ss]\d{1,2}[EeЕе]", name)[0]
    cut = re.split(r"[Ss]\d{1,2}\b", cut)[0]      # also cut bare "S05"
    cut = re.split(r"[Сс]езон", cut)[0]

    cands = []
    # RuTracker: split on '/'
    for p in cut.split("/"):
        p = re.sub(r"\[.*", "", p).strip()
        p = p.strip(" .-")
        if p and p not in cands:
            cands.append(p)

    # scene: dots as separators -> "The.Boys" => "The Boys"
    dotted = cut.replace(".", " ").strip(" .-")
    dotted = re.sub(r"\s+", " ", dotted)
    if dotted and dotted not in cands:
        cands.append(dotted)

    return cands


def episode_numbers_in_filename(filename: str):
    """Return the set of episode numbers a torrent file name represents (SxxEyy, ranges, or NxNN); empty if none."""
    base = filename.rsplit("/", 1)[-1]
    nums = set()

    # S05E01E02E03  or  S05E01
    for m in re.finditer(r"[Ss]\d{1,2}((?:[EeЕе]\d{1,3})+)", base):
        for em in re.finditer(r"[EeЕе](\d{1,3})", m.group(1)):
            nums.add(int(em.group(1)))

    # S05E01-E07  or S05E01-07 (range)
    m = re.search(r"[Ss]\d{1,2}[EeЕе](\d{1,3})\s*-\s*[EeЕе]?(\d{1,3})", base)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a <= b <= a + 100:
            nums.update(range(a, b + 1))

    # 5x07
    for m in re.finditer(r"\b\d{1,2}x(\d{1,3})\b", base):
        nums.add(int(m.group(1)))

    return nums


# --------------------------------------------------------------------------- #
# Sonarr
# --------------------------------------------------------------------------- #
def sonarr_get(path, **params):
    url = f"{SONARR_URL}/api/v3/{path}"
    headers = {"X-Api-Key": SONARR_API_KEY}
    r = requests.get(url, headers=headers, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def find_series_id(title_candidates):
    """Match a release title against Sonarr's series list (incl. alt titles)."""
    series = sonarr_get("series")

    def norm(s):
        return re.sub(r"[^a-zа-я0-9]", "", s.lower())

    cand_norm = [norm(c) for c in title_candidates]

    for s in series:
        titles = [s.get("title", "")]
        titles += [a.get("title", "") for a in s.get("alternateTitles", [])]
        for t in titles:
            tn = norm(t)
            if not tn:
                continue
            for cn in cand_norm:
                if cn and (cn == tn or cn in tn or tn in cn):
                    log.info("Matched Sonarr series '%s' (id=%s) via candidate '%s'",
                             s.get("title"), s.get("id"), t)
                    return s.get("id"), s.get("title")
    return None, None


def episodes_with_files(series_id, season):
    """Return set of episode numbers in this season that already have a file."""
    eps = sonarr_get("episode", seriesId=series_id)
    have = set()
    for e in eps:
        if e.get("seasonNumber") == season and e.get("hasFile"):
            have.add(e.get("episodeNumber"))
    return have


# --------------------------------------------------------------------------- #
# qBittorrent
# --------------------------------------------------------------------------- #
def qb_client():
    c = qbittorrentapi.Client(
        host=QB_HOST, port=QB_PORT,
        REQUESTS_ARGS={"timeout": 30},
    )
    # Auth not required when qB bypasses by whitelisted IP subnet.
    # Try login only if creds are provided; ignore failure (bypass covers it).
    if QB_USER and QB_PASS:
        try:
            c.auth_log_in()
        except qbittorrentapi.exceptions.LoginFailed:
            pass
    return c


def find_torrent(client, torrent_name):
    """Loosely match the torrent by name within QB_CATEGORY (qB may rename slightly). Returns hash or None."""
    def norm(s):
        return re.sub(r"[^a-z0-9]", "", s.lower())

    target = norm(torrent_name)
    best = None
    for t in client.torrents_info(category=QB_CATEGORY):
        tn = norm(t.name)
        if tn == target or target in tn or tn in target:
            return t.hash
        # fallback: remember any torrent if exact match fails
        best = best or None
    return best


def wait_for_torrent(client, torrent_name):
    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        h = find_torrent(client, torrent_name)
        if h:
            return h
        time.sleep(POLL_INTERVAL)
    return None


def wait_for_download_hash(series_id, season):
    """Poll Sonarr's queue for this series+season's downloadId (torrent hash) —
    more reliable than matching qBittorrent by name."""
    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        try:
            q = sonarr_get("queue", pageSize=1000, includeEpisode="true")
        except Exception as e:
            log.warning("Sonarr queue read failed: %s", e)
            q = {}
        for rec in (q.get("records") or []):
            if rec.get("seriesId") != series_id:
                continue
            sn = rec.get("seasonNumber")
            if sn is None:
                sn = (rec.get("episode") or {}).get("seasonNumber")
            if sn is not None and sn != season:
                continue
            dlid = rec.get("downloadId")
            if dlid:
                return dlid.lower()
        time.sleep(POLL_INTERVAL)
    return None


# --------------------------------------------------------------------------- #
# Core logic
# --------------------------------------------------------------------------- #
def process(torrent_name: str, keep_episodes=None, season_override=None):
    season = season_override if season_override is not None else parse_season(torrent_name)
    if season is None:
        return False, f"could not parse season from '{torrent_name}'"

    cands = parse_title_candidates(torrent_name)
    if not cands:
        return False, f"could not parse title from '{torrent_name}'"
    log.info("Parsed: season=%s, title candidates=%s", season, cands)

    series_id, series_title = find_series_id(cands)
    if series_id is None:
        return False, f"no Sonarr series matched candidates {cands}"

    keep_set = set(keep_episodes) if keep_episodes else None
    have = set()

    if keep_set is not None:
        # Explicit keep-list from packgrabbarr: download exactly these episodes
        # (missing + genuine upgrades) and drop everything else — even episodes
        # we already have at lower quality. This is what lets a single episode
        # be upgraded out of a pack.
        log.info("Series '%s' S%02d keep-list from caller: %s",
                 series_title, season, sorted(keep_set))
    else:
        # Legacy path (e.g. autobrr): keep only episodes we don't already have.
        have = episodes_with_files(series_id, season)

        # Inject fake episodes for testing
        if FAKE_HAVE_EPISODES.strip():
            fake = {int(x) for x in re.findall(r"\d+", FAKE_HAVE_EPISODES)}
            log.warning("TEST MODE: injecting fake existing episodes %s", fake)
            have |= fake

        log.info("Series '%s' S%02d already has episodes: %s",
                 series_title, season, sorted(have))

        if not have:
            return True, "no existing episodes; nothing to skip (full download)"

    client = qb_client()

    # Identify the torrent by infohash (robust), not by name.
    h = None
    dlhash = wait_for_download_hash(series_id, season)
    if dlhash:
        try:
            info = client.torrents_info(torrent_hashes=dlhash)
            if info:
                h = info[0].hash
                log.info("Matched torrent by hash %s: %s", h, info[0].name)
        except Exception as e:
            log.warning("qB lookup by hash failed: %s", e)

    # Fallback: legacy name matching (e.g. if no Sonarr queue record was found).
    if not h:
        log.info("hash lookup failed; falling back to name match")
        h = wait_for_torrent(client, torrent_name)

    if not h:
        return False, (f"torrent not found via Sonarr queue hash or name "
                       f"in category '{QB_CATEGORY}' within {POLL_TIMEOUT}s")

    files = client.torrents_files(torrent_hash=h)

    skip_ids = []
    keep = []
    for f in files:
        ep_nums = episode_numbers_in_filename(f.name)
        if keep_set is not None:
            # Drop files whose episodes are all outside the keep-list.
            drop = bool(ep_nums) and not (ep_nums & keep_set)
        else:
            # Drop files whose episodes we already have.
            drop = bool(ep_nums) and ep_nums.issubset(have)
        if drop:
            skip_ids.append(f.id)
            log.info("SKIP  idx=%s eps=%s  %s", f.id, sorted(ep_nums), f.name)
        else:
            keep.append((f.id, sorted(ep_nums), f.name))
            log.info("KEEP  idx=%s eps=%s  %s", f.id, sorted(ep_nums), f.name)

    if not skip_ids:
        return True, "no files needed skipping; downloading all"

    # priority 0 = Do not download
    client.torrents_file_priority(
        torrent_hash=h,
        file_ids="|".join(str(i) for i in skip_ids),
        priority=0,
    )
    if keep_set is not None:
        msg = (f"kept {len(keep)} file(s) for episodes {sorted(keep_set)}; "
               f"skipped {len(skip_ids)} file(s)")
    else:
        msg = (f"skipped {len(skip_ids)} file(s) for existing episodes {sorted(have)}; "
               f"kept {len(keep)} file(s)")
    log.info(msg)
    return True, msg


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
@app.route("/api/skip", methods=["POST"])
def skip():
    if API_TOKEN:
        token = request.args.get("token") or request.headers.get("X-API-Token")
        if token != API_TOKEN:
            return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(force=True, silent=True) or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "missing 'name'"}), 400

    # Optional explicit instructions from packgrabbarr: which episodes to KEEP
    # (download) and the season. Absent for autobrr -> legacy have-based logic.
    episodes = data.get("episodes")
    if episodes is not None:
        try:
            episodes = [int(x) for x in episodes]
        except (TypeError, ValueError):
            episodes = None
    season = data.get("season")
    if season is not None:
        try:
            season = int(season)
        except (TypeError, ValueError):
            season = None

    log.info("=== /api/skip request: %s (season=%s keep=%s)", name, season, episodes)
    try:
        ok, msg = process(name, keep_episodes=episodes, season_override=season)
    except Exception as e:
        log.exception("processing failed")
        return jsonify({"error": str(e)}), 500

    # autobrr's external filter expects a specific status (we use 250) to mean
    # "approved / proceed". We always approve the grab; skipping is best-effort.
    code = 250 if ok else 250
    return jsonify({"ok": ok, "message": msg}), code


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    if not SONARR_API_KEY:
        log.warning("SONARR_API_KEY is empty — Sonarr lookups will fail")
    if not QB_USER:
        log.warning("QB_USER/QB_PASS not set — relying on qBittorrent's IP-whitelist auth bypass")
    port = int(os.environ.get("PORT", "8088"))
    log.info("episodeskiparr starting on :%d (qb=%s:%d cat=%s sonarr=%s)",
             port, QB_HOST, QB_PORT, QB_CATEGORY, SONARR_URL)
    app.run(host="0.0.0.0", port=port)
