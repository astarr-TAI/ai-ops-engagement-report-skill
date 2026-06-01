"""
Zoom AI Companion transcript puller — Cloudflare-resilient via curl_cffi.

Replays the browser flow against Zoom's internal endpoints:
  1. Mint a Bearer JWT via the NAK endpoint using captured browser cookies.
  2. List meetings (us01docs.zoom.us/api/meeting/list_my_meetings) — returns
     all meetings you attended/were invited to (not just AIC). The endpoint
     server-side caps the time window at ~30 days; for wider windows the
     lister auto-chunks into 30d slices and stitches results.
  3. Pull a transcript (us01docs.zoom.us/api/bridge/meeting/transcripts/v2)
     by either meetingId (from list) or fileId (from a docs.zoom.us/doc/<id> URL).

Why curl_cffi: it impersonates Chrome's TLS/JA3 fingerprint, so Cloudflare's
cf_clearance challenge is far less likely to fire compared to plain requests/curl.
Cookies still rotate (cf_clearance ~hours, __cf_bm ~30min); when they go stale,
re-capture from DevTools.

Usage (CLI):
    python zoom_transcript.py mint --cookies cookies.txt
    python zoom_transcript.py list --cookies cookies.txt [--limit 20] [--since 7d]
    python zoom_transcript.py transcript --cookies cookies.txt --meeting-id 'SzMKmu...=='
    python zoom_transcript.py transcript --cookies cookies.txt --file-id ABC123
    python zoom_transcript.py find --cookies cookies.txt --match "Cognitiv" [--pull]
    python zoom_transcript.py bulk --cookies cookies.txt --since 7d --out /path/to/dir

cookies.txt format: paste the value of `-b '...'` from a DevTools "Copy as cURL",
or paste the full curl command — the loader will extract the cookie header.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Vendored deps live alongside this script
sys.path.insert(0, str(Path(__file__).parent / "vendor"))

from curl_cffi import requests  # noqa: E402

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)
IMPERSONATE = "chrome131"
DEFAULT_OUT_DIR = "~/Desktop/Transcripts 2.0"

# list_my_meetings caps the window server-side at ~30 days. We chunk in 30d slices.
WINDOW_CHUNK_MS = 30 * 86400 * 1000


def load_cookies(path: str) -> str:
    raw = Path(path).read_text().strip()
    if "-b '" in raw:
        start = raw.index("-b '") + 4
        end = raw.index("'", start)
        raw = raw[start:end]
    elif '-b "' in raw:
        start = raw.index('-b "') + 4
        end = raw.index('"', start)
        raw = raw[start:end]
    return raw


def _parse_cookie_header(raw: str) -> dict:
    out = {}
    for piece in raw.split(";"):
        piece = piece.strip()
        if not piece or "=" not in piece:
            continue
        k, v = piece.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def mint_token(cookies: str) -> str:
    url = (
        "https://docs.zoom.us/nws/common/2.0/nak"
        "?pms=Docs%2CUser%3ABase%2CAICW&src=aicw"
    )
    r = requests.get(
        url,
        headers={
            "accept": "*/*",
            "referer": "https://docs.zoom.us/",
            "x-requested-with": "XMLHttpRequest",
            "user-agent": UA,
        },
        cookies=_parse_cookie_header(cookies),
        impersonate=IMPERSONATE,
    )
    if r.status_code != 200:
        raise RuntimeError(
            f"NAK returned HTTP {r.status_code}. "
            f"First 200 chars: {r.text[:200]!r}. "
            "Cookies likely stale — re-capture from DevTools."
        )
    body = r.text.strip()
    if not body.startswith("eyJ"):
        raise RuntimeError(
            f"NAK did not return a JWT (got {body[:120]!r}). "
            "Likely Cloudflare challenge — cookies stale."
        )
    return body


def _normalize_meeting(m: dict) -> dict:
    """Map list_my_meetings shape -> the shape the rest of this module expects.
    Keeps the original keys too so callers can still see new fields."""
    # Find the host participant for owner_id / owner_name
    owner_id, owner_name = None, None
    for p in m.get("participants") or []:
        if p.get("role") == "host":
            owner_id = p.get("userId")
            owner_name = p.get("name")
            break

    start_ms = m.get("startTime")
    if isinstance(start_ms, str):
        try:
            start_ms = int(start_ms)
        except ValueError:
            start_ms = None
    end_ms = m.get("endTime")
    if isinstance(end_ms, str):
        try:
            end_ms = int(end_ms)
        except ValueError:
            end_ms = None

    has_tx = bool(m.get("hasTranscript")) or bool(m.get("hasRecordingTranscript"))
    has_summary = bool(m.get("hasSummary"))

    attendee_list = []
    for p in m.get("participants") or []:
        attendee_list.append({
            "user_name": p.get("name"),
            "display_name": p.get("name"),
            "email": p.get("email"),
            "role": p.get("role"),
            "is_external": p.get("isExternal"),
        })

    normalized = {
        # Legacy field names downstream code expects
        "id": m.get("meetingId"),
        "name": m.get("topic"),
        "meeting_topic": m.get("topic"),
        "date": start_ms,
        "meeting_start_time": start_ms,
        "meeting_end_time": end_ms,
        "meeting_number": m.get("meetingNumber"),
        "owner_id": owner_id,
        "owner_name": owner_name,
        "participant_size": m.get("participantSize"),
        "attendee_list": attendee_list,
        "has_transcript": has_tx,
        "has_summary": has_summary,
        # main_file_id is not present in list_my_meetings; resolve via get_meeting_assets if needed
        "main_file_id": None,
        # meetingAccountId is required by get_meeting_assets — keep handy
        "meeting_account_id": m.get("meetingAccountId"),
    }
    # Preserve raw fields for any caller that wants them
    for k, v in m.items():
        normalized.setdefault(k, v)
    return normalized


def _call_list_my_meetings(cookies: str, token: str, *, start_ms: int, end_ms: int,
                           page_num: int, page_size: int) -> dict:
    url = "https://us01docs.zoom.us/api/meeting/list_my_meetings"
    payload = {
        "startTime": start_ms,
        "endTime": end_ms,
        "pageNum": page_num,
        "pageSize": page_size,
        "meetingFilter": 0,
        "filters": {},
        "query": "",
    }
    r = requests.post(
        url,
        headers={
            "accept": "application/json, text/plain, */*",
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
            "origin": "https://docs.zoom.us",
            "referer": "https://docs.zoom.us/",
            "x-requested-with": "XMLHttpRequest",
            "x-zm-cluster-id": "aw1",
            "x-zm-docs-container": "docs/browser",
            "user-agent": UA,
        },
        cookies=_parse_cookie_header(cookies),
        json=payload,
        impersonate=IMPERSONATE,
    )
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and "code" in data and data.get("code") and "meetings" not in data:
        raise RuntimeError(
            f"list_my_meetings returned error code={data.get('code')} "
            f"reason={data.get('reason')!r} message={data.get('message')!r}. "
            f"(Window may exceed server cap — chunking is 30d.)"
        )
    return data


def _list_meetings_single_window(cookies: str, token: str, *,
                                 start_ms: int, end_ms: int,
                                 page_size: int = 50,
                                 verbose: bool = False,
                                 max_pages: int = 200,
                                 only_with_content: bool = False) -> list:
    """Paginate list_my_meetings inside a window <=30d, return normalized meetings.
    `only_with_content` defaults to False — the hasTranscript/hasSummary flags are
    unreliable for very recent meetings, so we always return everything and let
    the caller decide what to do per meeting."""
    out, page_num = [], 0
    while page_num < max_pages:
        data = _call_list_my_meetings(
            cookies, token,
            start_ms=start_ms, end_ms=end_ms,
            page_num=page_num, page_size=page_size,
        )
        page = data.get("meetings", []) or []
        for m in page:
            if only_with_content:
                if not (m.get("hasTranscript") or m.get("hasSummary") or m.get("hasRecordingTranscript")):
                    continue
            out.append(_normalize_meeting(m))
        if verbose:
            print(f"    page {page_num}: +{len(page)} raw, kept total {len(out)}"
                  f" (hasNext={data.get('hasNext')}, totalCount={data.get('totalCount')})",
                  flush=True)
        if not data.get("hasNext") or not page:
            break
        page_num += 1
    return out


def list_meetings(cookies: str, token: str, limit: int = 20,
                  start_time_ms: int | None = None,
                  end_time_ms: int | None = None,
                  *, only_with_content: bool = False) -> list:
    """Single-window list. If no window given, uses last 30d.
    Returns at most `limit` meetings (post content-filter)."""
    import time as _t
    if end_time_ms is None:
        end_time_ms = int(_t.time() * 1000)
    if start_time_ms is None:
        start_time_ms = end_time_ms - WINDOW_CHUNK_MS
    # Caller expects a single page, but the server may need multiple — collect up to `limit`.
    all_m = _list_meetings_single_window(
        cookies, token,
        start_ms=start_time_ms, end_ms=end_time_ms,
        page_size=min(50, max(limit, 10)),
        only_with_content=only_with_content,
    )
    return all_m[:limit]


def list_meetings_window(cookies: str, token: str,
                         start_time_ms: int, end_time_ms: int,
                         page_size: int = 50, *,
                         verbose: bool = False,
                         max_pages: int = 200,
                         only_with_content: bool = False) -> list:
    """Auto-chunk the [start, end] window into <=30d slices and stitch results.
    Each slice is paginated until hasNext is false."""
    if end_time_ms <= start_time_ms:
        return []
    out = []
    seen_ids: set[str] = set()
    chunk_end = end_time_ms
    chunk_idx = 0
    while chunk_end > start_time_ms:
        chunk_start = max(start_time_ms, chunk_end - WINDOW_CHUNK_MS)
        chunk_idx += 1
        if verbose:
            from datetime import datetime as _dt2, timezone as _tz2
            cs_iso = _dt2.fromtimestamp(chunk_start / 1000, _tz2.utc).strftime("%Y-%m-%d")
            ce_iso = _dt2.fromtimestamp(chunk_end / 1000, _tz2.utc).strftime("%Y-%m-%d")
            print(f"  chunk {chunk_idx}: {cs_iso} -> {ce_iso}", flush=True)
        page = _list_meetings_single_window(
            cookies, token,
            start_ms=chunk_start, end_ms=chunk_end,
            page_size=page_size,
            verbose=verbose,
            max_pages=max_pages,
            only_with_content=only_with_content,
        )
        for m in page:
            mid = m.get("id")
            if mid and mid in seen_ids:
                continue
            if mid:
                seen_ids.add(mid)
            out.append(m)
        # Step the window backward; subtract 1ms to avoid double-counting boundary
        chunk_end = chunk_start - 1
    # Sort newest-first by start time
    out.sort(key=lambda m: m.get("date") or 0, reverse=True)
    return out


def get_meeting_assets(cookies: str, token: str, *,
                       meeting_id: str,
                       meeting_account_id: str | None = None) -> dict:
    """Look up the doc fileId(s) for a meeting via /api/file/get_meeting_assets.
    Returns the raw response. The doc fileId is at
    `createFromMeeting[0].doc.id` when present.

    `meeting_account_id` is required by the API. If not provided, the call
    is sent without it and may 4xx — pass meeting["meeting_account_id"] from
    a normalized list_my_meetings record."""
    url = "https://us01docs.zoom.us/api/file/get_meeting_assets"
    payload: dict = {"meetingId": meeting_id}
    if meeting_account_id:
        payload["meetingAccountId"] = meeting_account_id
    r = requests.post(
        url,
        headers={
            "accept": "application/json, text/plain, */*",
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
            "origin": "https://docs.zoom.us",
            "referer": "https://docs.zoom.us/",
            "x-requested-with": "XMLHttpRequest",
            "x-zm-cluster-id": "aw1",
            "x-zm-docs-container": "docs/browser",
            "user-agent": UA,
        },
        cookies=_parse_cookie_header(cookies),
        json=payload,
        impersonate=IMPERSONATE,
    )
    r.raise_for_status()
    return r.json()


def resolve_doc_file_id(assets: dict) -> str | None:
    """Pick the primary doc fileId from a get_meeting_assets response.
    Prefers `summaryDoc.id` if present, then the first item in
    `createFromMeeting[].doc.id`. Returns None if no doc is associated."""
    if not isinstance(assets, dict):
        return None
    sd = assets.get("summaryDoc")
    if isinstance(sd, dict):
        fid = sd.get("id") or (sd.get("doc") or {}).get("id")
        if fid:
            return fid
    for entry in assets.get("createFromMeeting") or []:
        doc = (entry or {}).get("doc") or {}
        fid = doc.get("id")
        if fid:
            return fid
    return None


def get_transcript(cookies: str, token: str, *,
                   meeting_id: str | None = None,
                   file_id: str | None = None) -> dict:
    if not meeting_id and not file_id:
        raise ValueError("Provide meeting_id or file_id")
    from urllib.parse import quote
    mid = quote(meeting_id, safe="") if meeting_id else ""
    fid = quote(file_id, safe="") if file_id else ""
    url = (
        "https://us01docs.zoom.us/api/bridge/meeting/transcripts/v2"
        f"?meetingId={mid}&fileId={fid}"
    )
    r = requests.get(
        url,
        headers={
            "accept": "application/json",
            "authorization": f"Bearer {token}",
            "origin": "https://docs.zoom.us",
            "referer": "https://docs.zoom.us/",
            "x-zm-cluster-id": "aw1",
            "x-zm-docs-container": "docs/browser",
            "user-agent": UA,
        },
        cookies=_parse_cookie_header(cookies),
        impersonate=IMPERSONATE,
    )
    r.raise_for_status()
    return r.json()


def format_dialogue(transcript: dict) -> str:
    """Merge consecutive utterances by the same speaker, label by real name when known."""
    items = transcript.get("items", [])
    if not items:
        return "(no transcript items)"

    # Map userId -> display name from the transcript's speakers list
    name_by_uid = {}
    for sp in transcript.get("speakers", []) or []:
        uid = sp.get("userId")
        nm = sp.get("speakerName") or sp.get("username") or ""
        if uid and nm:
            name_by_uid[uid] = nm

    # Fallback labels for unknown speakers
    labels, fallback_n = {}, 0
    for it in items:
        u = it["userId"]
        if u in labels:
            continue
        if u in name_by_uid:
            labels[u] = name_by_uid[u]
        else:
            fallback_n += 1
            labels[u] = f"S{fallback_n}"

    out, cur_user, cur_text, cur_start = [], None, [], None
    for it in items:
        u = it["userId"]
        if u != cur_user:
            if cur_user is not None:
                out.append((cur_start, labels[cur_user], " ".join(cur_text)))
            cur_user, cur_text, cur_start = u, [it["text"]], it["startTime"]
        else:
            cur_text.append(it["text"])
    if cur_user is not None:
        out.append((cur_start, labels[cur_user], " ".join(cur_text)))

    speaker_summary = sorted(set(labels.values()))
    lines = [f"Speakers: {speaker_summary} ({len(speaker_summary)} total)", ""]
    for ts, spk, text in out:
        lines.append(f"[{str(ts)[:8]}] {spk}: {text}")
    return "\n".join(lines)



# ---------------------------------------------------------------------------
# Bulk download helpers
# ---------------------------------------------------------------------------

import re as _re
import time as _time
from datetime import datetime as _dt, timezone as _tz


def parse_since(spec: str) -> int:
    """'7d' / '24h' / '30d' / '2w' -> epoch ms representing now-spec.
    Also accepts an explicit YYYY-MM-DD date."""
    spec = spec.strip().lower()
    m = _re.fullmatch(r"(\d+)\s*([dhwm])", spec)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        secs = {"h": 3600, "d": 86400, "w": 604800, "m": 2592000}[unit] * n
        return int(_time.time() * 1000) - secs * 1000
    # date form
    try:
        d = _dt.strptime(spec, "%Y-%m-%d").replace(tzinfo=_tz.utc)
        return int(d.timestamp() * 1000)
    except ValueError:
        raise ValueError(f"Bad --since value: {spec!r}. Use '7d', '24h', '2w', or YYYY-MM-DD.")


def slugify(name: str, max_len: int = 60) -> str:
    s = _re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower()
    return (s or "meeting")[:max_len].rstrip("-")


def meeting_filename_stem(meeting: dict) -> str:
    date_iso = _dt.fromtimestamp(meeting["date"] / 1000, _tz.utc).strftime("%Y-%m-%d")
    # strip the trailing "YYYY-MM-DD HH:MM(GMT-...)" Zoom stamps from titles
    name = meeting.get("name") or meeting.get("meeting_topic") or "meeting"
    name = _re.sub(r"\s*\d{4}-\d{2}-\d{2}\s*\d{2}:\d{2}\(GMT[^)]+\)\s*$", "", name)
    return f"{date_iso}-{slugify(name)}"


def render_markdown(meeting: dict, transcript: dict) -> str:
    """YAML frontmatter + dialogue. Frontmatter is safe to grep / index."""
    import json as _json
    date_iso = _dt.fromtimestamp(meeting["date"] / 1000, _tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    duration_min = None
    if meeting.get("meeting_start_time") and meeting.get("meeting_end_time"):
        duration_min = round(
            (meeting["meeting_end_time"] - meeting["meeting_start_time"]) / 60000
        )
    speakers = []
    for sp in transcript.get("speakers", []) or []:
        nm = sp.get("speakerName") or sp.get("username")
        if nm:
            speakers.append(nm)
    attendees = []
    for a in meeting.get("attendee_list") or []:
        nm = a.get("user_name") or a.get("display_name")
        if nm:
            attendees.append(nm)

    fm = {
        "title": meeting.get("name") or meeting.get("meeting_topic"),
        "date": date_iso,
        "meeting_id": meeting.get("id"),
        "meeting_number": meeting.get("meeting_number"),
        "main_file_id": meeting.get("main_file_id"),
        "host": meeting.get("owner_name"),
        "duration_minutes": duration_min,
        "participant_size": meeting.get("participant_size"),
        "speakers": speakers,
        "attendees": attendees,
        "source": "zoom-aic",
    }
    fm_yaml_lines = ["---"]
    for k, v in fm.items():
        if v in (None, "", []):
            continue
        if isinstance(v, list):
            fm_yaml_lines.append(f"{k}:")
            for item in v:
                fm_yaml_lines.append(f"  - {_json.dumps(item, ensure_ascii=False)}")
        else:
            fm_yaml_lines.append(f"{k}: {_json.dumps(v, ensure_ascii=False)}")
    fm_yaml_lines.append("---")
    return "\n".join(fm_yaml_lines) + "\n\n" + format_dialogue(transcript) + "\n"



def _uid_from_token(token: str) -> str | None:
    """Decode 'uid' claim from a JWT payload. No signature check — claim read only."""
    import base64, json as _json
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = _json.loads(base64.urlsafe_b64decode(payload))
        return claims.get("uid")
    except Exception:
        return None


def bulk_pull(cookies: str, token: str, start_ms: int, end_ms: int,
              out_dir: Path, *, skip_existing: bool = True,
              dry_run: bool = False, sleep_s: float = 0.6,
              mine_only_uid: str | None = None) -> dict:
    """Pull a transcript file for every meeting in [start_ms, end_ms].
    For each meeting we try the transcripts bridge by `meetingId` first; if that
    returns no items we resolve the doc fileId via `get_meeting_assets` and retry
    by `fileId`. Either way we always write the .md and .json files — meetings
    with no transcript get a frontmatter-only file with an empty body.
    If `mine_only_uid` is set, skip meetings where owner_id != that uid.
    Auto-refreshes the Bearer token on 401 mid-batch."""
    import json as _json
    from curl_cffi.requests.exceptions import HTTPError as _HTTPError
    out_dir = Path(out_dir).expanduser()
    raw_dir = out_dir / ".raw"
    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        raw_dir.mkdir(parents=True, exist_ok=True)

    print(f"Listing meetings in window (auto-chunked into 30d slices)...", flush=True)
    meetings = list_meetings_window(cookies, token, start_ms, end_ms, verbose=True)
    print(f"  found {len(meetings)} meetings in window", flush=True)

    if mine_only_uid:
        before = len(meetings)
        meetings = [m for m in meetings if m.get("owner_id") == mine_only_uid]
        print(f"  --mine-only: {len(meetings)} hosted by you (filtered out {before - len(meetings)})",
              flush=True)

    summary = {
        "window": {"start_ms": start_ms, "end_ms": end_ms,
                   "start_iso": _dt.fromtimestamp(start_ms/1000, _tz.utc).isoformat(),
                   "end_iso": _dt.fromtimestamp(end_ms/1000, _tz.utc).isoformat()},
        "total_meetings": len(meetings),
        "pulled_with_transcript": 0,
        "pulled_empty": 0,
        "skipped_existing": 0,
        "errors": [],
        "files": [],
        "token_refreshes": 0,
    }

    # Mutable token holder so refresh affects subsequent calls
    bearer = {"tok": token}

    def _refresh_on_401(call):
        """Run `call(token)`, refreshing the bearer once on 401."""
        try:
            return call(bearer["tok"])
        except _HTTPError as e:
            if "401" not in str(e):
                raise
            print(f"  [token expired -> minting fresh Bearer]", flush=True)
            bearer["tok"] = mint_token(cookies)
            summary["token_refreshes"] += 1
            return call(bearer["tok"])

    def _pull_dual(meeting: dict) -> tuple[dict, str]:
        """Return (transcript_dict, source_label).
        Tries meetingId first, then resolves fileId via get_meeting_assets."""
        mid = meeting["id"]
        tx = _refresh_on_401(lambda tok: get_transcript(cookies, tok, meeting_id=mid))
        if tx.get("items"):
            return tx, "meetingId"
        # Empty — try fileId fallback
        try:
            assets = _refresh_on_401(lambda tok: get_meeting_assets(
                cookies, tok, meeting_id=mid,
                meeting_account_id=meeting.get("meeting_account_id"),
            ))
        except Exception as e:
            return tx, f"meetingId-empty (assets-error: {e})"
        fid = resolve_doc_file_id(assets)
        if not fid:
            return tx, "meetingId-empty (no-doc-asset)"
        try:
            tx2 = _refresh_on_401(lambda tok: get_transcript(cookies, tok, file_id=fid))
        except Exception as e:
            return tx, f"meetingId-empty (fileId-error: {e})"
        return tx2, f"fileId={fid}"

    for i, m in enumerate(meetings, 1):
        title = m.get("name") or m.get("meeting_topic") or "(no title)"
        stem = meeting_filename_stem(m)
        md_path = out_dir / f"{stem}.md"
        json_path = raw_dir / f"{stem}.json"
        if skip_existing and md_path.exists() and json_path.exists():
            summary["skipped_existing"] += 1
            print(f"[{i}/{len(meetings)}] EXISTS: {md_path.name}")
            continue
        if dry_run:
            print(f"[{i}/{len(meetings)}] WOULD PULL: {md_path.name}")
            continue
        try:
            tx, source = _pull_dual(m)
        except Exception as e:
            summary["errors"].append({"title": title, "meeting_id": m.get("id"), "error": str(e)})
            print(f"[{i}/{len(meetings)}] ERROR pulling {title}: {e}")
            _time.sleep(sleep_s)
            continue
        json_path.write_text(_json.dumps({"meeting": m, "transcript": tx, "source": source}, indent=2))
        md_path.write_text(render_markdown(m, tx))
        summary["files"].append(str(md_path))
        n_items = len(tx.get("items", []))
        if n_items:
            summary["pulled_with_transcript"] += 1
            print(f"[{i}/{len(meetings)}] OK ({n_items} utts via {source}): {md_path.name}")
        else:
            summary["pulled_empty"] += 1
            print(f"[{i}/{len(meetings)}] EMPTY ({source}): {md_path.name}")
        _time.sleep(sleep_s)

    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--cookies", required=True,
                        help="Path to file with cookie header (or full curl)")

    sub.add_parser("mint", parents=[common])

    p_list = sub.add_parser("list", parents=[common])
    p_list.add_argument("--limit", type=int, default=20)
    p_list.add_argument("--since", help="Optional window: 7d, 24h, 2w, or YYYY-MM-DD")
    p_list.add_argument("--only-with-content", action="store_true",
                        help="Filter to meetings where the list flags hasTranscript/hasSummary "
                             "are true. NOTE: those flags are unreliable for very recent meetings, "
                             "so the default is to return everything.")

    p_tx = sub.add_parser("transcript", parents=[common])
    g = p_tx.add_mutually_exclusive_group(required=True)
    g.add_argument("--meeting-id", help="Meeting ID from `list` (base64-ish, often ends in ==)")
    g.add_argument("--file-id", help="Doc fileId from a docs.zoom.us/doc/<id> URL")
    p_tx.add_argument("--account-id",
                      help="meetingAccountId for the assets fallback (used if "
                           "the bridge returns no items via --meeting-id)")
    p_tx.add_argument("--raw", action="store_true",
                      help="Print raw JSON instead of dialogue")

    p_find = sub.add_parser("find", parents=[common])
    p_find.add_argument("--match", required=True,
                        help="Substring to match against meeting topic")
    p_find.add_argument("--limit", type=int, default=50)
    p_find.add_argument("--pull", action="store_true",
                        help="Also pull transcript for first match")

    p_bulk = sub.add_parser("bulk", parents=[common],
                            help="Pull every transcript in a time window")
    p_bulk.add_argument("--since", required=True,
                        help="Window start: 7d, 24h, 2w, or YYYY-MM-DD")
    p_bulk.add_argument("--out", default=DEFAULT_OUT_DIR,
                        help=f"Output directory (default: {DEFAULT_OUT_DIR})")
    p_bulk.add_argument("--dry-run", action="store_true",
                        help="List what would be pulled, don't download")
    p_bulk.add_argument("--no-skip-existing", action="store_true",
                        help="Re-pull even if files already exist")
    p_bulk.add_argument("--sleep", type=float, default=0.6,
                        help="Seconds between transcript calls (default 0.6)")
    g_owner = p_bulk.add_mutually_exclusive_group()
    g_owner.add_argument("--mine-only", action="store_true",
                         help="Only pull meetings hosted by you (owner_id from token).")
    g_owner.add_argument("--owner-id",
                         help="Only pull meetings hosted by this Zoom user id (advanced).")

    args = ap.parse_args()
    cookies = load_cookies(args.cookies)

    if args.cmd == "mint":
        print(mint_token(cookies))
        return 0

    token = mint_token(cookies)

    if args.cmd == "list":
        start_ms = parse_since(args.since) if args.since else None
        end_ms = int(_time.time() * 1000) if args.since else None
        only_with_content = bool(getattr(args, "only_with_content", False))
        if args.since and (end_ms - start_ms) > WINDOW_CHUNK_MS:
            meetings = list_meetings_window(
                cookies, token, start_ms, end_ms,
                verbose=False, only_with_content=only_with_content,
            )[:args.limit]
        else:
            meetings = list_meetings(
                cookies, token, args.limit, start_ms, end_ms,
                only_with_content=only_with_content,
            )
        for m in meetings:
            print(f"{m['date']}\t{m['id']}\t{m['name']}")
        return 0

    if args.cmd == "transcript":
        tx = get_transcript(cookies, token,
                            meeting_id=args.meeting_id,
                            file_id=args.file_id)
        # If meetingId was used and produced no items, try fallback via
        # get_meeting_assets -> doc fileId -> bridge by fileId.
        if args.meeting_id and not tx.get("items"):
            try:
                assets = get_meeting_assets(cookies, token,
                                            meeting_id=args.meeting_id,
                                            meeting_account_id=args.account_id)
                fid = resolve_doc_file_id(assets)
                if fid:
                    print(f"# meetingId returned 0 items — retrying via fileId={fid}",
                          file=sys.stderr)
                    tx = get_transcript(cookies, token, file_id=fid)
            except Exception as e:
                print(f"# fileId fallback failed: {e}", file=sys.stderr)
        if args.raw:
            print(json.dumps(tx, indent=2))
        else:
            print(format_dialogue(tx))
        return 0

    if args.cmd == "find":
        meetings = list_meetings(cookies, token, args.limit)
        needle = args.match.lower()
        matches = [m for m in meetings if needle in (m["name"] or "").lower()]
        if not matches:
            print(f"No meetings match {args.match!r}")
            return 1
        for m in matches:
            print(f"{m['date']}\t{m['id']}\t{m['name']}")
        if args.pull:
            first = matches[0]
            print(f"\n--- Transcript for: {first['name']} ---\n")
            tx = get_transcript(cookies, token, meeting_id=first["id"])
            print(format_dialogue(tx))
        return 0

    if args.cmd == "bulk":
        start_ms = parse_since(args.since)
        end_ms = int(_time.time() * 1000)
        mine_uid = None
        if args.owner_id:
            mine_uid = args.owner_id
        elif args.mine_only:
            mine_uid = _uid_from_token(token)
            if not mine_uid:
                print("ERROR: could not extract uid from token; use --owner-id explicitly", flush=True)
                return 1
            print(f"--mine-only: filtering to owner_id={mine_uid}", flush=True)
        summary = bulk_pull(
            cookies, token, start_ms, end_ms,
            Path(args.out),
            skip_existing=not args.no_skip_existing,
            dry_run=args.dry_run,
            sleep_s=args.sleep,
            mine_only_uid=mine_uid,
        )
        print()
        print(f"Window: {summary['window']['start_iso']} -> {summary['window']['end_iso']}")
        print(f"Total meetings (after filters): {summary['total_meetings']}")
        print(f"  pulled with transcript: {summary['pulled_with_transcript']}")
        print(f"  pulled empty:           {summary['pulled_empty']}")
        print(f"  skipped (existing):     {summary['skipped_existing']}")
        print(f"  errors:                 {len(summary['errors'])}")
        print(f"  token refreshes:        {summary.get('token_refreshes', 0)}")
        if summary['errors']:
            for e in summary['errors']:
                print(f"    - {e['title']}: {e['error']}")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
