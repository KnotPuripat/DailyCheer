#!/usr/bin/env python3
"""
Aster "Daily Cheer" auto-discovery + auto-heart bot.

Run this ONCE PER DAY manually (hearting is limited to once/day per the site).

Requirements before running:
  - Chrome must already be open with: --remote-debugging-port=9222
  - You must already be logged in / wallet-connected on
    https://aster.arise.tech/daily-cheer/ in that Chrome window
    (this script does NOT touch MetaMask or wallet-connect at all)

What it does:
  1. Attaches to the existing, already-authenticated "Daily Cheer" tab
     (never opens a new tab -- opening a new tab re-triggers a MetaMask
     signature request, which this script cannot and will not approve).
  2. Re-checks every previously-discovered 3-letter prefix (stored in
     known_prefixes.json) for new people.
  3. Tries NEW_PREFIXES_PER_RUN random 3-letter prefixes (excluding
     aaa/bbb/ccc-style all-same-letter ones, and ones already known).
  4. For every person found, clicks "Heart" -- but only if the button
     is not already disabled (disabled == already hearted TODAY).
  5. Saves newly-discovered working prefixes back to known_prefixes.json
     (permanent -- these don't reset).
  6. Appends a summary of what happened to run_log.jsonl.

Hearting resets daily on the site -- everyone's Heart button goes back to
clickable the next day. So hearted_people.json and exhausted_prefixes.json
are PER-DAY caches (each tagged with the date they were written), not
permanent history: a stale entry from a previous day is discarded on load,
not treated as still true today.
"""

import json
import random
import re
import string
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

STATE_DIR = Path.home() / ".aster_daily_cheer"
KNOWN_PREFIXES_FILE = STATE_DIR / "known_prefixes.json"
RUN_LOG_FILE = STATE_DIR / "run_log.jsonl"
# Hearting resets daily on the site itself -- someone hearted today can be
# hearted again tomorrow (their Heart button goes from disabled back to
# clickable). So both of these are PER-DAY state, not permanent history:
# stale entries from a previous day are discarded on load, not carried
# forward, otherwise the bot would skip real work forever.
HEARTED_PEOPLE_FILE = STATE_DIR / "hearted_people.json"
EXHAUSTED_PREFIXES_FILE = STATE_DIR / "exhausted_prefixes.json"

CDP_URL = "http://localhost:9222"
DAILY_CHEER_URL = "https://aster.arise.tech/daily-cheer/"
SEARCH_SELECTOR = '[placeholder="Search by name, email"]'
NEW_PREFIXES_PER_RUN = 5


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return default
    return default


def save_json(path, data):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def today_str():
    return date.today().isoformat()  # local date -- "today" as the user experiences it


def load_daily_list(path):
    """
    Loads a {"date": "...", "items": [...]} file, but only if its date is
    today's -- otherwise returns [] (yesterday's hearted/exhausted list is
    stale, since the site resets hearting daily). Also accepts a bare old-style
    list (pre-dated files from before this reset logic existed) as stale/empty,
    since there's no way to know what day it was written on.
    """
    raw = load_json(path, None)
    if not isinstance(raw, dict):
        return []
    if raw.get("date") != today_str():
        return []
    return raw.get("items", [])


def save_daily_list(path, items):
    save_json(path, {"date": today_str(), "items": sorted(set(items))})


def is_all_same_letter(s):
    return len(set(s)) == 1


# The site's DOM text concatenates "{Name} {Initial}" directly against the
# lowercase email local-part with no separator, e.g. a row's scraped label
# reads "Jane Djane.d@example.com" for name "Jane D" + email
# "jane.d@example.com". Anchoring the match to a lowercase start correctly
# skips over that trailing uppercase initial instead of swallowing it into
# the email.
EMAIL_RE = re.compile(r"[a-z][a-z0-9_.+-]*@[A-Za-z0-9-]+\.[A-Za-z0-9.-]+")


def parse_label(label):
    """'Jane Djane.d@example.com' -> ('Jane D', 'jane.d@example.com')"""
    m = EMAIL_RE.search(label)
    if not m:
        return label.strip(), None
    email = m.group(0)
    name = label[: m.start()].strip()
    return name, email


def format_label(label, width=24):
    """Raw scraped label -> readable 'Name    email' for logs/UI."""
    name, email = parse_label(label)
    if not email:
        return label
    return f"{name:<{width}} {email}"


def random_prefix():
    return "".join(random.choice(string.ascii_lowercase) for _ in range(3))


def generate_new_prefixes(known, count):
    known_set = set(known)
    candidates = []
    attempts = 0
    while len(candidates) < count and attempts < count * 50:
        attempts += 1
        p = random_prefix()
        if is_all_same_letter(p):
            continue
        if p in known_set or p in candidates:
            continue
        candidates.append(p)
    return candidates


