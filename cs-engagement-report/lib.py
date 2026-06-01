"""Deterministic decision rules for cs-engagement-report.

This module is the single source of truth for the pure-logic rules in the
cs-engagement-report skill. The skill's SKILL.md prose explains the rules in
narrative form; this file implements them. When the rules change, update this
file AND the prose in SKILL.md together — the cs-engagement-report-redteam
skill imports these functions directly to test them, so drift between the two
will be caught by red-team runs.

All functions here are pure: no I/O, no MCP calls, no network. Inputs are
plain dicts/lists; outputs are plain dicts/lists/strings/numbers. This is what
makes them testable in isolation by the red-team skill.

Event objects passed to attribute_event_to_company use the Google Calendar v3
events.list resource shape verbatim (summary, attendees[].email,
attendees[].responseStatus, organizer.email, organizer.self, status,
recurringEventId, eventType). Do not invent abbreviated field names — the
real skill consumes raw GCal payloads and any rename here would mask bugs.
"""

from __future__ import annotations

from collections import Counter


INTERNAL_DOMAINS = frozenset({
    "treasuredata.com",
    "treasure-data.com",
    "treasure.ai",
})

SYSTEM_DOMAINS = frozenset({
    "resource.calendar.google.com",
    "group.calendar.google.com",
    "calendar.google.com",
})

PARTNER_DOMAINS = frozenset({
    "liveramp.com",
    "valtech.com",
    "acxiom.com",
    "publicismedia.com",
    "starcomww.com",
    "lakefrontdatasolutions.com",
    "partner.fluidra.com",
})

VALID_REGIONS = frozenset({
    "Americas",
    "EMEA",
    "Japan",
    "ASIAexJapan",
    "APAC",
})

# Deterministic tie-break order when two regions have the same majority count.
# Documented in csm-region-mapping.yaml steps[3].tie_break and SKILL.md
# "Region aggregation". Earlier entries win ties.
TIE_BREAK_ORDER = ("Americas", "EMEA", "Japan", "ASIAexJapan", "APAC")

DROP_AMBIGUOUS = "DROP_AMBIGUOUS"
AMBIGUOUS = "AMBIGUOUS"  # multiple distinct contact records share an email
MATCHED_BUT_NO_COMPANY = "MATCHED_BUT_NO_COMPANY"  # contact exists but has no company

SHARD_CAP = 100  # hubspot_search_crm_objects MCP cap per response

# Single-token csm_email local-parts that are legitimate despite missing a ".".
# Anything else with no "." in the local part is a data error (a display-name
# string, a partner email, etc.) and gets dropped during normalization.
SINGLE_TOKEN_ALLOWLIST = frozenset({
    "valencia",
    "ino",
    "tsukahara",
    "sekikawa",
})

TD_DOMAINS = frozenset({
    "treasure-data.com",
    "treasure.ai",
    "treasuredata.com",
})


def tier_from_arr(arr):
    """Classify an account into a tier based on closed-won ARR.

    Rule (SKILL.md Step 6):
        Strategic   > $1,000,000
        Enterprise  $250,000 - $1,000,000  (inclusive of both bounds)
        Commercial  $100,000 - $250,000    (inclusive lower, exclusive upper)
        Digital     < $100,000              (unscored — no engagement mandate)
        Unknown ARR -> Commercial (and caller should flag for review)

    Negative or zero ARR is floored to 0 per known_pitfalls in
    csm-region-mapping.yaml (cancelled deals, unreconciled credits).
    A floored value of 0 lands in Digital (no engagement mandate), which
    matches the intent — a $0-ARR account has no contractual basis for a
    cadence requirement.
    """
    if arr is None:
        return "Commercial"
    arr = max(float(arr), 0.0)
    if arr > 1_000_000:
        return "Strategic"
    if arr >= 250_000:
        return "Enterprise"
    if arr >= 100_000:
        return "Commercial"
    return "Digital"


