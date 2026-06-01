---
name: cs-engagement-report
description: Use when CS leadership wants to assess customer engagement levels, identify under-engaged accounts, check meeting frequency against tier-based mandates, or generate a health scorecard for a CSM's book of business. Triggers on "engagement report", "meeting frequency report", "customer engagement", "CSM scorecard", "accounts we haven't met", "care and feeding report", "engagement health", "under-engaged accounts", "red accounts", "meeting cadence", "CS health check", "book of business review", "Americas team rollup", "EMEA team rollup", or any request to visualize CSM-to-customer meeting activity against expected standards.
---

# CS Engagement Report

> **Canonical rule source: `lib.py` (sibling file in this skill folder).**
> The deterministic decision rules in this skill — tier classification (Step 6),
> Green/Yellow/Red scoring (Step 7), CSM region majority-rule (CSM Discovery),
> contact-first event attribution (Step 4), and domain disambiguation — are
> implemented as pure Python functions in `lib.py`. The prose below explains
> the rules in narrative form, but `lib.py` is the source of truth.
>
> Functions: `tier_from_arr`, `score_account`, `resolve_csm_region`,
> `attribute_event_to_company`, `disambiguate_domain`. The
> `cs-engagement-report-redteam` skill imports these directly to test them, so
> when a rule changes, update `lib.py` and the matching prose section together.

Generate an interactive React dashboard that scores CSM engagement against tier-based meeting frequency mandates by cross-referencing Google Calendar data with HubSpot account/deal data.

## Report Versions

This skill produces **two distinct report versions**. Pick one based on the user's request:

| Mode | Trigger | Template |
|------|---------|----------|
| **Regional Roundup** | "Americas team rollup", "EMEA engagement", "APAC roundup", "show me {region} engagement", any request scoped to a region | See `## Regional Roundup Template` below — **canonical, do not deviate** |
| **Individual Roundup** | "engagement report for {name}", "{name}'s book of business", "Alex Starr's accounts", any request scoped to a single CSM | See `## Individual Roundup Template` below |

## Overview

This skill produces a customer engagement health report that:
1. Pulls a CSM's calendar meetings (L30D and L60D)
2. Identifies external (client) meetings by **looking up each external attendee in HubSpot contacts and reading the contact's associated company** (contact-first attribution — never start from the email domain)
3. Scores each account's meeting frequency as Green/Yellow/Red against tier thresholds
4. Surfaces alerts for high-value accounts with insufficient engagement
5. Renders an interactive React dashboard with filtering and drill-down

## Data Sources

- **Google Calendar** — CSM's meetings (accepted or organized) over L60D minimum
- **HubSpot Contacts** — Resolve each external attendee email → contact → `associatedcompanyid`. This is the primary attribution path.
- **HubSpot Companies** — Get company name, owner (CSM assignment via `hubspot_owner_id` / `ownername`), domain, **region** (via `td_company_owner_region`), and **CSM identity** (via `csm_email` + `csm__c`). Also the source of the CSM→region matrix.
- **HubSpot Deals** — Get ARR from closed-won deals (`dealstage` containing "6.0 Closed Won", `amount` field) associated with companies. Note: for tier classification you can use `companies.arr_won_all_rollup` directly instead of summing deals.

## CSM Discovery

CSM→region mapping is **derived at runtime from HubSpot alone** by following the procedure in **`csm-region-mapping.yaml`** — a sibling file inside this skill folder (bundled with the skill, not workspace-relative). The file is a *playbook*, not a static lookup table — it specifies the query, sharding strategy, identity normalization, and aggregation rules. Read it first, then execute the steps it documents.

**What the playbook specifies:**
- **Single source — HubSpot company-region.** Use `hubspot_search_crm_objects` on `companies` with `csm_email` and `td_company_owner_region` populated. Both fields live on the same company record, so one paginated query covers everything.
- **Sharding** (`inputs.primary.sharding`): the MCP tool caps each response at 100 records and exposes no cursor. Fan out 8 parallel calls sharded by `hs_object_id` ranges (the playbook lists the exact boundaries that keep each shard under 100 as of 2026-05-19). If any shard returns `count == 100` in a future run, subdivide it.
- **Identity normalization** (`steps[2]`): dedupe `csm_email` on the local part, lowercased. The same CSM appears under both `@treasure.ai` and `@treasure-data.com`; failing to dedupe splits their book in half.
- **Region aggregation** (`steps[3]`): for each `csm_local`, count valid regions across their accounts and take the majority — that majority is the CSM's `csm_region`, full stop. **`csm_region` is the only field that decides which regional roundup a CSM appears in.** A CSM whose majority is EMEA reports in EMEA even if half their accounts are tagged Americas; their Americas-tagged accounts come along for the ride, they do not split off into the Americas roundup. Set `cross_region: true` when **the minority of *valid* regions** is ≥20% AND ≥2 accounts (catches books like Ana María Villota Serrano's Americas+EMEA Nestlé split, Carol Nicholson-Cole's UK+US Mars/Levi, Naohiro Yoshikawa's Japan+Korea); the flag is for surfacing the breakdown in the rendered report, NOT for assigning an account to a different region. Null/garbage region values are missing-data noise — they are excluded from both the numerator and denominator of the minority calculation, so a CSM with 3 Americas + 2 unknown-region accounts is single-region, not cross-region. **Tie-break (deterministic):** when two regions tie for first place, pick in this order: `Americas` > `EMEA` > `Japan` > `ASIAexJapan` > `APAC`. Document the tie in the matrix as `tied_majority: [Americas, EMEA]`.
- **Confidence** (`steps[4]`): based on book size alone — `high` if `n_accounts ≥ 5`, `medium` if 2-4, `low` if 1.
- **Display name resolution** (`steps[5]`): apply the override map first (Ana María Villota Serrano → `ana.villotaserrano`, Sean Valencia → `valencia`, Megan Roy → `meg.roy`, Tsuyuki Ino → `ino`, Kazuki Tsukahara → `tsukahara`, Makoto Sekikawa → `sekikawa`, Nao Nakatani → `naohiro.nakatani`), otherwise period-delimiter capitalization (`meg.roy` → `Meg Roy`).
- **Output matrix**: one row per CSM with `name`, `email`, `region`, `confidence`, `n_accounts`, `arr_total`, `region_breakdown`, `cross_region`. Plus a per-account table the rest of this skill consumes directly as the book-of-business filter.
- **Excluded sources** (do not use): Greenspace HeatMap (now redundant — every column derived from HubSpot company properties + deal associations), Gainsight (no region field), Glean people search (no structured region), `hubspot_search_owners` (blocked by current MCP auth scope; would be the preferred display-name source if unblocked).

**Valid regions:** `Americas`, `EMEA`, `Japan`, `ASIAexJapan`, `APAC`.

**Known pitfalls** (from `known_pitfalls` in the YAML — always honor):
- The same CSM appears under both `@treasure-data.com` and `@treasure.ai`. Dedupe on the local part before aggregating.
- Cross-region books are real and structural — don't reassign the CSM's primary region; flag with `cross_region: true` and let the rendered report show the breakdown. **And do NOT split a cross-region CSM's accounts across two regional roundups.** A CSM lives in exactly one regional roundup (their majority `csm_region`); their full book travels with them. Filtering the per-account table by `td_company_owner_region == <target>` is the canonical way to produce a wrong report — the audit signature is "a CSM's account appears in region X but the CSM is missing from the rollup heading." If you see that pattern, the scoping logic is account-first instead of CSM-first; restart the run and apply the rule in Step 1.3.
- `csm_email` occasionally contains a name string ("Hiroki Ito") or external partner address (`jamie@origin63.com`). Filter these as data errors — require either a `.` in the local part or a known single-token override (`valencia`, `ino`, `tsukahara`, `sekikawa`), AND a `treasure.ai` / `treasure-data.com` domain.
- Deactivated CSMs are indistinguishable from active ones in HubSpot company data alone. Surface via a separate signal (HubSpot owner last-login if accessible, or a workspace-maintained `deactivated_csms.yaml`).
- `arr_won_all_rollup` occasionally has negative or zero values (cancelled deals, unreconciled credits). Floor to 0 when summing.