def ensure_on_daily_cheer(page, timeout_ms=15000):
    """
    The Aster SPA occasionally client-side-routes itself back to '/' (Home)
    mid-session, with no MetaMask prompt involved (confirmed by checking for
    popups when this happens) -- just a site-side routing quirk. Since this
    stays within the same already-authenticated tab/origin, re-navigating to
    the daily-cheer path is safe (unlike opening a brand new tab, which does
    trigger a fresh wallet signature request). Returns True if the search box
    is present/usable afterwards.
    """
    if "daily-cheer" in page.url:
        try:
            page.locator(SEARCH_SELECTOR).wait_for(state="attached", timeout=1500)
            return True
        except Exception:
            pass  # URL says daily-cheer but box isn't there yet/anymore -- fall through to reload

    page.goto(DAILY_CHEER_URL)
    try:
        page.locator(SEARCH_SELECTOR).wait_for(state="attached", timeout=timeout_ms)
        return True
    except Exception:
        return False


def find_daily_cheer_page(browser):
    """
    Finds the already-authenticated Aster tab. Prefers one already sitting on
    /daily-cheer/, but falls back to any aster.arise.tech tab (e.g. one the
    SPA's client-side router bounced back to Home) -- callers should follow
    up with ensure_on_daily_cheer() to route it to the right page, since that
    re-navigation is safe within an already-open, already-authenticated tab.
    """
    fallback = None
    for ctx in browser.contexts:
        for page in ctx.pages:
            if "daily-cheer" in page.url:
                return page
            if "aster.arise.tech" in page.url and fallback is None:
                fallback = page
    return fallback


_SNAPSHOT_JS = """
() => {
    const buttons = Array.from(document.querySelectorAll('button'))
        .filter(b => b.offsetParent !== null && b.textContent.trim() === 'Heart');
    return buttons.map(b => {
        let row = b.closest('div');
        for (let i = 0; i < 6 && row; i++) {
            if (row.textContent.includes('@')) break;
            row = row.parentElement;
        }
        const label = row ? row.textContent.replace('View profileHeart', '').trim() : 'unknown';
        const disabled = b.classList.contains('cursor-not-allowed') || b.disabled;
        return [label, disabled];
    });
}
"""


def _raw_result_signature(page):
    """
    Cheap, ATOMIC snapshot of current result state: ('empty',) or
    ('people', (labels...)). Uses a single page.evaluate() call (not
    per-element Playwright locators) so a mid-read re-render of the results
    list can't cause a stale-handle timeout/crash.
    """
    if page.locator("text=No Result").count() > 0:
        return ("empty",)
    try:
        data = page.evaluate(_SNAPSHOT_JS)
    except Exception:
        return ("empty",)
    labels = tuple(d[0] for d in data)
    if not labels:
        return ("empty",)
    return ("people", labels)


def search_and_wait_for_stable_results(page, term, prev_signature, max_wait_s=8, poll_s=0.3):
    """
    Fills the search box, then polls until the result panel reports a
    trustworthy snapshot. Fast-path: an ('empty',) reading, or a ('people', X)
    reading where X != the previous prefix's people tuple, is accepted as
    soon as it's seen twice in a row (debounce has settled). Only the
    suspicious case -- X identical to the immediately preceding prefix's
    people tuple, which is how the original stale-read bug manifested --
    falls back to waiting for a genuine change or the full timeout.
    Returns the final (possibly still-stale-if-timed-out) signature.
    """
    box = page.locator(SEARCH_SELECTOR)
    box.fill("")
    box.fill(term)

    prev_people = prev_signature[1] if prev_signature and prev_signature[0] == "people" else None

    deadline = time.time() + max_wait_s
    last = None
    while time.time() < deadline:
        time.sleep(poll_s)
        current = _raw_result_signature(page)
        if current == last:
            if current[0] == "empty":
                return current
            if current[0] == "people" and current[1] != prev_people:
                return current
            # same people-list as the previous prefix: keep polling for real
            # change, protecting against the stale-carryover bug, up to deadline
        last = current
    return last


def get_result_rows(page):
    """Returns list of dicts: {label, index, disabled}. No live button handles
    (those go stale across re-renders) -- callers re-locate by index right
    before clicking, via click_heart_by_index()."""
    try:
        data = page.evaluate(_SNAPSHOT_JS)
    except Exception:
        return []
    return [{"label": label, "index": i, "disabled": disabled} for i, (label, disabled) in enumerate(data)]


def click_heart_by_index(page, index, timeout_ms=5000):
    """Re-locates the Heart button fresh (by current visible position) right
    before clicking, so it can't go stale from an earlier read."""
    btn = page.locator('button:visible:has-text("Heart")').nth(index)
    btn.click(timeout=timeout_ms)


def has_no_result(page):
    return page.locator("text=No Result").count() > 0


