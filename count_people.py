#!/usr/bin/env python3
"""
Computes an ACCURATE, deduplicated "how many people total" count.

Why this exists: summing each prefix's match count (what sweep_state.json
stores) badly overcounts, because the same person usually matches many
different 3-letter prefixes (e.g. a name like "Apisit" matches "api",
"pis", "isi", "sit", ...). Simply adding those counts together is wrong.

How it stays privacy-safe while still deduplicating correctly: for each
person row, the browser itself computes a SHA-256 hash of that row's text
(see get_result_hashes() in daily_cheer_bot.py) and only the hash crosses
back into this script -- the actual name/email is read by the page's own
JS engine but never serialized to Python. Two rows for the same person
(found under different prefixes) hash identically, so a plain Python set()
dedupes them correctly. A SHA-256 digest cannot be reversed back into the
original text, so persisting the hash set itself (in people_count_state.json,
to make this script resumable) still never stores anyone's name or email --
only the final integer in people_count.json is meant to be read by anything
else (the dashboard).

Only checks the prefixes already confirmed valid in known_prefixes.json
(not the full 17,550-prefix search space -- that's full_sweep.py's job),
so this runs in minutes, not hours. Safe to re-run any time you want an
updated figure (e.g. after full_sweep.py discovers more prefixes) -- it
resumes from where a previous run left off.
"""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

from daily_cheer_bot import (
    CDP_URL,
    KNOWN_PREFIXES_FILE,
    PEOPLE_COUNT_FILE,
    STATE_DIR,
    ensure_on_daily_cheer,
    find_daily_cheer_page,
    get_result_hashes,
    load_json,
    save_json,
    search_and_wait_for_stable_results,
)

STATE_FILE = STATE_DIR / "people_count_state.json"
SAVE_EVERY = 25
MAX_CONSECUTIVE_FAILURES = 3


def get_page_or_none(browser):
    """find_daily_cheer_page() + ensure_on_daily_cheer(), tolerating a fully
    closed/dead page object (not just one that navigated away)."""
    page = find_daily_cheer_page(browser)
    if page is None:
        return None
    try:
        page.bring_to_front()
        if not ensure_on_daily_cheer(page):
            return None
    except Exception:
        return None
    return page


def run():
    known_prefixes = load_json(KNOWN_PREFIXES_FILE, [])
    if not known_prefixes:
        print("No known prefixes yet -- run full_sweep.py first.")
        sys.exit(1)

    state = load_json(STATE_FILE, {"completed": [], "hashes": []})
    completed = set(state["completed"])
    unique_hashes = set(state["hashes"])
    remaining = [p for p in sorted(known_prefixes) if p not in completed]

    print(f"Known prefixes: {len(known_prefixes)} total, "
          f"{len(completed)} already counted (resumed), {len(remaining)} remaining")
    print(f"Unique people so far: {len(unique_hashes)}")

    if not remaining:
        print("Nothing left to do.")
        save_json(PEOPLE_COUNT_FILE, {"count": len(unique_hashes)})
        return

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(CDP_URL)
        page = get_page_or_none(browser)
        if page is None:
            print("ERROR: no open/usable Aster tab found. Open https://aster.arise.tech/daily-cheer/ first.")
            sys.exit(1)

        def checkpoint():
            save_json(STATE_FILE, {"completed": sorted(completed), "hashes": sorted(unique_hashes)})
            save_json(PEOPLE_COUNT_FILE, {"count": len(unique_hashes)})

        prev_signature = None
        consecutive_failures = 0
        aborted = False

        for i, prefix in enumerate(remaining, 1):
            try:
                if "daily-cheer" not in page.url:
                    raise RuntimeError("page drifted away from daily-cheer")
            except Exception:
                page = get_page_or_none(browser)
                if page is None:
                    consecutive_failures += 1
                    print(f"  [{prefix}] could not recover the page "
                          f"({consecutive_failures}/{MAX_CONSECUTIVE_FAILURES})")
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        print("Too many consecutive failures -- stopping early "
                              "(re-open the Aster tab and re-run to continue).")
                        aborted = True
                        break
                    continue
                prev_signature = None

            try:
                prev_signature = search_and_wait_for_stable_results(page, prefix, prev_signature)
                hashes = get_result_hashes(page)
                unique_hashes.update(hashes)
                completed.add(prefix)
                consecutive_failures = 0
            except Exception as e:
                consecutive_failures += 1
                print(f"  [{prefix}] WARN: {e} ({consecutive_failures}/{MAX_CONSECUTIVE_FAILURES})")
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    print("Too many consecutive failures -- stopping early "
                          "(re-open the Aster tab and re-run to continue).")
                    aborted = True
                    break

            if i % SAVE_EVERY == 0:
                checkpoint()
                print(f"  [{i}/{len(remaining)}] unique so far: {len(unique_hashes)}")

        browser.close()
        checkpoint()

    print(f"\n{'Partial' if aborted else 'Done'}. Unique people: {len(unique_hashes)} "
          f"({len(completed)}/{len(known_prefixes)} prefixes checked)")
    print(f"Saved to {PEOPLE_COUNT_FILE}")
    if aborted:
        print("Re-run this script to pick up where it left off.")


if __name__ == "__main__":
    run()