def score_account(meetings_l30d, meetings_l60d, tier, *, calendar_unavailable=False):
    """Score an account Green / Yellow / Red / Digital / Unknown.

    Rule (SKILL.md Step 7):
        Strategic:   Green >=3 L30D | Yellow >=5 L60D (but <3 L30D) | Red <5 L60D
        Enterprise:  Green >=2 L30D | Yellow >=3 L60D (but <2 L30D) | Red <3 L60D
        Commercial:  Green >=1 L30D | Yellow >=1 L60D (but 0 L30D)  | Red <1 L60D
        Digital:     Unscored. Always returns "Digital" — meeting counts may be
                     non-zero but the account has no engagement mandate.

    Tier check happens BEFORE calendar_unavailable: a Digital account renders
    as "Digital" regardless of calendar access, since calendar gaps are
    irrelevant when there is no scoring rule. For scored tiers,
    calendar_unavailable returns "Unknown".
    """
    if tier == "Digital":
        return "Digital"
    if calendar_unavailable:
        return "Unknown"
    thresholds = {
        "Strategic":  (3, 5),
        "Enterprise": (2, 3),
        "Commercial": (1, 1),
    }
    if tier not in thresholds:
        return "Red"
    green_l30d, yellow_l60d = thresholds[tier]
    if meetings_l30d >= green_l30d:
        return "Green"
    if meetings_l60d >= yellow_l60d:
        return "Yellow"
    return "Red"


def resolve_csm_region(accounts):
    """Compute a CSM's primary region by majority rule across their accounts.

    Rule (SKILL.md "CSM Discovery", csm-region-mapping.yaml steps[3]+[4]):
        - Count td_company_owner_region values across the CSM's accounts.
        - Primary region = majority (most common).
        - cross_region = True if minority is >=20% AND >=2 accounts.
        - Confidence: high if n_accounts >= 5, medium if 2-4, low if 1.
        - Floor negative arr_won_all_rollup values to 0 when summing.

    Inputs: list of {region, arr} dicts. Region is a value from VALID_REGIONS
    or None. If all accounts have null region, primary is None.

    Output: {region, cross_region, breakdown, n_accounts, arr_total,
             confidence, tied_majority}
    Determinism: when two regions tie for first place, the tie-break order is
    Americas > EMEA > Japan > ASIAexJapan > APAC (TIE_BREAK_ORDER constant).
    The tied list is preserved in `tied_majority` so a reviewer can audit which
    CSMs landed on a tie-broken assignment.
    """
    n_accounts = len(accounts)
    arr_total = sum(max(float(a.get("arr") or 0.0), 0.0) for a in accounts)

    region_counter = Counter()
    for a in accounts:
        region_counter[a.get("region")] += 1
    breakdown = dict(region_counter)

    valid_regions_only = {r: c for r, c in region_counter.items() if r in VALID_REGIONS}
    if valid_regions_only:
        max_count = max(valid_regions_only.values())
        # Deterministic tie-break: among regions tied at max_count, pick the
        # earliest in TIE_BREAK_ORDER. Guarantees identical input lists produce
        # identical csm_region output regardless of iteration order.
        tied = [r for r, c in valid_regions_only.items() if c == max_count]
        primary = sorted(tied, key=lambda r: TIE_BREAK_ORDER.index(r))[0]
        tied_majority = tied if len(tied) > 1 else None
    else:
        primary = None
        tied_majority = None

    # cross_region is computed from VALID regions only. Null/garbage region
    # values are missing-data noise, not real cross-region work — including
    # them inflates the minority count and silently flags single-region CSMs.
    cross_region = False
    if primary is not None:
        valid_total = sum(valid_regions_only.values())
        primary_count = valid_regions_only[primary]
        minority_count = valid_total - primary_count
        if valid_total > 0:
            minority_pct = minority_count / valid_total
            if minority_count >= 2 and minority_pct >= 0.20:
                cross_region = True

    if n_accounts >= 5:
        confidence = "high"
    elif n_accounts >= 2:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "region": primary,
        "cross_region": cross_region,
        "breakdown": breakdown,
        "n_accounts": n_accounts,
        "arr_total": arr_total,
        "confidence": confidence,
        "tied_majority": tied_majority,
    }