def run():
    known_prefixes = load_json(KNOWN_PREFIXES_FILE, [])
    hearted_set = set(load_daily_list(HEARTED_PEOPLE_FILE))
    exhausted = set(load_daily_list(EXHAUSTED_PREFIXES_FILE))

    active_known = [p for p in known_prefixes if p not in exhausted]
    new_candidates = generate_new_prefixes(known_prefixes, NEW_PREFIXES_PER_RUN)
    all_prefixes_to_try = active_known + new_candidates

    print(f"Today: {today_str()}")
    print(f"Known prefixes: {len(known_prefixes)} total, "
          f"{len(exhausted)} exhausted TODAY (skipped), "
          f"{len(active_known)} to check")
    print(f"New candidates this run: {new_candidates}")

    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "known_prefixes_checked": known_prefixes,
        "new_candidates_tried": new_candidates,
        "newly_discovered_prefixes": [],
        "hearted_this_run": [],
        "already_hearted_skipped": [],
        "no_result_prefixes": [],
    }

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            print(f"ERROR: could not connect to Chrome at {CDP_URL}: {e}")
            print("Make sure Chrome is running with --remote-debugging-port=9222")
            sys.exit(1)

        page = find_daily_cheer_page(browser)
        if page is None:
            print("ERROR: no open Aster tab found at all")
            print("Open https://aster.arise.tech/daily-cheer/ manually in the already-running Chrome first.")
            sys.exit(1)

        page.bring_to_front()
        if not ensure_on_daily_cheer(page):
            print("ERROR: could not load the daily-cheer search page (timed out)")
            sys.exit(1)

        def checkpoint():
            save_json(KNOWN_PREFIXES_FILE, sorted(set(known_prefixes)))
            save_daily_list(HEARTED_PEOPLE_FILE, hearted_set)
            save_daily_list(EXHAUSTED_PREFIXES_FILE, exhausted)

        prev_signature = None
        interrupted = False
        for i, prefix in enumerate(all_prefixes_to_try, 1):
            try:
                if "daily-cheer" not in page.url:
                    print(f"  (page drifted to {page.url}, re-navigating back to daily-cheer)")
                    if not ensure_on_daily_cheer(page):
                        raise RuntimeError("lost the daily-cheer page and could not recover it")
                    prev_signature = None  # fresh page load, no stale-result risk to guard against

                prev_signature = search_and_wait_for_stable_results(page, prefix, prev_signature)

                if has_no_result(page):
                    summary["no_result_prefixes"].append(prefix)
                    print(f"  [{prefix}] no result")
                    continue

                rows = get_result_rows(page)
                if not rows:
                    summary["no_result_prefixes"].append(prefix)
                    print(f"  [{prefix}] no result")
                    continue

                if prefix not in known_prefixes:
                    known_prefixes.append(prefix)
                    summary["newly_discovered_prefixes"].append(prefix)
                    print(f"  [{prefix}] NEW working prefix, {len(rows)} match(es)")
                else:
                    print(f"  [{prefix}] {len(rows)} match(es)")

                all_done = True
                for row in rows:
                    label = row["label"]
                    if row["disabled"]:
                        summary["already_hearted_skipped"].append(label)
                        print(f"    - skip (already hearted): {format_label(label)}")
                        continue
                    try:
                        click_heart_by_index(page, row["index"])
                        page.wait_for_timeout(1200)
                        summary["hearted_this_run"].append(label)
                        hearted_set.add(label)
                        print(f"    - HEARTED: {format_label(label)}")
                    except Exception as e:
                        all_done = False
                        print(f"    - FAILED to heart {format_label(label)}: {e}")

                if all_done and prefix not in exhausted:
                    exhausted.add(prefix)

            except Exception as e:
                # e.g. the page became unusable mid-run (tab navigated away,
                # closed, etc). Stop the loop but keep everything done so far
                # -- the checkpoint below still saves it. Previously an
                # exception here propagated straight out of run(), skipping
                # every save and silently discarding an entire run's progress.
                print(f"  [{prefix}] INTERRUPTED: {e}")
                interrupted = True
                break

            if i % 20 == 0:
                checkpoint()

        browser.close()
        checkpoint()

    if interrupted:
        print("\n(Run stopped early -- progress up to this point was saved. Re-run to continue.)")

    with RUN_LOG_FILE.open("a") as f:
        f.write(json.dumps(summary, ensure_ascii=False) + "\n")

    print("\n=== Summary ===")
    print(f"Hearted this run: {summary['hearted_this_run']}")
    print(f"Already hearted (skipped): {summary['already_hearted_skipped']}")
    print(f"Newly discovered prefixes: {summary['newly_discovered_prefixes']}")
    print(f"No-result prefixes: {summary['no_result_prefixes']}")
    print(f"Exhausted prefixes today (resets tomorrow): {len(exhausted)} total")


if __name__ == "__main__":
    run()