**Calendar email derivation:** Take the CSM's `csm_email` from the matrix and replace `@treasure-data.com` with `@treasure.ai` for the Google Calendar lookup (see Step 2 for the 404 fallback). The HubSpot value is the authoritative identity, but the calendar mailbox typically lives on `@treasure.ai`.

**Display-name formatting (required):** Whenever a CSM is shown in the UI (header sub-line, table CSM column, "Book of Business by CSM" rollup, filter dropdown, etc.), render their **proper name** — never the email login. Resolve in this order:

1. **Primary — override map in the playbook.** Step 5 of `csm-region-mapping.yaml` lists the 7 CSMs whose names can't be recovered from their email (concatenated last names like `villotaserrano`, single-token aliases like `valencia` / `ino` / `tsukahara` / `sekikawa`). Use the override value verbatim, including diacritics and middle names.
2. **Secondary — HubSpot owner record** (when accessible). If `hubspot_search_owners` is unblocked, use the owner's full name as a cross-check. Today this endpoint is blocked by MCP auth scope, so skip until resolved.
3. **Fallback — period-delimiter rule.** For everyone else: split the email local part on `.` and `-`, capitalize each token, join with a single space. Example: `meg.roy` → `Meg Roy`, `carol.nicholson-cole` → `Carol Nicholson Cole` (loses the hyphen — known limitation; add to the override map if the polished form matters).

Always keep the raw email available on the account record (e.g. `csmEmail`) for joins, but never display it.

## Region Support

Valid region values come from `valid_regions` in `csm-region-mapping.yaml`:
- `Americas`
- `EMEA`
- `Japan`
- `ASIAexJapan`
- `APAC`

Regional roundup mode accepts any of these. `APAC` is a union — if any CSM ever resolves to a literal `APAC` value (HubSpot may add this in future), treat them as covering both `Japan` and `ASIAexJapan`. As of 2026-05-19, no CSM resolves to `APAC` directly; they all land in `Japan` or `ASIAexJapan` based on majority. When the user asks for a "Japan" or "ASIAexJapan" report specifically, do not roll APAC-tagged CSMs in.

**Membership is determined by `csm_region`, never by `td_company_owner_region`.** A CSM's `csm_region` (the majority of their book) decides which regional roundup they appear in; the per-account region tag is informational only. This is non-negotiable — see Step 1.3 for the deterministic algorithm and the cross-region pitfall above for the failure mode.

## Step-by-Step Process

### Step 0: Parse user-supplied filter overrides

Before running the report, parse the user's request for filter overrides. Today there's one configurable knob; treat the rules below as the canonical phrasings.

**`min_arr`** — minimum `arr_won_all_rollup` for an account to appear on the scorecard. Defaults to **`1`** (drops $0 ARR accounts, which are usually stale HubSpot ownership assignments — closed-lost deals or churned customers not yet reassigned). Override when the user says any of:

- "include zero-ARR accounts" / "include $0 accounts" / "include all accounts" → `min_arr = 0`
- "only accounts over $X" / "min ARR $X" / "filter to $X+ ARR" → `min_arr = X` (parse `250K`, `1M`, `100,000`, etc.)
- "include prospects" / "include unqualified" → `min_arr = 0` (treat as the include-all alias)

Excluded accounts are not silently dropped — they appear in the dashboard's `excludedAccounts` footer so the user can see what was filtered and re-run with a different threshold if needed (see Step 8 data shape).

When in doubt about the user's intent, default to `min_arr = 1` and rely on the footer to surface what was filtered. Do NOT prompt the user with AskUserQuestion for this — the default + footer combination handles the common case without an extra turn.

### Step 1: Identify the CSM(s)

1. **Read the playbook.** Open `csm-region-mapping.yaml` to refresh on the sharding strategy, identity overrides, aggregation rules, and known pitfalls.
2. **Execute the playbook to produce the CSM matrix:**
   - Fan out 8 parallel `hubspot_search_crm_objects` calls on `companies`, sharded by `hs_object_id` range per `inputs.primary.sharding`. Request `hs_object_id`, `name`, `csm_email`, `td_company_owner_region`, `arr_won_all_rollup`, and `domain`. Filter to records with both `csm_email` and `td_company_owner_region` populated.
   - Concatenate shard results, dedupe by `hs_object_id`, drop bogus `csm_email` values (no `.` and not in the single-token allowlist, or non-TD domain).
   - Group by `csm_email` local part (lowercased). For each CSM compute: majority region, `n_accounts`, `arr_total` (floor negatives to 0), `region_breakdown`, and `cross_region` flag.
   - Apply confidence rules (`high` if `n_accounts >= 5`, `medium` if 2-4, `low` if 1).
   - Resolve display names via the override map first, then the period-delimiter fallback.
3. **Scope to the requested mode — CSM-first, never account-first.** The unit of inclusion is the **CSM**, not the individual account. Once a CSM is in scope, **every account they own travels with them**, regardless of each account's `td_company_owner_region` tag.
   - **Regional rollup**:
     1. Compute `csm_region` for every CSM via the playbook's majority rule (`steps[3]` of `csm-region-mapping.yaml`).
     2. Build `in_scope_csms = { csm_local | csm_region == <target> }`. For `APAC`, expand to `csm_region in {APAC, Japan, ASIAexJapan}` unless the user narrowed.
     3. The scorecard is the union of every account owned by every CSM in `in_scope_csms` — **including their cross-region accounts** (e.g. a Carol Nicholson-Cole `csm_region = EMEA` book includes her US-tagged Mars/Levi accounts under EMEA, NOT Americas). The cross-region accounts render in the scorecard with their original region tag visible, but they are scored under their CSM's region.
     4. **Hard rule**: an account is in the Americas roundup **if and only if** its owning CSM resolves to `csm_region == Americas`. An account whose own `td_company_owner_region == Americas` but whose CSM resolves to EMEA does NOT appear in the Americas roundup. The rendered footer should list these as "owner region mismatch" only when the user explicitly asks for the audit; do not surface by default.
   - **Single CSM**: look up by name in the matrix; their resolved `csm_region` drives the report header. The scorecard is their full book regardless of per-account region tags. If a name doesn't reconcile, tell the user — don't guess.

   **Deterministic check before continuing**: print the in-scope CSM list (count + names) before pulling any calendar data. If a CSM the user expected is missing — or one they didn't expect is present — the cause is the majority-region computation in step 1.2, not this step. Re-derive `csm_region` rather than hand-patching the scope.

4. **Resolve each CSM's calendar email**: take the matrix `email` and replace `@treasure-data.com` with `@treasure.ai` (see Step 2 for the 404 fallback).
5. **Pull each CSM's book of business.** The per-account table emitted by `steps[6]` of the playbook has `{hs_object_id, name, csm_email_local, region, arr, domain}` for every account. Build the scorecard as:

   ```
   scorecard_rows = [row for row in per_account_table
                     if row.csm_email_local in in_scope_csms]
   ```

   **Never** filter the per-account table by `row.region == <target>` — that produces the bug where individual cross-region accounts (Carol's US Mars, Gaelle's Kuwait Americana) leak into the wrong roundup while their owning CSM appears nowhere. The only valid filter at this step is `csm_email_local in in_scope_csms`.