def _email_domain(email):
    return email.rsplit("@", 1)[-1].lower() if "@" in email else ""


def _filter_external_attendees(attendees):
    """Drop internal TD attendees, system-calendar entries, and resource rows.

    Mirrors SKILL.md Step 3 + Step 4.1: external means email domain is not in
    INTERNAL_DOMAINS and not in SYSTEM_DOMAINS, and the attendee is not a
    resource (room) entry. Matches the real GCal attendee shape including
    the `self`, `resource`, and `responseStatus` fields.
    """
    out = []
    for a in attendees or []:
        if a.get("resource"):
            continue
        email = (a.get("email") or "").lower()
        domain = _email_domain(email)
        if not domain:
            continue
        if domain in INTERNAL_DOMAINS:
            continue
        if domain in SYSTEM_DOMAINS:
            continue
        out.append(a)
    return out


def disambiguate_domain(domain, csm_book):
    """Map a domain to a single company in the CSM's book, or DROP_AMBIGUOUS.

    Rule (SKILL.md Step 4 "Domain-only fallback"):
        - If exactly one company in the book has this domain, return [hs_object_id].
        - If multiple companies in the book share the domain, return DROP_AMBIGUOUS.
        - If no company in the book has this domain, return [].

    csm_book entries: {hs_object_id, domain, ...}. Comparison is case-
    insensitive on the full domain string. Subdomain handling is intentionally
    strict — nestle.com and nestle-mexico.com are distinct domains by design.
    """
    domain = (domain or "").strip().lower()
    if not domain:
        return []
    matches = [c["hs_object_id"] for c in csm_book if (c.get("domain") or "").strip().lower() == domain]
    if len(matches) == 0:
        return []
    if len(matches) == 1:
        return matches
    return DROP_AMBIGUOUS


