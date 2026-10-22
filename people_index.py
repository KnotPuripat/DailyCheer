"""Builds a deduplicated people index from sweep_state.json's raw labels.

Raw labels look like "Jane Djane.d@example.com" -- name and email
concatenated with no separator (that's just how the site's DOM text comes
out). We split on the email pattern to recover both parts.
"""

import json
from pathlib import Path

from daily_cheer_bot import HEARTED_PEOPLE_FILE, load_daily_list, parse_label  # noqa: F401 (re-exported)

STATE_DIR = Path.home() / ".aster_daily_cheer"
SWEEP_STATE_FILE = STATE_DIR / "sweep_state.json"


def build_people_index():
    """Returns dict: email -> {name, email, prefixes: [...]}"""
    sweep_state = json.loads(SWEEP_STATE_FILE.read_text()) if SWEEP_STATE_FILE.exists() else {"found": {}}
    found = sweep_state.get("found", {})

    people = {}
    for prefix, labels in found.items():
        for label in labels:
            name, email = parse_label(label)
            if not email:
                continue
            key = email.lower()
            if key not in people:
                people[key] = {"name": name, "email": email, "label": label, "prefixes": []}
            people[key]["prefixes"].append(prefix)

    return people


def load_hearted_set():
    """Hearted TODAY only -- the site resets everyone's Heart button daily,
    so yesterday's hearted list is stale and must not be reused."""
    return set(load_daily_list(HEARTED_PEOPLE_FILE))
