---
name: auto-pull-zoom-transcript
description: Use when the user wants to pull, fetch, retrieve, extract, or download a transcript from a Zoom meeting doc URL (e.g., https://docs.zoom.us/doc/{fileId}). Triggers on "pull zoom transcript", "get zoom transcript", "fetch transcript from zoom", "extract zoom meeting transcript", "zoom doc transcript", or any docs.zoom.us/doc/ URL. Handles token minting via Zoom's NAK endpoint using captured browser cookies, calls the transcripts bridge API, and formats the result as readable speaker-labeled dialogue. Does NOT use the supported Zoom REST API — this is a browser-session replay flow that requires fresh cookies.
---

# Auto-Pull Zoom Transcript

Fetch and format the AI Companion transcript for any Zoom meeting doc the user has access to in their browser session. Works by replaying the browser's auth flow — mint a fresh Bearer JWT from Zoom's internal NAK endpoint using the user's cookies, then call the transcripts bridge API.

This is **not** the supported Zoom REST API. It uses the same internal endpoints `docs.zoom.us` calls from the browser. Tokens last ~1 hour, cookies last hours-to-days.

## When to use

Three modes:
- **Single transcript** — user gives a `https://docs.zoom.us/doc/{fileId}` URL, or names a meeting (e.g. *"the Cognitiv one from today"*).
- **Bulk window** — *"pull the last 7 days of transcripts"*, *"download every meeting since 2026-05-01"*. Uses the `bulk` subcommand of `zoom_transcript.py`. Windows wider than 30 days are auto-chunked into 30d slices (server-side cap). Always writes one file per meeting in the window — meetings without a transcript get a frontmatter-only file.
- **List only** — user wants to see what meetings are available without pulling. Use `list --since 7d`.

## Inputs needed

1. **Zoom doc URL or fileId** — extract `{fileId}` from `https://docs.zoom.us/doc/{fileId}`.
2. **Cookie jar** — captured from a browser request to `docs.zoom.us` while the user is logged in. The user typically pastes a curl from DevTools.

If no cookies are available in this session, ask the user to:
- Open `https://docs.zoom.us` in Chrome while logged in
- DevTools → Network → right-click any `docs.zoom.us` request → **Copy → Copy as cURL**
- Paste the curl in chat

Extract the `-b '...'` cookie string from the pasted curl.

## Steps

### 1. Mint a fresh Bearer token via NAK

```bash
curl -sS 'https://docs.zoom.us/nws/common/2.0/nak?pms=Docs%2CUser%3ABase%2CAICW&src=aicw' \
  -H 'accept: */*' \
  -H 'referer: https://docs.zoom.us/' \
  -H 'x-requested-with: XMLHttpRequest' \
  -H 'user-agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36' \
  -b '{COOKIE_STRING}'
```

The response body **is** the JWT — a single string starting with `eyJ`. Save it as `$TOKEN`.

If the response is HTML, a Cloudflare challenge, or `401`/`403`, the cookies are stale. Tell the user to re-grab a fresh curl from DevTools.

### 2. List meetings (find the meeting ID)

```bash
curl -sS 'https://us01docs.zoom.us/api/meeting/list_my_meetings' \
  -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -H 'origin: https://docs.zoom.us' \
  -H 'referer: https://docs.zoom.us/' \
  -H 'x-zm-cluster-id: aw1' \
  -H 'x-zm-docs-container: docs/browser' \
  -b '{COOKIE_STRING}' \
  --data-raw '{"startTime":{START_MS},"endTime":{END_MS},"pageNum":0,"pageSize":50,"meetingFilter":0,"filters":{},"query":""}'
```

Returns `{"meetings":[...], "hasNext": bool, "totalCount": int}`. Each meeting has:
- `meetingId` — the base64-ish ID (e.g. `qYhsD/onTiGGmCJ2GAWgvA==`) — pass to step 3
- `topic` — meeting title
- `startTime` / `endTime` — epoch ms (as strings)
- `participants[]` — names, emails, host role, internal/external flag
- `hasTranscript`, `hasSummary`, `hasRecordingTranscript` — only meetings where one of these is true have content the bridge API can return
- `meetingNumber`, `participantSize`, `isRecurring`, etc.

**Window cap:** the API enforces a server-side cap of ~30 days. A 90d or 365d window returns `{"code": 500, "reason": "REASON_UNSPECIFIED"}`. For wider windows, slice into ≤30d chunks and stitch results client-side (the script does this automatically).

**Filter rule:** before pulling, only keep meetings where `hasTranscript || hasSummary || hasRecordingTranscript` — anything else returns empty content from the bridge.

### 2.5. Resolve the doc fileId via `get_meeting_assets`

The transcripts bridge can be hit by `meetingId` *or* by `fileId`. For very recent meetings, the bridge often returns empty when called by `meetingId` even though a transcript exists — Zoom's indexes are eventually-consistent. The reliable workaround is to look up the meeting's doc fileId and call the bridge with that instead.

```bash
curl -sS 'https://us01docs.zoom.us/api/file/get_meeting_assets' \
  -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -H 'origin: https://docs.zoom.us' \
  -H 'referer: https://docs.zoom.us/' \
  -H 'x-zm-cluster-id: aw1' \
  -H 'x-zm-docs-container: docs/browser' \
  -b '{COOKIE_STRING}' \
  --data-raw '{"meetingId":"{MEETING_ID}","meetingAccountId":"{ACCOUNT_ID}"}'
```

Both `meetingId` and `meetingAccountId` come straight from the `list_my_meetings` response.

The doc fileId is at `createFromMeeting[0].doc.id` in the response (or `summaryDoc.id` for older summaries). If `createFromMeeting` is empty, no doc was created — the meeting will have no transcript yet.

The fileId returned here is the **same** value found in `https://docs.zoom.us/doc/<fileId>` URLs and is what the transcripts bridge expects in the `fileId` query parameter (step 3).

> Note: the `main_file_id` field on `items/page` records is **not** this fileId — calling the bridge with `main_file_id` returns `404 REASON_FILE_NOT_FOUND`. Always use `get_meeting_assets`.

### 3. Pull the transcript

```bash
curl -sS "https://us01docs.zoom.us/api/bridge/meeting/transcripts/v2?meetingId={URL_ENCODED_MEETING_ID}&fileId=" \
  -H "authorization: Bearer $TOKEN" \
  -H 'origin: https://docs.zoom.us' \
  -H 'referer: https://docs.zoom.us/' \
  -H 'x-zm-cluster-id: aw1' \
  -H 'x-zm-docs-container: docs/browser' \
  -H 'user-agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36'
```

The bridge accepts either `meetingId` (from step 2) or `fileId` (from step 2.5 or from a `docs.zoom.us/doc/<fileId>` URL) — leave the other empty. Note the `==` at the end of meeting IDs must be URL-encoded as `%3D%3D`.

**Recommended strategy:** call the bridge with `meetingId` first. If `items` is empty, fall back to step 2.5 to resolve the fileId, then call the bridge again with `fileId`. The script does this automatically.

Response is JSON: `{"meetingId": "...", "meetingStartTime": "...", "items": [{text, startTime, endTime, userId, ...}], "speakers": [...]}`.

If the response is small (<5KB) or `items` is empty/short, the meeting may have ended very recently or had no AI Companion transcription. Note this to the user.

### 4. (Optional) Fetch meeting title for context

```bash
curl -sS 'https://docs.zoom.us/api/file/files/action/batch_get' \
  -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -H 'origin: https://docs.zoom.us' \
  -H 'referer: https://docs.zoom.us/' \
  -H 'x-zm-cluster-id: aw1' \
  -H 'x-zm-docs-container: docs/browser' \
  --data-raw '{"ids":["{FILE_ID}"]}'
```

Title is at `successItems[0].title`. (Not needed if you got the topic from `list_my_meetings`.)

### 5. Format as speaker-labeled dialogue

Use this Python snippet to merge consecutive utterances by the same speaker and label speakers `S1`, `S2`, ... in order of first appearance:

```python
import json
with open('/tmp/transcript.json') as f:
    obj = json.load(f)
items = obj.get('items', [])

labels, order = {}, 0
for it in items:
    if it['userId'] not in labels:
        order += 1
        labels[it['userId']] = f"S{order}"

out, cur_user, cur_text, cur_start = [], None, [], None
for it in items:
    u = it['userId']
    if u != cur_user:
        if cur_user is not None:
            out.append((cur_start, labels[cur_user], ' '.join(cur_text)))
        cur_user, cur_text, cur_start = u, [it['text']], it['startTime']
    else:
        cur_text.append(it['text'])
if cur_user is not None:
    out.append((cur_start, labels[cur_user], ' '.join(cur_text)))

print(f"Speakers: {list(labels.values())} ({len(labels)} total)\n")
for ts, spk, text in out:
    print(f"[{ts[:8]}] {spk}: {text}")
```

The Zoom API returns numeric `userId`s, not names. Speakers are usually identifiable from greetings and context in the first few turns — point this out so the user can map S1/S2/... to real people. (The script also reads `speakers[]` from the transcript when present and labels with real names.)

## Script-based flow (preferred for unattended / repeated use)

The skill folder ships a `zoom_transcript.py` helper that uses `curl_cffi` to
impersonate Chrome's TLS fingerprint — this is more resilient to Cloudflare
than plain `curl`/`requests` and lets you list meetings without needing a doc URL.

Dependencies are vendored alongside the script in `./vendor/` — no installs needed.

The lister auto-chunks any window >30d into 30d slices, paginates each, dedupes by meeting ID, and sorts newest-first. By default it filters to meetings where `hasTranscript || hasSummary || hasRecordingTranscript`; pass `--all` on `list` to disable the filter.

### Setup: capture cookies once

Save the cookie header (or the entire curl command) to a file the script can read:

```bash
# Either paste just the -b '...' value, or the whole `curl ...` command
cat > /tmp/zoom_cookies.txt <<'EOF'
_zm_ssid=...; cf_clearance=...; __cf_bm=...; zm_aid=...; (etc.)
EOF
chmod 600 /tmp/zoom_cookies.txt
```

### Commands

```bash
SKILL=/Users/alex.starr/.treasure-work/.claude/skills/auto-pull-zoom-transcript

# 1. Mint a Bearer (sanity check that cookies are alive)
python3 $SKILL/zoom_transcript.py mint --cookies /tmp/zoom_cookies.txt

# 2. List meetings (date, meetingId, topic). By default returns ALL meetings —
#    the list endpoint's hasTranscript/hasSummary flags are unreliable for very
#    recent meetings, so we don't filter on them.
python3 $SKILL/zoom_transcript.py list --cookies /tmp/zoom_cookies.txt --since 30d --limit 50
# Add --only-with-content to filter to flagged meetings (legacy behavior)
python3 $SKILL/zoom_transcript.py list --cookies /tmp/zoom_cookies.txt --since 7d --only-with-content

# 3. Pull transcript by meetingId (from `list`) — no doc URL needed
python3 $SKILL/zoom_transcript.py transcript --cookies /tmp/zoom_cookies.txt   --meeting-id 'SzMKmuW1SLCeIhtHEAPPAQ=='

# 4. Pull transcript by fileId (when user pasted a docs.zoom.us/doc/<id> URL)
python3 $SKILL/zoom_transcript.py transcript --cookies /tmp/zoom_cookies.txt   --file-id E4T_vNyiTLeQvDAi5fLuHQ

# 5. Find by topic substring and pull in one shot
python3 $SKILL/zoom_transcript.py find --cookies /tmp/zoom_cookies.txt   --match "Cognitiv" --pull

# 6. Bulk: pull every transcript from the last 60 days into a folder
#    (auto-chunks into 30d slices to stay under the server-side window cap)
python3 $SKILL/zoom_transcript.py bulk --cookies /tmp/zoom_cookies.txt --since 60d --out ~/Desktop/Zoom\ Transcripts
```

### Important: `meetingId` vs `fileId` vs `main_file_id`

The transcripts bridge takes one or the other:

- **`meetingId`** (from `list_my_meetings` — the `meetingId` field, e.g. `qYhsD/onTiGGmCJ2GAWgvA==`) —
  must be URL-encoded (`==` → `%3D%3D`). The script handles this. Often returns empty for very recent meetings.
- **`fileId`** (from a `docs.zoom.us/doc/<fileId>` URL, or from `get_meeting_assets.createFromMeeting[].doc.id`) —
  the most reliable lookup key. The script always falls back to this if `meetingId` returns empty.

`main_file_id` from `items/page` records is **not** the same fileId — calling
the bridge with `main_file_id` returns `404 REASON_FILE_NOT_FOUND`. Use
`get_meeting_assets` (step 2.5) to resolve the correct doc fileId.

### When to use which flow

| Situation | Flow |
|---|---|
| User pastes a `docs.zoom.us/doc/<id>` URL | curl `--file-id` or original cURL block |
| User says "the Cognitiv meeting" / "yesterday's planning sync" / "most recent" | `find` or `list` then `transcript --meeting-id` |
| User wants to download many transcripts to disk | `bulk --since <window> --out <dir>` |
| Cloudflare 403 / HTML challenge from plain curl | Switch to the script (curl_cffi) |
| Scheduled / agent invocation | Script only — bare curl will fail Cloudflare more often |

## Output format

After pulling, give the user:

1. **One-line confirmation** — meeting title, total utterances, total turns after merging, number of speakers.
2. **The full dialogue** — speaker-labeled with timestamps, in a code block or as plain text.
3. **(If asked or if long)** A 3–6 bullet summary of the meeting.

If the transcript is large (>200 turns), offer to save it to the workspace as a note instead of printing inline.

For `bulk`, the script writes:
- `<out>/YYYY-MM-DD-<slug>.md` — YAML frontmatter (title, date, host, attendees, speakers, etc.) + speaker-labeled dialogue
- `<out>/.raw/YYYY-MM-DD-<slug>.json` — raw meeting + transcript JSON for re-processing

## Caveats to mention when relevant

- Token expires in ~1 hour — re-mint via step 1 if needed. The `bulk` flow auto-refreshes mid-batch on 401.
- Cookies (especially `__cf_bm`, `cf_clearance`, `cred`) rotate. If NAK starts returning HTML or 401, cookies are stale and need re-capturing from DevTools.
- `list_my_meetings` enforces a ~30d server-side window. Wider windows return `code: 500, reason: REASON_UNSPECIFIED`. The script chunks automatically, but a raw curl will fail.
- The list endpoint's `hasTranscript`/`hasSummary` flags lag behind real ingestion — a meeting can have a fully-generated transcript while the list still says `hasTranscript=false`. For that reason the script no longer filters on those flags and always tries the bridge per meeting.
- This is unsupported / brittle — Zoom can break it any time. For a long-term automation, suggest the **Server-to-Server OAuth app** approach instead.
- Never print the captured cookie jar or Bearer token back to the user in plain text — they're live credentials.

## Examples

### Example: User pastes a doc URL with fresh cookies

**User:** "Pull the transcript from https://docs.zoom.us/doc/abc123XYZ — here's a curl with fresh cookies: `curl 'https://docs.zoom.us/...' -b '_zm_ssid=...; cred=...; ...'`"

**You do:**
1. Extract `fileId=abc123XYZ` from URL and cookies from `-b '...'`.
2. POST to NAK → get fresh Bearer.
3. GET transcripts with `fileId=abc123XYZ`.
4. Format and print dialogue.

**Output starts with:**
> Pulled "Q3 Planning Sync" — 412 utterances, 287 turns, 5 speakers.
>
> ```
> [00:00:14] S1: Hey team, thanks for joining...
> ```

### Example: Cookies are stale

**You see:** NAK returns HTML or 401.

**You say:** "The cookies are stale (NAK returned a Cloudflare challenge / 401). Open `https://docs.zoom.us` in Chrome, DevTools → Network → copy any request as cURL, and paste it here."