def attribute_event_to_company(event, *, csm_email, hubspot_contacts, csm_book):
    """Attribute a GCal event to one or more HubSpot companies in the CSM's book.

    Rule (SKILL.md Step 3 + Step 4 — contact-first attribution):
        1. The event must be active (status != "cancelled").
        2. The CSM must have either accepted the event or be the organizer.
           - Organizer: organizer.email matches csm_email OR organizer.self == True.
           - Accepted: the CSM's own attendee row has responseStatus == "accepted".
        3. Build the external attendee list: drop internal TD, system, and
           resource attendees.
        4. Drop attendees whose domain is in PARTNER_DOMAINS.
        5. For each remaining attendee, look up hubspot_contacts[email] and
           read associatedcompanyid. If that id is in csm_book, the event is
           attributed to it.
        6. If multiple distinct companies in the book are matched on one event,
           emit one attribution per company (joint meetings count for both).
        7. If zero attendees resolve to a company in the book, fall back to
           domain disambiguation — but ONLY if every external attendee has no
           HubSpot contact at all. If any attendee did match a contact (just
           not one in the book), do not fall back.
        8. The domain fallback uses disambiguate_domain. If it returns
           DROP_AMBIGUOUS, drop the event and add the attendee email to the
           audit list.

    Returns:
        {
            "company_ids": [hs_object_id, ...],   # may be empty
            "audit_emails": [email, ...],         # attendees with no contact match
            "dropped": bool,
            "reason": "cancelled" | "non_engagement_event" | "not_accepted" |
                      "no_external" | "ambiguous_domain" | "no_match" | None,
        }

    csm_email comparison is case-insensitive. hubspot_contacts is keyed by
    lowercased email; values are {associatedcompanyid, ...}.
    """
    csm_email = (csm_email or "").lower()
    book_ids = {c["hs_object_id"] for c in csm_book}

    if event.get("status") == "cancelled":
        return {"company_ids": [], "audit_emails": [], "dropped": True, "reason": "cancelled"}

    # eventType filter: outOfOffice and focusTime are personal calendar blocks,
    # not real customer engagement. Default and workingLocation events are
    # eligible. Treat unknown/missing eventType as "default" (eligible) so
    # legacy events without the field continue to attribute correctly.
    if event.get("eventType") in ("outOfOffice", "focusTime"):
        return {"company_ids": [], "audit_emails": [], "dropped": True, "reason": "non_engagement_event"}

    organizer = event.get("organizer") or {}
    organizer_email = (organizer.get("email") or "").lower()
    is_organizer = organizer.get("self") is True or organizer_email == csm_email

    csm_attendee_row = None
    for a in event.get("attendees") or []:
        a_email = (a.get("email") or "").lower()
        if a.get("self") is True or a_email == csm_email:
            csm_attendee_row = a
            break
    csm_accepted = bool(csm_attendee_row and csm_attendee_row.get("responseStatus") == "accepted")

    if not (is_organizer or csm_accepted):
        return {"company_ids": [], "audit_emails": [], "dropped": True, "reason": "not_accepted"}

    external = _filter_external_attendees(event.get("attendees") or [])
    external = [a for a in external if _email_domain((a.get("email") or "").lower()) not in PARTNER_DOMAINS]

    if not external:
        return {"company_ids": [], "audit_emails": [], "dropped": True, "reason": "no_external"}

    matched_company_ids = set()
    audit_emails = []
    any_attendee_had_contact = False

    for a in external:
        email = (a.get("email") or "").lower()
        contact = hubspot_contacts.get(email)
        if contact is None:
            audit_emails.append(email)
            continue
        any_attendee_had_contact = True
        company_id = contact.get("associatedcompanyid")
        if company_id and company_id in book_ids:
            matched_company_ids.add(company_id)

    if matched_company_ids:
        return {
            "company_ids": sorted(matched_company_ids),
            "audit_emails": audit_emails,
            "dropped": False,
            "reason": None,
        }

    if any_attendee_had_contact:
        return {"company_ids": [], "audit_emails": audit_emails, "dropped": True, "reason": "no_match"}

    fallback_matches = set()
    fallback_ambiguous = False
    for a in external:
        domain = _email_domain((a.get("email") or "").lower())
        result = disambiguate_domain(domain, csm_book)
        if result == DROP_AMBIGUOUS:
            fallback_ambiguous = True
            break
        if isinstance(result, list) and result:
            fallback_matches.update(result)

    if fallback_ambiguous:
        return {"company_ids": [], "audit_emails": audit_emails, "dropped": True, "reason": "ambiguous_domain"}
    if fallback_matches:
        return {
            "company_ids": sorted(fallback_matches),
            "audit_emails": audit_emails,
            "dropped": False,
            "reason": None,
        }
    return {"company_ids": [], "audit_emails": audit_emails, "dropped": True, "reason": "no_match"}