6. **Apply the `min_arr` filter from Step 0.** Partition the book into:
   - **`scorecard_accounts`**: rows where `arr >= min_arr`. These are the accounts that get calendar lookups and appear in the scorecard table.
   - **`excluded_accounts`**: rows where `arr < min_arr`. These skip calendar work entirely and surface in the dashboard's `excludedAccounts` footer with `{company, arr, reason}`. For the default (`min_arr = 1`), `reason = "stale_ownership"`; for higher thresholds, `reason = "below_arr_threshold"`. Don't run contact lookups, calendar queries, or attribution for excluded accounts — the filter is a book-of-business shaper, not a scoring rule.

### Step 2: Pull Calendar Data

The Google Calendar API exposes any calendar the authenticated user has at least "See all event details" access to — including calendars shared via Workspace org-wide sharing, even if they don't appear on the user's calendar list. Pass the CSM's email as `calendar_id`; the calendar email is typically `{first.last}@treasure.ai` (the `@treasure-data.com` form in HubSpot is usually an alias and not the calendar address).

```
google_calendar_list_events:
  calendar_id: "{first.last}@treasure.ai"
  time_min: <60 days ago, ISO 8601>
  time_max: <today, ISO 8601>
  max_results: 250
```

A `404 Not Found` from this call means the calendar isn't accessible — record those CSMs in the "Not Accessible" list (Step 9) instead of failing the whole report.

**Pagination — required for active CSMs.** The Calendar API caps `max_results` at 250 per call, and the MCP tool does not currently expose `pageToken`. An active CSM frequently exceeds 250 events over 60 days, so a single call will silently truncate the window (later events get dropped, not earlier ones — the API returns events in time order from `time_min`).

Use **date-range chunking** instead of `pageToken`:

1. Issue the first call with the full L60D window (`time_min = today - 60d`, `time_max = today`).
2. If the response has `count == 250`, the window is truncated. Find the latest event start date in the response (call it `last_returned`).
3. Issue a follow-up call with `time_min = last_returned` (same `time_max = today`). Repeat until a call returns `count < 250`.
4. Concatenate events across calls and **deduplicate by event `id`** — recurring instances and the boundary event will appear in both pages.
5. In practice 2 calls cover most CSMs over L60D; very busy ones may need 3. Cap at 4 calls per CSM to bound runtime.

Across CSMs, fan out the first calls in parallel (one per CSM), then issue follow-up chunks only for the calendars that hit the 250 cap.

**Calendar email resolution.** If `{first.last}@treasure.ai` returns 404, do NOT silently skip — try `{first.last}@treasure-data.com` once, and if that also 404s, mark the CSM as "calendar not accessible" and continue. Do not retry endlessly with name variants; surface the gap in the final report.

**Token-budget note.** The MCP response can exceed the assistant's token limit on busy calendars; the tool will spill the full result to a file and return a path. Read and parse that file with a script rather than re-running the call.

### Step 3: Identify External Meetings

For each calendar event, classify as external if:
1. Event has attendees with email domains **outside** these internal domains: `treasuredata.com`, `treasure-data.com`, `treasure.ai`
2. Skip system domains: `resource.calendar.google.com`, `group.calendar.google.com`, `calendar.google.com`
3. The CSM either **accepted** the event (`status: "accepted"`) or is the **organizer**
4. **eventType filter**: drop events with `eventType: "outOfOffice"` or `eventType: "focusTime"` — these are personal calendar blocks, not customer engagement. `eventType: "default"` and `eventType: "workingLocation"` are eligible. Missing/unknown `eventType` is treated as `default` (eligible) for legacy compatibility. See `lib.py:attribute_event_to_company` — the drop reason is `non_engagement_event`.

### Step 4: Attribute Meetings to Clients (CONTACT-FIRST — NEVER DOMAIN-FIRST)

**Direction of attribution.**
Start from the **calendar invitee list**. For each external attendee, look up the **HubSpot contact by email**, then read the contact's **`associatedcompanyid`** (or primary company association) to find the company. Match that company against the **CSM's book of business** (collected as `hs_object_id` set in Step 1.5). This is the only correct attribution path because:
- A single corporate domain can map to multiple HubSpot company records (e.g. `nestle.com` covers Nestlé Brasil, Marcas Nestlé México, Nestlé Purina, etc. — domain-matching collapses them all into one and mis-attributes meetings).
- Contacts on free-mail providers (`gmail.com`, `yahoo.com`, `hotmail.com`) are only resolvable through the contact record.
- Subsidiaries, JVs, and agency-of-record relationships only show up correctly through the contact's company association.

**Known partner domains** (used only as a pre-filter for noise — never as an attribution shortcut):
- `liveramp.com`, `valtech.com`, `acxiom.com`, `publicismedia.com`, `starcomww.com`, `lakefrontdatasolutions.com`, `partner.fluidra.com`

**Attribution algorithm (in order):**
1. Build the external-attendee email list per event: drop internal TD attendees (`treasure.ai`, `treasure-data.com`, `treasuredata.com`) and system-domain entries (`*.calendar.google.com`).
2. Drop attendees whose domain is in the partner list above. They're noise, not clients.
3. **For each remaining email, look up a HubSpot contact** via `hubspot_search_crm_objects` on `contacts` with `email EQ <attendee_email>`, requesting properties `email`, `associatedcompanyid`, `firstname`, `lastname`, `company`. Cache by email — the same attendee will recur across many events, so query each unique address once per run.
4. From each matched contact, take `associatedcompanyid` (or, if multiple associations exist, the primary company association). Look up that `hs_object_id` in the CSM's book of business set.
5. If the company is in the CSM's book → attribute the meeting to that company.
6. If attendees on a single event resolve to **multiple distinct companies** in the book → log the meeting once per company (e.g. a joint Nestlé Brasil + Marcas Nestlé México sync counts for both records).
7. If **0 attendees resolve to a company in the CSM's book**, drop the meeting. Do not infer the client from the event title or location. Do not flag as "Unattributed."

**Domain-only fallback — last resort, narrowly scoped.**
Falling back to "domain matches a HubSpot company's `domain` field" is allowed only when **every** external attendee on the event has no HubSpot contact record at all. In that case attribute by domain *only if the domain is unambiguous in the CSM's book* — i.e. exactly one company in their book has that domain. If the domain maps to multiple companies in the book (Nestlé case, Honda case), drop the meeting and flag the attendee email for sales-ops to add as a HubSpot contact. This fallback exists because brand-new prospects sometimes haven't been logged as contacts yet; it must never be the default path.

**Caching:**
- Cache contact lookups by email — one query per unique attendee. Persist the cache across CSMs in a single run; many emails recur (TD field engineers, partner agencies, etc.).
- Cache the per-CSM book-of-business `hs_object_id` set in memory; the contact's `associatedcompanyid` only counts if it's in that set.
- Keep a `domains_with_no_contact_match` audit list — emit it at the end of the run as a sales-ops follow-up so HubSpot coverage improves over time.

### Step 5: Get Account Metadata from HubSpot

For each identified client company:
1. Search HubSpot companies by name (full-text query)
2. Pull: `name`, `domain`, `hubspot_owner_id`, `ownername`, `td_company_owner_region`
3. Search deals by company name for closed-won deals (stage `1670665975` or `1670572765`) and get `amount`
4. Sum closed-won deal amounts as the account's ARR

