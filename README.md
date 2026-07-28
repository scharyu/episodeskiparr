# episodeskiparr

![License](https://img.shields.io/badge/license-GPL--3.0-blue)
![Python](https://img.shields.io/badge/python-3.12-blue)

Skips already-owned episodes in season-pack torrents by setting qBittorrent
file priorities, using Sonarr as the source of truth.

When a season-pack release gets grabbed, episodeskiparr looks up which
episodes of that show/season already have a file in Sonarr, finds the
matching torrent in qBittorrent, and sets file priority to `0` ("do not
download") on the episodes you already have. qBittorrent then only pulls
down what's actually missing.

Unlike [seasonpackarr](https://github.com/santiagosayshey/seasonpackarr), it
doesn't rely on hardlinks, identical release groups, or seeding — it matches
purely by episode number, so it works with mixed release groups and
alongside [Decluttarr](https://github.com/ManiMatter/decluttarr) removing
torrents after import.

## How it fits in the pipeline

```
autobrr (webhook)    ─┐
                       ├─▶ episodeskiparr ─▶ qBittorrent (downloads only what's missing)
packgrabbarr (HTTP)  ─┘          │
                                  ▼
                            Sonarr import ─▶ Decluttarr (cleans up the torrent)
```

episodeskiparr has no direct integration with Decluttarr — they're just
independent services designed to coexist in the same Sonarr/qBittorrent
pipeline. [packgrabbarr](https://github.com/scharyu/packgrabbarr) is a
sibling project that force-grabs season packs Sonarr would otherwise reject,
then calls episodeskiparr to trim them down to what's needed.

## Features

- Parses both RuTracker-style and scene-style release names
- Looks up existing episodes via the Sonarr API
- Finds the torrent in qBittorrent by Sonarr's queue infohash, falling back
  to fuzzy name matching
- Sets per-file priority to skip episodes you already have
- Two modes: legacy "have"-based skipping (autobrr) and explicit keep-lists
  (packgrabbarr), the latter allowing a single episode to be upgraded out of
  a pack
- Optional token-authenticated API

## Requirements

- Docker / Docker Compose
- A running Sonarr instance with an API key
- qBittorrent with the WebUI enabled

## Configuration

All configuration is via environment variables:

| Variable | Default | Notes |
|---|---|---|
| `QB_HOST` | `qbittorrent` | qBittorrent WebUI hostname |
| `QB_PORT` | `8080` | qBittorrent WebUI port |
| `QB_USER` | *(none)* | qBittorrent WebUI username — see Security |
| `QB_PASS` | *(none)* | qBittorrent WebUI password — see Security |
| `SONARR_URL` | `http://sonarr:8989` | Sonarr base URL |
| `SONARR_API_KEY` | *(required)* | Sonarr → Settings → General |
| `QB_CATEGORY` | `tv-sonarr` | Category Sonarr assigns to TV downloads |
| `POLL_TIMEOUT` | `60` | Seconds to wait for the torrent to appear in qB |
| `POLL_INTERVAL` | `2` | Seconds between polls |
| `API_TOKEN` | *(empty = disabled)* | Optional shared secret for `/api/skip` |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `PORT` | `8088` | HTTP listen port |
| `FAKE_HAVE_EPISODES` | *(empty)* | Test-only; leave unset in production |

## Usage

```yaml
services:
  episodeskiparr:
    build: .
    container_name: episodeskiparr
    restart: unless-stopped
    environment:
      QB_HOST: qbittorrent
      QB_USER: ${QB_USER}
      QB_PASS: ${QB_PASS}
      SONARR_URL: http://sonarr:8989
      SONARR_API_KEY: ${SONARR_API_KEY}
      QB_CATEGORY: tv-sonarr
    ports:
      - "8088:8088"
```

Copy `.env.example` to `.env`, fill in your real values, and Docker Compose
will pick it up automatically — `.env` is git-ignored, so your credentials
never end up in version control.

## API

- `POST /api/skip` — body `{"name": "<release name>", "season": <int>, "episodes": [<int>, ...]}` (`season`/`episodes` are optional; when omitted, episodeskiparr falls back to asking Sonarr what you already have). Returns `{"ok": bool, "message": str}` with HTTP 250 (autobrr's "approved" convention).
- `GET /health` — liveness check.

## Security

`API_TOKEN` is unset (disabled) by default — episodeskiparr is meant to sit
on an internal Docker network reachable only by autobrr/packgrabbarr. If you
expose the port more broadly, set `API_TOKEN` and require it via
`?token=...` or an `X-API-Token` header.

If `QB_USER`/`QB_PASS` are left unset, episodeskiparr skips qBittorrent login
entirely and relies on qBittorrent's own "bypass authentication for clients
in whitelisted IP subnets" setting. Set both or neither — a mismatched pair
is rejected at startup.

## License

[GPL-3.0](LICENSE)

## Related projects

- [packgrabbarr](https://github.com/scharyu/packgrabbarr) — force-grabs
  Sonarr-rejected season packs and hands them to episodeskiparr
- [Sonarr](https://github.com/Sonarr/Sonarr)
- [qBittorrent](https://github.com/qbittorrent/qBittorrent)
- [Decluttarr](https://github.com/ManiMatter/decluttarr)
- [autobrr](https://github.com/autobrr/autobrr)