def lookup_contact(attendee_email, hubspot_contacts_table, *, csm_book=None):
    """Resolve a calendar attendee email to a Hubspot contact's company id.

    This is step 1 of contact-first attribution: turn an attendee email into
    a company id (or a sentinel if the lookup is inconclusive). The caller
    then checks whether that company is in the CSM's book.

    Inputs:
        attendee_email           — the email as it appears on the calendar
                                   invite (any case, may have plus-tag).
        hubspot_contacts_table   — list of contact dicts. Each may carry:
                                     email, hs_additional_emails (list),
                                     associatedcompanyid, company_associations
                                     (list of {id, primary}).
        csm_book                 — optional list of {hs_object_id, ...} for
                                   the CSM's book. Used only as a tiebreaker
                                   when multiple matches exist.

    Returns one of:
        {"associatedcompanyid": <id>}             single resolution
        {"associatedcompanyid": AMBIGUOUS,
         "candidates": [<id>, ...]}                multiple distinct cards/companies
        {"associatedcompanyid": MATCHED_BUT_NO_COMPANY}  contact exists, no company
        None                                       no contact match at all

    Documented contract decisions (revisit if real-world cases prove these wrong):

        1. Case-fold both sides before comparing — comparison is fully
           case-insensitive across the email, including the local part.
           ``Buyer@Nestle.com`` matches ``buyer@nestle.com`` matches
           ``BUYER@NESTLE.COM``. Casing is the only normalization; no
           whitespace stripping beyond an outer ``.strip()``, no unicode
           folding, no plus-tag handling here (see rule 2).
        2. Plus-tags are NOT stripped. ``buyer+td@nestle.com`` != ``buyer@nestle.com``.
           Reason: plus-tags are user-controlled; auto-stripping risks cross-
           attribution between distinct contacts who share a base address.
        3. Match against ``email`` AND ``hs_additional_emails``. Hubspot stores
           secondary emails on the additional-emails field; both are valid.
        4. Multiple cards for the same email -> prefer a card whose
           associatedcompanyid is in csm_book; if none qualify, return
           AMBIGUOUS with the candidate ids so the caller can decide.
        5. Multiple company associations on one contact (``company_associations``
           list with multiple entries) -> prefer one whose id is in csm_book;
           else use the entry with ``primary: true``; else the first entry.
           Falls through to ``associatedcompanyid`` if ``company_associations``
           is absent.
        6. Card with no company at all (no associatedcompanyid AND no
           non-empty company_associations) -> return MATCHED_BUT_NO_COMPANY.
           Do NOT return None — a contact existed; the caller must NOT fall
           back to domain disambiguation in this case.
    """
    if not attendee_email or "@" not in attendee_email:
        return None
    target = attendee_email.strip().lower()
    book_ids = {c["hs_object_id"] for c in (csm_book or [])}

    matches = []
    for contact in hubspot_contacts_table or []:
        emails = []
        primary = (contact.get("email") or "").strip().lower()
        if primary:
            emails.append(primary)
        for e in contact.get("hs_additional_emails") or []:
            e = (e or "").strip().lower()
            if e:
                emails.append(e)
        if target in emails:
            matches.append(contact)

    if not matches:
        return None

    def resolve_company(contact):
        """Pick a single company id off one contact record per rule 5."""
        associations = contact.get("company_associations")
        if associations:
            in_book = [a for a in associations if a.get("id") in book_ids]
            if in_book:
                return in_book[0].get("id")
            primaries = [a for a in associations if a.get("primary") is True]
            if primaries:
                return primaries[0].get("id")
            return associations[0].get("id")
        cid = contact.get("associatedcompanyid")
        return cid or None

    candidate_ids = []
    for contact in matches:
        cid = resolve_company(contact)
        if cid is not None:
            candidate_ids.append(cid)

    distinct_ids = []
    for cid in candidate_ids:
        if cid not in distinct_ids:
            distinct_ids.append(cid)

    if not distinct_ids:
        # All matched contacts have no company at all
        return {"associatedcompanyid": MATCHED_BUT_NO_COMPANY}

    if len(distinct_ids) == 1:
        return {"associatedcompanyid": distinct_ids[0]}

    # Multiple distinct companies — apply book tiebreaker
    in_book = [cid for cid in distinct_ids if cid in book_ids]
    if len(in_book) == 1:
        return {"associatedcompanyid": in_book[0]}

    return {"associatedcompanyid": AMBIGUOUS, "candidates": distinct_ids}


# ---------------------------------------------------------------------------
# Determinism layer: book-of-business assembly
#
# These functions exist because the rendered "Total Accounts" KPI fluctuates
# between runs of the same regional report. The skill's Step 1 fans out
# sharded HubSpot searches and aggregates the results into a per-account
# table. That table is the input to every downstream count. Any silent drop,
# silent dedupe miss, or rule ambiguity in the aggregation step shows up as a
# different KPI value on the next run.
#
# By pulling the assembly into pure functions here, the redteam skill can
# pin behavior with synthetic fixtures, AND a separate observational harness
# can run the real pipeline 3 times back-to-back and diff the inputs to
# build_regional_book — narrowing fluctuation to either (a) HubSpot mutated
# between runs, or (b) the shard fan-out is non-deterministic.
# ---------------------------------------------------------------------------


SHARD_OVERFLOW = "SHARD_OVERFLOW"