**Note:** Always search companies by name, not domain — the company `domain` may not match the email domain (e.g., `royalcaribbean.com` vs `rccl.com`). And remember: in this skill, the `domain` field is *only* used for the narrow fallback in Step 4. The primary attribution path goes through `associatedcompanyid`.

### Step 6: Determine Account Tier from ARR

Default tier thresholds (configurable):

| Tier | ARR Range |
|------|-----------|
| Strategic | > $1,000,000 |
| Enterprise | $250,000 – $1,000,000 |
| Commercial | $100,000 – $250,000 |
| Digital | < $100,000 |

If ARR is unknown, default to **Commercial** and flag for review.

**Digital tier is unscored.** Digital accounts have no engagement mandate — meetings may be logged for reference but Green/Yellow/Red status is not computed. They render with a neutral "Digital" badge in the scorecard, no threshold ratio, and no Days-Since color. Excluded from KPI percentages and Engagement-by-Tier counts.

### Step 7: Score Meeting Frequency (Green/Yellow/Red)

**Strategic:** Green ≥3 L30D | Yellow ≥5 L60D (but <3 L30D) | Red <5 L60D
**Enterprise:** Green ≥2 L30D | Yellow ≥3 L60D (but <2 L30D) | Red <3 L60D
**Commercial:** Green ≥1 L30D | Yellow ≥1 L60D (but 0 L30D) | Red <1 L60D
**Digital:** Unscored. Status renders as "Digital" (neutral badge, no RAG color). Meeting counts still display for reference but no threshold is enforced.

**Date math — always compare dates, not datetimes.** When computing `daysSinceLast` and the L30D/L60D windows, use **calendar dates only** (drop the time component). Compute as `(today.date() - meeting_start.date()).days`. If you compare full datetimes, a meeting that starts later today (after the moment you snapshot "now") will produce a negative `daysSinceLast` and drop into the wrong bucket. Same-day meetings should always render as `0d`, never `-1d`. Apply the same dates-only rule to the L30D/L60D thresholds — a meeting from exactly 30 days ago counts as L30D.

### Step 8: Render Interactive React Dashboard

**HARD REQUIREMENT — row-level meeting drill-down.** Every rendered dashboard must support clicking an account row to expand and reveal the meetings counted for that account. The React component below implements this — do not remove the `expandedRow` state, the click handler on `<tr>`, or the expansion `<tr>` that renders the meeting list. If you ever produce a report and the user cannot click an account to see its meetings, the report is wrong and must be regenerated. This applies equally to the Regional Roundup and Individual Roundup templates.


Pick the template based on report mode (see top of skill):
- **Regional Roundup** → use the Regional Roundup Template (below) verbatim
- **Individual Roundup** → use the Individual Roundup Template (below)

**Common styling defaults:**
- Green = `#22c55e`, Yellow = `#eab308`, Red = `#ef4444`
- Use RAG circles/badges for status indicators
- Sort Red accounts first by default (most urgent)

---

## Regional Roundup Template

When generating a **regional report** (Americas, EMEA, APAC, or multi-region), call `mcp__work__render_react` with the React component below. **This is the canonical regional roundup format — use the component code verbatim. Do not change layout, ordering, styling, color tokens, Tailwind classes, copy, or interactions. The only thing that varies between runs is the `data` payload.**

**Required `data` shape:**

```json
{
  "csm": "CSM name (or 'Region Roundup' for multi-CSM)",
  "region": "Americas | EMEA | APAC",
  "reportDate": "YYYY-MM-DD",
  "windowLabel": "Mar 13 – May 12, 2026",
  "tierThresholds": {
    "Strategic":  { "greenL30D": 3, "yellowL60D": 5, "label": "> $1M ARR" },
    "Enterprise": { "greenL30D": 2, "yellowL60D": 3, "label": "$250K–$1M ARR" },
    "Commercial": { "greenL30D": 1, "yellowL60D": 1, "label": "$100K–$250K ARR" },
    "Digital":    { "unscored": true, "label": "< $100K ARR" }
  },
  "accounts": [
    {
      "company": "string",
      "domain": "string",
      "csm": "Display Name",
      "csmEmail": "first.last@treasure-data.com",
      "tier": "Strategic | Enterprise | Commercial | Digital",
      "region": "Americas | EMEA | Japan | ASIAexJapan",
      "arr": 1640160,
      "meetingsL30D": 6,
      "meetingsL60D": 11,
      "thresholdL30D": 3,
      "thresholdL60D": 5,
      "lastMeeting": "YYYY-MM-DD",
      "daysSinceLast": 6,
      "calendarUnavailable": false,
      "meetings": [
        {
          "id": "calendar-event-id",
          "title": "Quarterly Business Review",
          "start": "2026-04-29T14:00:00-04:00",
          "displayStart": "Wed, Apr 29, 2026 · 2:00 PM UTC-04:00",
          "organizer": "first.last@treasure.ai",
          "attendees": [
            { "email": "...", "name": "...", "status": "accepted | declined | tentative | needsAction", "internal": false }
          ]
        }
      ]
    }
  ],
  "minArrApplied": 1,
  "excludedAccounts": [
    { "company": "Some Stale Account", "csm": "Display Name", "arr": 0, "reason": "stale_ownership" }
  ]
}
```

**`excludedAccounts` is REQUIRED** (always present, may be `[]`). It surfaces every account that was filtered out by the `min_arr` threshold from Step 0. The footer renders a collapsed list so the user can see what was filtered without polluting the scorecard. `minArrApplied` echoes the threshold the report used so the rendered footer can label it correctly (e.g. "10 accounts filtered out (below $1 ARR)").

**`meetings` field is REQUIRED on every account record.** This is non-negotiable — the dashboard must support row-level drill-down to the meetings that produced the L30D/L60D counts. If you produce a dashboard without per-account meeting lists, the report is wrong and must be regenerated. Sort each account's `meetings` newest-first (descending by date). If an account has zero counted client meetings, set `meetings: []` (still required as an empty array, never omitted).

**How the meeting list is built (from Step 4 attribution):** As you attribute each calendar event to a HubSpot company (via contact-first lookup, or the narrow domain fallback), append a record to that account's `meetings` array. Carry forward at minimum: `date` (YYYY-MM-DD), `title`, `isOrganizer`, `response`, `externalDomains`. Do **not** include the raw event ID, attendee emails, or event description — keep the payload compact for render time.

**Sections rendered (in order):**
1. **Header** — `{region} CS Engagement Report` + sub-line (CSM, region, report date, window)
2. **KPI Cards** (5 cards) — Total Accounts, Total ARR, Green count + %, Yellow count + %, Red count + %
3. **Engagement by Tier** — Horizontal stacked bar chart (Recharts)
4. **Book of Business by CSM** — Per-CSM rollup with G/Y/R counts and total ARR. Each CSM row is a clickable button that toggles the table's CSM filter to that CSM (click again to clear). Show a small "Clear filter" link next to the section heading whenever a CSM is selected, and visually highlight the selected row.
5. **Filters** — Tier, Status, CSM dropdowns + count display
6. **Engagement Scorecard Table** — Sortable: Status, Account, CSM, Tier, ARR, Mtgs L30D (with `/threshold`), Mtgs L60D, Days Since (color-coded), Last Meeting. **Each account row is clickable** — clicking expands an inline panel listing every meeting in the lookback window with title, date/time, organizer, and full invite list with RSVP statuses (accepted / tentative / declined / no response). Click again to collapse.
7. **Scoring Thresholds** — Reference card
8. **Excluded Accounts** (footer) — Collapsible list of accounts filtered out by the `min_arr` threshold. Header: "X accounts filtered out (below $Y ARR)". Each row shows company name, CSM (regional mode only), ARR, and reason. If `excludedAccounts` is empty, the section is hidden entirely. Renders below Scoring Thresholds.

