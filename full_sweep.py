#!/usr/bin/env python3
"""
Exhaustive 26x26x26 prefix sweep for Aster Daily Cheer search.

READ-ONLY / discovery only -- never clicks Heart. Records which 3-letter
prefixes return people, and HOW MANY -- never names or emails. Nothing in
sweep_state.json (or any other file this project writes) identifies anyone.

Saves incrementally to sweep_state.json so it can be killed and resumed
without losing progress. Merges discovered prefixes into known_prefixes.json
as it goes, so daily_cheer_bot.py can pick them up (and do the actual
hearting) on its next run.
"""

import itertools
import json
import string
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from daily_cheer_bot import (
    CDP_URL,
    KNOWN_PREFIXES_FILE,
    STATE_DIR,
    find_daily_cheer_page,
    is_all_same_letter,
    load_json,
    save_json,
    search_and_wait_for_stable_results,
    seed_known_prefixes,
)

SWEEP_STATE_FILE = STATE_DIR / "sweep_state.json"
SAVE_EVERY = 25


def all_prefixes():
    for combo in itertools.product(string.ascii_lowercase, repeat=3):
        p = "".join(combo)
        if not is_all_same_letter(p):
            yield p


def run():
    seed_known_prefixes()
    state = load_json(SWEEP_STATE_FILE, {"completed": [], "found": {}})
    completed_set = set(state["completed"])
    found = state["found"]

    full_list = list(all_prefixes())
    remaining = [p for p in full_list if p not in completed_set]

    print(f"Total prefixes: {len(full_list)}")
    print(f"Already completed (resumed): {len(completed_set)}")
    print(f"Remaining: {len(remaining)}")

    if not remaining:
        print("Nothing left to do -- sweep already complete.")
        return

    known_prefixes = set(load_json(KNOWN_PREFIXES_FILE, []))

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(CDP_URL)
        page = find_daily_cheer_page(browser)
        if page is None:
            print("ERROR: no open daily-cheer tab found. Open it manually first.")
            sys.exit(1)
        page.bring_to_front()

        prev_signature = None
        t_start = time.time()
        for i, prefix in enumerate(remaining, 1):
            try:
                signature = search_and_wait_for_stable_results(page, prefix, prev_signature)
            except Exception as e:
                print(f"  WARN [{prefix}] read failed, skipping: {e}")
                completed_set.add(prefix)
                prev_signature = ("empty",)
                continue
            prev_signature = signature

            if signature[0] == "people":
                count = len(signature[1])  # count only -- never store the names/emails themselves
                found[prefix] = count
                known_prefixes.add(prefix)
                print(f"  FOUND [{prefix}]: {count} people")

            completed_set.add(prefix)

            if i % SAVE_EVERY == 0 or i == len(remaining):
                state["completed"] = sorted(completed_set)
                state["found"] = found
                save_json(SWEEP_STATE_FILE, state)
                save_json(KNOWN_PREFIXES_FILE, sorted(known_prefixes))

                elapsed = time.time() - t_start
                rate = i / elapsed
                remaining_count = len(remaining) - i
                eta_s = remaining_count / rate if rate > 0 else 0
                print(
                    f"[{i}/{len(remaining)}] elapsed={elapsed/60:.1f}m "
                    f"eta={eta_s/60:.1f}m found_so_far={len(found)}"
                )

        browser.close()

    print("\n=== Sweep complete ===")
    print(f"Total working prefixes found: {len(found)}")
    for prefix, count in sorted(found.items()):
        print(f"  {prefix}: {count} people")


if __name__ == "__main__":
    run()