def normalize_csm_email(csm_email):
    """Return the lowercased local part if csm_email is a real TD email, else None.

    Mirrors the SKILL.md "bogus csm_email" filter (Step 1 / known_pitfalls):
        - Require an "@" with a TD domain on the right side.
        - Require a "." in the local part, OR a local part in
          SINGLE_TOKEN_ALLOWLIST.
        - Strip whitespace and case-fold before checking.

    Names like "Hiroki Ito" (no "@"), partner emails like "jamie@origin63.com"
    (non-TD domain), and stray strings all return None.
    """
    if not csm_email:
        return None
    s = str(csm_email).strip().lower()
    if "@" not in s:
        return None
    local, _, domain = s.rpartition("@")
    if domain not in TD_DOMAINS:
        return None
    if "." not in local and local not in SINGLE_TOKEN_ALLOWLIST:
        return None
    return local


def detect_shard_overflow(shard_responses):
    """Return a list of shard names whose response hit the 100-record cap.

    shard_responses: list of {name, count, ...} dicts. Any shard at the cap
    has silently truncated and the run cannot produce a deterministic count
    until the shard is subdivided. Callers should fail-fast on a non-empty
    return.

    SKILL.md csm-region-mapping.yaml inputs.primary.sharding says "if any
    shard returns count == 100 in a future run, subdivide it." This makes
    that check explicit instead of leaving it to the agent's discretion.
    """
    return [s.get("name", "?") for s in (shard_responses or []) if (s.get("count") or 0) >= SHARD_CAP]


def dedupe_companies(records):
    """Dedupe HubSpot company records by hs_object_id.

    Sharded fan-out can return the same record more than once when shard
    boundaries overlap. Dedupe is by hs_object_id only — the same company
    cannot legitimately have two distinct ids. Order is preserved (first
    occurrence wins) so the function is deterministic regardless of how
    shards arrived.
    """
    seen = set()
    out = []
    for r in records or []:
        rid = r.get("hs_object_id")
        if rid is None or rid in seen:
            continue
        seen.add(rid)
        out.append(r)
    return out


def build_regional_book(
    company_records,
    *,
    region,
    min_arr=1,
    cross_region_policy="csm_scoped",
):
    """Assemble the scorecard book for a regional rollup.

    This is the single pure function that produces the inputs to the
    rendered "Total Accounts" KPI. Given:

        company_records       - deduped HubSpot company rows. Each carries:
                                  hs_object_id, name, csm_email,
                                  td_company_owner_region, arr_won_all_rollup,
                                  domain.
        region                - target region for the rollup. Must be a
                                value in VALID_REGIONS.
        min_arr               - threshold from Step 0 (default 1).
        cross_region_policy   - how to handle a CSM whose primary region is
                                the target but who has accounts outside it,
                                AND a CSM whose primary region is NOT the
                                target but who has accounts inside it.

                                "csm_scoped"     (default): include EVERY
                                  account belonging to a CSM whose primary
                                  region is the target. Excludes accounts
                                  belonging to CSMs whose primary is
                                  elsewhere, even if the account itself sits
                                  in the target region.
                                "account_scoped": include only accounts
                                  whose td_company_owner_region equals the
                                  target, regardless of CSM primary region.

                                Document the choice on the rendered report so
                                two runs with different policies do not look
                                identical from the outside.

    Returns:
        {
            "region": region,
            "policy": cross_region_policy,
            "min_arr": min_arr,
            "scorecard_accounts": [...],   # rows that count toward Total Accounts
            "excluded_accounts":  [...],   # rows below min_arr (footer)
            "dropped_bogus_csm":  [...],   # csm_email failed normalize
            "matrix": {csm_local: resolve_csm_region(...)},
            "n_total": int,                # len(scorecard_accounts)
        }

    Determinism contract: given the same input list (in any order), the
    function returns the same scorecard_accounts set, ordered by
    hs_object_id ascending. Run it twice on the same input and the output
    is byte-identical.
    """
    if region not in VALID_REGIONS:
        raise ValueError(f"region must be one of {sorted(VALID_REGIONS)}, got {region!r}")
    if cross_region_policy not in ("csm_scoped", "account_scoped"):
        raise ValueError(f"unknown cross_region_policy: {cross_region_policy!r}")

    deduped = dedupe_companies(company_records)

    normalized = []
    dropped_bogus = []
    for r in deduped:
        csm_local = normalize_csm_email(r.get("csm_email"))
        if csm_local is None:
            dropped_bogus.append(r)
            continue
        arr = max(float(r.get("arr_won_all_rollup") or 0.0), 0.0)
        normalized.append({
            "hs_object_id": r["hs_object_id"],
            "name": r.get("name"),
            "csm_local": csm_local,
            "region": r.get("td_company_owner_region"),
            "arr": arr,
            "domain": r.get("domain"),
        })

    by_csm = {}
    for row in normalized:
        by_csm.setdefault(row["csm_local"], []).append(row)
    matrix = {csm: resolve_csm_region(rows) for csm, rows in by_csm.items()}

    in_scope = []
    if cross_region_policy == "csm_scoped":
        target_csms = {csm for csm, m in matrix.items() if m["region"] == region}
        in_scope = [r for r in normalized if r["csm_local"] in target_csms]
    else:  # account_scoped
        in_scope = [r for r in normalized if r["region"] == region]

    scorecard = sorted(
        (r for r in in_scope if r["arr"] >= min_arr),
        key=lambda r: r["hs_object_id"],
    )
    excluded = sorted(
        (r for r in in_scope if r["arr"] < min_arr),
        key=lambda r: r["hs_object_id"],
    )

    return {
        "region": region,
        "policy": cross_region_policy,
        "min_arr": min_arr,
        "scorecard_accounts": scorecard,
        "excluded_accounts": excluded,
        "dropped_bogus_csm": dropped_bogus,
        "matrix": matrix,
        "n_total": len(scorecard),
    }