**Explicitly NOT included** (do not add):
- Alert banners (CRITICAL / WARNING)
- Calendar coverage / scope notes
- Status Distribution pie chart
- Deactivated CSM list

**Component code (use verbatim):**

```jsx
export default function EngagementDashboard({ data }) {
  const [tierFilter, setTierFilter] = useState("All");
  const [statusFilter, setStatusFilter] = useState("All");
  const [csmFilter, setCsmFilter] = useState("All");
  const [sortField, setSortField] = useState("status");
  const [sortDir, setSortDir] = useState("asc");
  const [expandedRow, setExpandedRow] = useState(null);
  const [excludedOpen, setExcludedOpen] = useState(false);

  const isIndividual = !!data.individualReport;

  const getStatus = (a) => {
    if (a.tier === "Digital") return "Digital";
    if (a.calendarUnavailable) return "Unknown";
    const t = data.tierThresholds[a.tier];
    if (!t || t.unscored) return "Digital";
    if (a.meetingsL30D >= t.greenL30D) return "Green";
    if (a.meetingsL60D >= t.yellowL60D) return "Yellow";
    return "Red";
  };

  const statusOrder = { Red: 0, Yellow: 1, Green: 2, Digital: 3, Unknown: 4 };
  const accounts = data.accounts.map((a) => ({ ...a, status: getStatus(a) }));
  const csms = [...new Set(accounts.map((a) => a.csm))].sort();

  const filtered = accounts.filter((a) => {
    if (tierFilter !== "All" && a.tier !== tierFilter) return false;
    if (statusFilter !== "All" && a.status !== statusFilter) return false;
    if (csmFilter !== "All" && a.csm !== csmFilter) return false;
    return true;
  });

  const sorted = [...filtered].sort((a, b) => {
    let cmp = 0;
    if (sortField === "status") cmp = statusOrder[a.status] - statusOrder[b.status];
    else if (sortField === "arr") cmp = b.arr - a.arr;
    else if (sortField === "meetingsL30D") cmp = a.meetingsL30D - b.meetingsL30D;
    else if (sortField === "daysSinceLast") cmp = b.daysSinceLast - a.daysSinceLast;
    else if (sortField === "company") cmp = a.company.localeCompare(b.company);
    else if (sortField === "tier") cmp = a.tier.localeCompare(b.tier);
    else if (sortField === "csm") cmp = a.csm.localeCompare(b.csm);
    return sortDir === "asc" ? cmp : -cmp;
  });

  const calAvail = accounts.filter((a) => !a.calendarUnavailable);
  const scored = calAvail.filter((a) => a.tier !== "Digital");
  const digitalCount = accounts.filter((a) => a.tier === "Digital").length;
  const totalAccounts = accounts.length;
  const greenCount = scored.filter((a) => a.status === "Green").length;
  const yellowCount = scored.filter((a) => a.status === "Yellow").length;
  const redCount = scored.filter((a) => a.status === "Red").length;
  const scoredDenom = scored.length || 1;
  const totalARR = accounts.reduce((s, a) => s + a.arr, 0);

  const fmt = (v) => v >= 1e6 ? `$${(v/1e6).toFixed(1)}M` : v >= 1e3 ? `$${(v/1e3).toFixed(0)}K` : `$${v}`;

  const StatusBadge = ({ status }) => {
    const colors = {
      Green: "bg-emerald-100 text-emerald-800 dark:bg-emerald-900 dark:text-emerald-200",
      Yellow: "bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200",
      Red: "bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200",
      Digital: "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300",
      Unknown: "bg-gray-200 text-gray-700 dark:bg-gray-700 dark:text-gray-300",
    };
    const dots = { Green: "#22c55e", Yellow: "#eab308", Red: "#ef4444", Digital: "#64748b", Unknown: "#9ca3af" };
    return (
      <span className={`inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium ${colors[status]}`}>
        <span className="w-2 h-2 rounded-full" style={{ backgroundColor: dots[status] }}></span>
        {status === "Unknown" ? "No Cal" : status}
      </span>
    );
  };

  const AttendeeStatusDot = ({ status }) => {
    const palette = {
      accepted: { bg: "bg-emerald-500", label: "Accepted" },
      declined: { bg: "bg-red-500", label: "Declined" },
      tentative: { bg: "bg-yellow-500", label: "Tentative" },
      needsAction: { bg: "bg-gray-400", label: "No response" },
    };
    const p = palette[status] || palette.needsAction;
    return <span title={p.label} className={`inline-block w-2 h-2 rounded-full ${p.bg}`} />;
  };

  const handleSort = (f) => {
    if (sortField === f) setSortDir(sortDir === "asc" ? "desc" : "asc");
    else { setSortField(f); setSortDir("asc"); }
  };
  const SortHeader = ({ field, children }) => (
    <th className="px-3 py-2 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase cursor-pointer hover:text-gray-700 dark:hover:text-gray-200 select-none" onClick={() => handleSort(field)}>
      <span className="inline-flex items-center gap-1">{children}{sortField === field && <span>{sortDir === "asc" ? "▲" : "▼"}</span>}</span>
    </th>
  );

  const tierData = ["Strategic", "Enterprise", "Commercial"].map((tier) => {
    const ta = calAvail.filter((a) => a.tier === tier);
    return {
      tier, total: ta.length,
      Green: ta.filter((a) => a.status === "Green").length,
      Yellow: ta.filter((a) => a.status === "Yellow").length,
      Red: ta.filter((a) => a.status === "Red").length,
    };
  }).filter((d) => d.total > 0);
  // Digital tier is intentionally excluded from the Engagement-by-Tier chart — it has no RAG mandate.

  const csmData = csms.map((c) => {
    const all = accounts.filter((a) => a.csm === c);
    const ca = calAvail.filter((a) => a.csm === c);
    return {
      csm: c, total: all.length,
      Green: ca.filter((a) => a.status === "Green").length,
      Yellow: ca.filter((a) => a.status === "Yellow").length,
      Red: ca.filter((a) => a.status === "Red").length,
      Digital: all.filter((a) => a.tier === "Digital").length,
      Unknown: all.filter((a) => a.status === "Unknown").length,
      arr: all.reduce((s, a) => s + a.arr, 0),
    };
  }).sort((a, b) => b.arr - a.arr);

  const toggleCsm = (name) => setCsmFilter((cur) => (cur === name ? "All" : name));
  const toggleRow = (key) => setExpandedRow((cur) => (cur === key ? null : key));
  const colSpan = isIndividual ? 8 : 9;

  return (
    <div className="p-6 space-y-6 bg-white dark:bg-gray-900 min-h-screen">
      <div className="border-b border-gray-200 dark:border-gray-700 pb-4">
        <h1 className="text-2xl font-bold text-gray-900 dark:text-white">
          {isIndividual ? `${data.csm} — Engagement Report` : `${data.region} CS Engagement Report`}
        </h1>
        <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
          {isIndividual
            ? `${data.csm}${data.csmTitle ? ` · ${data.csmTitle}` : ""} | Region: ${data.region} | Report Date: ${data.reportDate} | Window: ${data.windowLabel}`
            : `${data.csm} | Region: ${data.region} | Report Date: ${data.reportDate} | Window: ${data.windowLabel}`}
        </p>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
        <div className="bg-gray-50 dark:bg-gray-800 rounded-xl p-4 text-center">
          <div className="text-2xl font-bold text-gray-900 dark:text-white">{totalAccounts}</div>
          <div className="text-xs text-gray-500 dark:text-gray-400 mt-1">Total Accounts</div>
        </div>
        <div className="bg-gray-50 dark:bg-gray-800 rounded-xl p-4 text-center">
          <div className="text-2xl font-bold text-gray-900 dark:text-white">{fmt(totalARR)}</div>
          <div className="text-xs text-gray-500 dark:text-gray-400 mt-1">Total ARR</div>
        </div>
        <div className="bg-emerald-50 dark:bg-emerald-900/30 rounded-xl p-4 text-center">
          <div className="text-2xl font-bold text-emerald-700 dark:text-emerald-300">{greenCount} <span className="text-sm font-normal">({Math.round(greenCount/scoredDenom*100)}%)</span></div>
          <div className="text-xs text-emerald-600 dark:text-emerald-400 mt-1">Green</div>
        </div>
        <div className="bg-yellow-50 dark:bg-yellow-900/30 rounded-xl p-4 text-center">
          <div className="text-2xl font-bold text-yellow-700 dark:text-yellow-300">{yellowCount} <span className="text-sm font-normal">({Math.round(yellowCount/scoredDenom*100)}%)</span></div>
          <div className="text-xs text-yellow-600 dark:text-yellow-400 mt-1">Yellow</div>
        </div>
        <div className="bg-red-50 dark:bg-red-900/30 rounded-xl p-4 text-center">
          <div className="text-2xl font-bold text-red-700 dark:text-red-300">{redCount} <span className="text-sm font-normal">({Math.round(redCount/scoredDenom*100)}%)</span></div>
          <div className="text-xs text-red-600 dark:text-red-400 mt-1">Red</div>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="bg-gray-50 dark:bg-gray-800 rounded-xl p-4">
          <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-3">Engagement by Tier</h3>
          <ResponsiveContainer width="100%" height={200}>
            <BarChart data={tierData} layout="vertical">
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis type="number" />
              <YAxis type="category" dataKey="tier" width={90} tick={{ fontSize: 12 }} />
              <Tooltip />
              <Legend />
              <Bar dataKey="Green" stackId="a" fill="#22c55e" />
              <Bar dataKey="Yellow" stackId="a" fill="#eab308" />
              <Bar dataKey="Red" stackId="a" fill="#ef4444" />
            </BarChart>
          </ResponsiveContainer>
        </div>
        <div className="bg-gray-50 dark:bg-gray-800 rounded-xl p-4">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300">{isIndividual ? "Book of Business" : "Book of Business by CSM"}</h3>
            {!isIndividual && csmFilter !== "All" && (
              <button onClick={() => setCsmFilter("All")} className="text-xs text-blue-700 dark:text-blue-300 hover:underline font-medium">Clear filter ({csmFilter})</button>
            )}
          </div>
          <div className="space-y-1">
            {csmData.map((c) => {
              const isSelected = csmFilter === c.csm;
              return (
                <button
                  key={c.csm}
                  type="button"
                  onClick={isIndividual ? undefined : () => toggleCsm(c.csm)}
                  disabled={isIndividual}
                  className={`w-full flex items-center justify-between text-xs gap-3 rounded-md px-2 py-1.5 text-left transition-colors ${
                    isIndividual
                      ? "cursor-default"
                      : isSelected
                        ? "bg-blue-200 dark:bg-blue-900/60 ring-1 ring-blue-500 dark:ring-blue-500"
                        : "hover:bg-gray-100 dark:hover:bg-gray-700/50"
                  }`}
                >
                  <div className="flex-1 min-w-0">
                    <div className={`font-semibold truncate ${isSelected && !isIndividual ? "text-blue-950 dark:text-white" : "text-gray-900 dark:text-gray-100"}`}>{c.csm}</div>
                    <div className={isSelected && !isIndividual ? "text-blue-900 dark:text-blue-100" : "text-gray-500 dark:text-gray-400"}>{c.total} accounts · {fmt(c.arr)} ARR</div>
                  </div>
                  <div className="flex gap-1 flex-shrink-0">
                    {c.Green > 0 && <span className="px-1.5 py-0.5 bg-emerald-100 dark:bg-emerald-900 text-emerald-800 dark:text-emerald-200 rounded">{c.Green}G</span>}
                    {c.Yellow > 0 && <span className="px-1.5 py-0.5 bg-yellow-100 dark:bg-yellow-900 text-yellow-800 dark:text-yellow-200 rounded">{c.Yellow}Y</span>}
                    {c.Red > 0 && <span className="px-1.5 py-0.5 bg-red-100 dark:bg-red-900 text-red-800 dark:text-red-200 rounded">{c.Red}R</span>}
                    {c.Digital > 0 && <span className="px-1.5 py-0.5 bg-slate-100 dark:bg-slate-800 text-slate-700 dark:text-slate-300 rounded">{c.Digital}D</span>}
                    {c.Unknown > 0 && <span className="px-1.5 py-0.5 bg-gray-200 dark:bg-gray-700 text-gray-600 dark:text-gray-400 rounded">{c.Unknown} no cal</span>}
                  </div>
                </button>
              );
            })}
          </div>
          {!isIndividual && (<div className="text-[10px] text-gray-400 dark:text-gray-500 mt-2 italic">Click a CSM to filter the table below. Click again to clear.</div>)}
        </div>
      </div>

      <div className="flex flex-wrap gap-3 items-center">
        <span className="text-xs font-medium text-gray-500 dark:text-gray-400">Filters:</span>
        <select className="text-sm border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-1.5 bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100" value={tierFilter} onChange={(e) => setTierFilter(e.target.value)}>
          <option value="All">All Tiers</option>
          <option value="Strategic">Strategic</option>
          <option value="Enterprise">Enterprise</option>
          <option value="Commercial">Commercial</option>
          <option value="Digital">Digital</option>
        </select>
        <select className="text-sm border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-1.5 bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100" value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
          <option value="All">All Status</option>
          <option value="Red">Red Only</option>
          <option value="Yellow">Yellow Only</option>
          <option value="Green">Green Only</option>
          <option value="Digital">Digital (unscored)</option>
          <option value="Unknown">No Calendar</option>
        </select>
        {!isIndividual && (
          <select className="text-sm border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-1.5 bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100" value={csmFilter} onChange={(e) => setCsmFilter(e.target.value)}>
            <option value="All">All CSMs</option>
            {csms.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        )}
        <span className="text-xs text-gray-400 dark:text-gray-500 ml-auto">{filtered.length} of {totalAccounts} accounts shown</span>
      </div>

      <div className="overflow-x-auto rounded-xl border border-gray-200 dark:border-gray-700">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700">
            <tr>
              <SortHeader field="status">Status</SortHeader>
              <SortHeader field="company">Account</SortHeader>
              {!isIndividual && <SortHeader field="csm">CSM</SortHeader>}
              <SortHeader field="tier">Tier</SortHeader>
              <SortHeader field="arr">ARR</SortHeader>
              <SortHeader field="meetingsL30D">Mtgs L30D</SortHeader>
              <th className="px-3 py-2 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">Mtgs L60D</th>
              <SortHeader field="daysSinceLast">Days Since</SortHeader>
              <th className="px-3 py-2 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">Last Meeting</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100 dark:divide-gray-800">
            {sorted.map((a) => {
              const isExpanded = expandedRow === a.company;
              const meetings = a.meetings || [];
              const canExpand = !a.calendarUnavailable;
              return (
                <React.Fragment key={a.company}>
                  <tr
                    onClick={canExpand ? () => toggleRow(a.company) : undefined}
                    className={`${canExpand ? "cursor-pointer" : ""} ${isExpanded ? "bg-blue-200 dark:bg-blue-900/60 ring-1 ring-blue-500 dark:ring-blue-500" : "hover:bg-gray-50 dark:hover:bg-gray-800"}`}
                  >
                    <td className="px-3 py-2.5">
                      <div className="flex items-center gap-2">
                        {canExpand && (<span className={`text-xs transition-transform ${isExpanded ? "rotate-90 text-blue-900 dark:text-blue-100" : "text-gray-400 dark:text-gray-500"}`}>▶</span>)}
                        <StatusBadge status={a.status} />
                      </div>
                    </td>
                    <td className={`px-3 py-2.5 font-semibold ${isExpanded ? "text-blue-950 dark:text-white" : "font-medium text-gray-900 dark:text-white"}`}>{a.company}</td>
                    {!isIndividual && <td className={`px-3 py-2.5 text-xs ${isExpanded ? "text-blue-900 dark:text-blue-100" : "text-gray-600 dark:text-gray-400"}`}>{a.csm}</td>}
                    <td className="px-3 py-2.5">
                      <span className={`text-xs px-2 py-0.5 rounded ${a.tier === "Strategic" ? "bg-purple-100 text-purple-800 dark:bg-purple-900 dark:text-purple-200" : a.tier === "Enterprise" ? "bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200" : a.tier === "Digital" ? "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300" : "bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-200"}`}>{a.tier}</span>
                    </td>
                    <td className={`px-3 py-2.5 font-mono ${isExpanded ? "text-blue-950 dark:text-white" : "text-gray-900 dark:text-white"}`}>{fmt(a.arr)}</td>
                    <td className="px-3 py-2.5">
                      {a.calendarUnavailable ? <span className="text-gray-400 dark:text-gray-500 italic text-xs">no cal</span> : a.tier === "Digital" ? (
                        <span className={`text-gray-700 dark:text-gray-300`}>{a.meetingsL30D}</span>
                      ) : (
                        <>
                          <span className={`font-bold ${a.meetingsL30D >= a.thresholdL30D ? "text-emerald-700 dark:text-emerald-300" : a.meetingsL30D > 0 ? "text-yellow-700 dark:text-yellow-300" : "text-red-700 dark:text-red-300"}`}>{a.meetingsL30D}</span>
                          <span className={`text-xs ml-1 ${isExpanded ? "text-blue-900 dark:text-blue-100" : "text-gray-400 dark:text-gray-500"}`}>/ {a.thresholdL30D}</span>
                        </>
                      )}
                    </td>
                    <td className={`px-3 py-2.5 ${isExpanded ? "text-blue-950 dark:text-white font-medium" : "text-gray-700 dark:text-gray-300"}`}>{a.calendarUnavailable ? "—" : a.meetingsL60D}</td>
                    <td className="px-3 py-2.5">
                      {a.calendarUnavailable ? <span className="text-gray-400 dark:text-gray-500">—</span> : a.daysSinceLast >= 999 ? <span className={`font-mono ${a.tier === "Digital" ? "text-gray-500 dark:text-gray-400" : "text-red-700 dark:text-red-300"}`}>{a.tier === "Digital" ? "—" : "never"}</span> : (
                        <span className={`font-mono ${a.tier === "Digital" ? "text-gray-700 dark:text-gray-300" : a.daysSinceLast > 30 ? "text-red-700 dark:text-red-300" : a.daysSinceLast > 14 ? "text-yellow-700 dark:text-yellow-300" : isExpanded ? "text-blue-950 dark:text-white" : "text-gray-700 dark:text-gray-300"}`}>{a.daysSinceLast}d</span>
                      )}
                    </td>
                    <td className={`px-3 py-2.5 ${isExpanded ? "text-blue-900 dark:text-blue-100" : "text-gray-600 dark:text-gray-400"}`}>{a.lastMeeting}</td>
                  </tr>
                  {isExpanded && (
                    <tr className="bg-blue-50/40 dark:bg-blue-900/10">
                      <td colSpan={colSpan} className="px-6 py-4">
                        {meetings.length === 0 ? (
                          <div className="text-xs text-gray-500 dark:text-gray-400 italic">No meetings logged in the {data.windowLabel || "L60D"} window.</div>
                        ) : (
                          <div className="space-y-3">
                            <div className="text-xs font-semibold text-gray-700 dark:text-gray-300 uppercase tracking-wide">{meetings.length} meeting{meetings.length === 1 ? "" : "s"} · most recent first</div>
                            {meetings.map((m) => (
                              <div key={m.id} className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg p-3">
                                <div className="flex items-start justify-between gap-3 mb-1.5">
                                  <div className="font-semibold text-sm text-gray-900 dark:text-white">{m.title}</div>
                                  <div className="text-xs text-gray-500 dark:text-gray-400 whitespace-nowrap">{m.displayStart}</div>
                                </div>
                                {m.organizer && (<div className="text-xs text-gray-500 dark:text-gray-400 mb-2">Organizer: <span className="font-mono">{m.organizer}</span></div>)}
                                {m.attendees && m.attendees.length > 0 && (
                                  <div>
                                    <div className="text-[11px] font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-1">Invitees ({m.attendees.length})</div>
                                    <div className="grid grid-cols-1 md:grid-cols-2 gap-x-4 gap-y-1">
                                      {m.attendees.map((at, i) => (
                                        <div key={i} className="flex items-center gap-2 text-xs">
                                          <AttendeeStatusDot status={at.status} />
                                          <span className={`font-medium ${at.internal ? "text-gray-700 dark:text-gray-300" : "text-gray-900 dark:text-white"}`}>{at.name}</span>
                                          {at.internal && (<span className="text-[10px] px-1 py-0.5 bg-gray-100 dark:bg-gray-700 text-gray-500 dark:text-gray-400 rounded">TD</span>)}
                                          <span className="text-gray-400 dark:text-gray-500 truncate">{at.email}</span>
                                        </div>
                                      ))}
                                    </div>
                                    <div className="flex items-center gap-3 mt-2 text-[10px] text-gray-400 dark:text-gray-500">
                                      <span className="flex items-center gap-1"><AttendeeStatusDot status="accepted" /> Accepted</span>
                                      <span className="flex items-center gap-1"><AttendeeStatusDot status="tentative" /> Tentative</span>
                                      <span className="flex items-center gap-1"><AttendeeStatusDot status="declined" /> Declined</span>
                                      <span className="flex items-center gap-1"><AttendeeStatusDot status="needsAction" /> No response</span>
                                    </div>
                                  </div>
                                )}
                              </div>
                            ))}
                          </div>
                        )}
                      </td>
                    </tr>
                  )}
                </React.Fragment>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="bg-gray-50 dark:bg-gray-800 rounded-xl p-4">
        <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-2">Scoring Thresholds</h3>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4 text-xs">
          {Object.entries(data.tierThresholds).map(([tier, t]) => (
            <div key={tier} className="space-y-1">
              <div className="font-semibold text-gray-800 dark:text-gray-200">{tier} <span className="font-normal text-gray-500">({t.label})</span></div>
              {t.unscored ? (
                <div className="text-slate-600 dark:text-slate-400">Unscored — no engagement mandate. Meetings logged for reference only.</div>
              ) : (
                <>
                  <div className="text-emerald-600 dark:text-emerald-400">Green: ≥{t.greenL30D} mtgs in L30D</div>
                  <div className="text-yellow-600 dark:text-yellow-400">Yellow: ≥{t.yellowL60D} mtgs in L60D (but below L30D threshold)</div>
                  <div className="text-red-600 dark:text-red-400">Red: &lt;{t.yellowL60D} mtgs in L60D</div>
                </>
              )}
            </div>
          ))}
        </div>
      </div>

      {data.excludedAccounts && data.excludedAccounts.length > 0 && (
        <div className="bg-gray-50 dark:bg-gray-800 rounded-xl p-4">
          <button
            type="button"
            onClick={() => setExcludedOpen((v) => !v)}
            className="w-full flex items-center justify-between text-left"
          >
            <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300">
              {data.excludedAccounts.length} account{data.excludedAccounts.length === 1 ? "" : "s"} filtered out
              {typeof data.minArrApplied === "number" && (
                <span className="font-normal text-gray-500 dark:text-gray-400"> (below {fmt(data.minArrApplied)} ARR)</span>
              )}
            </h3>
            <span className={`text-xs text-gray-400 transition-transform ${excludedOpen ? "rotate-90" : ""}`}>▶</span>
          </button>
          {excludedOpen && (
            <div className="mt-3 overflow-x-auto">
              <table className="w-full text-xs">
                <thead className="text-gray-500 dark:text-gray-400">
                  <tr>
                    <th className="px-2 py-1 text-left font-medium uppercase">Account</th>
                    {!isIndividual && <th className="px-2 py-1 text-left font-medium uppercase">CSM</th>}
                    <th className="px-2 py-1 text-left font-medium uppercase">ARR</th>
                    <th className="px-2 py-1 text-left font-medium uppercase">Reason</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100 dark:divide-gray-800">
                  {data.excludedAccounts.map((x, i) => (
                    <tr key={i}>
                      <td className="px-2 py-1 text-gray-900 dark:text-gray-100">{x.company}</td>
                      {!isIndividual && <td className="px-2 py-1 text-gray-600 dark:text-gray-400">{x.csm}</td>}
                      <td className="px-2 py-1 font-mono text-gray-700 dark:text-gray-300">{fmt(x.arr || 0)}</td>
                      <td className="px-2 py-1 text-gray-500 dark:text-gray-400">{x.reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className="text-[10px] text-gray-400 dark:text-gray-500 mt-2 italic">To include these in the next report, ask: "include zero-ARR accounts" or "include all accounts".</div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
```