def diff_books(book_a, book_b):
    """Diff two build_regional_book outputs to surface determinism breaks.

    Used by the observational determinism harness — runs the real pipeline
    multiple times and feeds each result through this function. A non-empty
    return means the assembly is not deterministic on real HubSpot data.

    Returns:
        {
            "n_total_delta":   book_b["n_total"] - book_a["n_total"],
            "only_in_a":       [hs_object_id, ...],   # in A but not B
            "only_in_b":       [hs_object_id, ...],   # in B but not A
            "arr_changed":     [{id, arr_a, arr_b}, ...],
            "csm_changed":     [{id, csm_a, csm_b}, ...],
            "region_changed":  [{id, region_a, region_b}, ...],
            "identical":       bool,
        }

    Sorting is stable and id-keyed so the diff itself is deterministic.
    """
    by_a = {r["hs_object_id"]: r for r in book_a.get("scorecard_accounts") or []}
    by_b = {r["hs_object_id"]: r for r in book_b.get("scorecard_accounts") or []}
    ids_a = set(by_a)
    ids_b = set(by_b)

    only_in_a = sorted(ids_a - ids_b)
    only_in_b = sorted(ids_b - ids_a)

    arr_changed = []
    csm_changed = []
    region_changed = []
    for rid in sorted(ids_a & ids_b):
        a, b = by_a[rid], by_b[rid]
        if a.get("arr") != b.get("arr"):
            arr_changed.append({"id": rid, "arr_a": a.get("arr"), "arr_b": b.get("arr")})
        if a.get("csm_local") != b.get("csm_local"):
            csm_changed.append({"id": rid, "csm_a": a.get("csm_local"), "csm_b": b.get("csm_local")})
        if a.get("region") != b.get("region"):
            region_changed.append({"id": rid, "region_a": a.get("region"), "region_b": b.get("region")})

    identical = (
        not only_in_a and not only_in_b
        and not arr_changed and not csm_changed and not region_changed
    )
    return {
        "n_total_delta": book_b.get("n_total", 0) - book_a.get("n_total", 0),
        "only_in_a": only_in_a,
        "only_in_b": only_in_b,
        "arr_changed": arr_changed,
        "csm_changed": csm_changed,
        "region_changed": region_changed,
        "identical": identical,
    }