---

## Individual Roundup Template

The **same React component** handles both Regional Roundups and Individual Roundups. The component branches off `data.individualReport` — set it to `true` for individual reports. **Use the component code verbatim; do not modify layout, copy, classes, or interactions. Only the `data` payload changes between report modes.**

When `data.individualReport === true`, the component automatically:
- Renders the header as `{data.csm} — Engagement Report` instead of `{data.region} CS Engagement Report`.
- Adds the CSM's title to the sub-line if `data.csmTitle` is provided: `{csm} · {csmTitle} | Region: ... | Report Date: ... | Window: ...`.
- Hides the CSM filter dropdown (only one CSM in scope).
- Hides the CSM column in the scorecard table.
- Hides the "Click a CSM to filter" hint and the "Clear filter" link.
- Renders the Book of Business panel as a single non-clickable row (label changes from "Book of Business by CSM" to "Book of Business").

**Required `data` shape for individual reports:**

```json
{
  "csm": "Alex Starr",
  "csmTitle": "Senior Customer Success Manager",
  "region": "Americas",
  "reportDate": "2026-05-15",
  "windowLabel": "Mar 16 – May 15, 2026 (L60D)",
  "individualReport": true,
  "tierThresholds": { /* same as regional */ },
  "accounts": [
    {
      "company": "...",
      "domain": "...",
      "csm": "Alex Starr",
      "csmEmail": "alex.starr@treasure-data.com",
      "tier": "Strategic | Enterprise | Commercial | Digital",
      "region": "Americas",
      "arr": 1234567,
      "meetingsL30D": 3,
      "meetingsL60D": 8,
      "thresholdL30D": 3,
      "thresholdL60D": 5,
      "lastMeeting": "YYYY-MM-DD",
      "daysSinceLast": 8,
      "calendarUnavailable": false,
      "meetings": [
        {
          "id": "...",
          "title": "TD x UMG Bi-Weekly Sync",
          "start": "2026-05-06T15:30:00-04:00",
          "displayStart": "Wed, May 6, 2026 · 3:30 PM UTC-04:00",
          "organizer": "alex.starr@treasure.ai",
          "attendees": [
            { "email": "...", "name": "...", "status": "accepted | declined | tentative | needsAction", "internal": true }
          ]
        }
      ]
    }
  ],
  "minArrApplied": 1,
  "excludedAccounts": [
    { "company": "Some Stale Account", "csm": "Alex Starr", "arr": 0, "reason": "stale_ownership" }
  ]
}
```

**`minArrApplied` and `excludedAccounts` are required for individual reports too** (same shape as regional). The footer renders identically — collapsed by default, click to expand, lists company / ARR / reason. CSM column is hidden in individual mode (only one CSM in scope).

**Click-to-expand meeting details (always-on, both report modes):** every account row in the scorecard is clickable. Clicking expands an inline detail panel below the row showing all meetings in `account.meetings` (sorted most-recent-first). Each meeting card displays:
- Event title (bold)
- Event date and time (`displayStart`)
- Organizer email
- Invite list with each attendee's name, email, internal/external "TD" badge, and a colored RSVP dot (green=accepted, yellow=tentative, red=declined, gray=no response)

The `meetings` array is required when `calendarUnavailable !== true`. If a CSM's calendar wasn't accessible, set `calendarUnavailable: true` and omit (or empty) `meetings` — the component automatically suppresses the expand affordance for that row.

All "explicitly NOT included" rules from the regional template apply (no alert banners, no calendar-coverage note, no pie chart, no deactivated CSM list).

## Edge Cases

- **Deactivated CSMs**: If `deleted: true`, exclude from active team reports. Flag any accounts still assigned to deactivated users as "Orphaned — needs reassignment."
- **CSM has no external meetings**: Report this clearly — "0 external meetings found in L60D. Verify calendar access."
- **Attendee not in HubSpot contacts**: Try the narrow domain fallback (Step 4); if the domain is ambiguous in the CSM's book, drop the meeting and emit the email to the `domains_with_no_contact_match` audit list for sales-ops follow-up. Never collapse multiple companies onto the same domain.
- **Shared corporate domain across multiple HubSpot companies** (e.g. `nestle.com`, `honda.com`, `jnj.com`): Always resolve via `associatedcompanyid`. Domain-only fallback is forbidden in this case — log to the audit list instead.
- **Contact has multiple company associations**: Use the primary association. If the primary is not in the CSM's book but a secondary association is, count it for the secondary (rare — log it).
- **Duplicate meetings**: Deduplicate by event ID before counting.
- **Team rollup with overlapping accounts**: Attribute to the HubSpot company owner (`hubspot_owner_id`).
- **Region is null**: Display as "Unassigned" — do not exclude.
- **CSM calendar not accessible**: Note which CSMs could not be checked.

## Examples

**Input:** "Americas team rollup" / "EMEA engagement" / "APAC roundup"
→ **Regional Roundup Template**, scoped to that region's CSMs.

**Input:** "Run an engagement report for Alex Starr" / "{name}'s book of business"
→ **Individual Roundup Template**, scoped to that CSM only.

**Input:** "Show me which accounts over $1M haven't been met in 30 days"
→ Either template works; pre-filter to ARR > $1M and `meetingsL30D === 0`.