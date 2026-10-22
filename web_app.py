#!/usr/bin/env python3
"""
Local-only web UI for the Aster Daily Cheer bot. Styled to match the real
aster.arise.tech Daily Cheer page (same font, colors, pill buttons).

One tab: "Heart Everyone" -- bulk-hearts everyone not yet hearted today
(same logic as daily_cheer_bot.py), with a live progress bar, log, and
pause/resume.

PRIVACY: this page shows COUNTS ONLY -- how many people a prefix matched,
how many have been hearted. It never lists, searches, or displays anyone's
name or email. (An earlier version had a browsable "All People" list with
names; that's been removed on purpose.)

Run:
    python3 web_app.py
Then open:
    http://127.0.0.1:5057

Binds to 127.0.0.1 only -- not reachable from other machines on the network.
"""

import json
import threading
import time
from datetime import datetime, timezone

from flask import Flask, jsonify
from playwright.sync_api import sync_playwright

from daily_cheer_bot import (
    CDP_URL,
    EXHAUSTED_PREFIXES_FILE,
    HEARTED_COUNT_FILE,
    KNOWN_PREFIXES_FILE,
    NEW_PREFIXES_PER_RUN,
    RUN_LOG_FILE,
    click_heart_by_index,
    ensure_on_daily_cheer,
    find_daily_cheer_page,
    generate_new_prefixes,
    get_result_rows,
    load_daily_count,
    load_daily_list,
    load_json,
    PEOPLE_COUNT_FILE,
    save_daily_count,
    save_daily_list,
    save_json,
    search_and_wait_for_stable_results,
    seed_known_prefixes,
)
from full_sweep import SWEEP_STATE_FILE

seed_known_prefixes()

app = Flask(__name__)

_state_lock = threading.Lock()
_bulk_state = {
    "running": False,
    "paused": False,
    "done": False,
    "error": None,
    "total": 0,
    "processed": 0,
    "hearted_count": 0,
    "skipped_count": 0,
    "newly_discovered": [],
    "log": [],
}


def _log(msg, cap=200):
    with _state_lock:
        _bulk_state["log"].append(msg)
        if len(_bulk_state["log"]) > cap:
            _bulk_state["log"] = _bulk_state["log"][-cap:]


def run_bulk_heart():
    with _state_lock:
        _bulk_state.update(
            running=True, paused=False, done=False, error=None, total=0, processed=0,
            hearted_count=0, skipped_count=0, newly_discovered=[], log=[],
        )

    known_prefixes = load_json(KNOWN_PREFIXES_FILE, [])
    hearted_today_start = load_daily_count(HEARTED_COUNT_FILE)
    hearted_this_run = 0
    exhausted = set(load_daily_list(EXHAUSTED_PREFIXES_FILE))
    active_known = [p for p in known_prefixes if p not in exhausted]
    new_candidates = generate_new_prefixes(known_prefixes, NEW_PREFIXES_PER_RUN)
    all_prefixes = active_known + new_candidates

    with _state_lock:
        _bulk_state["total"] = len(all_prefixes)

    _log(f"Checking {len(active_known)} active known prefixes "
         f"({len(exhausted)} exhausted/skipped) + {len(new_candidates)} new")

    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "web_app bulk run",
        "hearted_count": 0,
        "skipped_count": 0,
        "newly_discovered_prefixes": [],
    }

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(CDP_URL)
            page = find_daily_cheer_page(browser)
            if page is None:
                raise RuntimeError("no open Aster tab found -- open https://aster.arise.tech/daily-cheer/ in Chrome first")
            page.bring_to_front()
            if not ensure_on_daily_cheer(page):
                raise RuntimeError("could not load the daily-cheer search page (timed out)")

            def checkpoint():
                save_json(KNOWN_PREFIXES_FILE, sorted(set(known_prefixes)))
                save_daily_count(HEARTED_COUNT_FILE, hearted_today_start + hearted_this_run)
                save_daily_list(EXHAUSTED_PREFIXES_FILE, exhausted)

            prev_signature = None
            interrupted_error = None
            for i, prefix in enumerate(all_prefixes, 1):
                # Pause point: only between prefixes (never mid-click), so we
                # never leave a prefix half-hearted.
                while True:
                    with _state_lock:
                        if not _bulk_state["paused"]:
                            break
                    time.sleep(0.3)

                try:
                    if "daily-cheer" not in page.url:
                        _log(f"(page drifted away, re-navigating back to daily-cheer)")
                        if not ensure_on_daily_cheer(page):
                            raise RuntimeError("lost the daily-cheer page and could not recover it")
                        prev_signature = None

                    prev_signature = search_and_wait_for_stable_results(page, prefix, prev_signature)
                    rows = get_result_rows(page)

                    if rows and prefix not in known_prefixes:
                        known_prefixes.append(prefix)
                        summary["newly_discovered_prefixes"].append(prefix)
                        with _state_lock:
                            _bulk_state["newly_discovered"].append(prefix)
                        _log(f"[{prefix}] new prefix, {len(rows)} people")

                    prefix_hearted = 0
                    prefix_skipped = 0
                    all_done = True
                    for row in rows:
                        if row["disabled"]:
                            prefix_skipped += 1
                            continue
                        try:
                            click_heart_by_index(page, row["index"])
                            page.wait_for_timeout(1000)
                            prefix_hearted += 1
                        except Exception as e:
                            all_done = False
                            _log(f"[{prefix}] failed to heart one person: {e}")

                    if prefix_hearted:
                        hearted_this_run += prefix_hearted
                        summary["hearted_count"] += prefix_hearted
                        with _state_lock:
                            _bulk_state["hearted_count"] += prefix_hearted
                        _log(f"[{prefix}] hearted {prefix_hearted}")
                    if prefix_skipped:
                        summary["skipped_count"] += prefix_skipped
                        with _state_lock:
                            _bulk_state["skipped_count"] += prefix_skipped

                    if all_done and rows and prefix not in exhausted:
                        exhausted.add(prefix)

                except Exception as e:
                    # Page became unusable mid-run (tab navigated away/closed,
                    # etc). Stop but keep everything done so far -- the
                    # checkpoint right after this loop still saves it, instead
                    # of the whole run's progress silently vanishing.
                    _log(f"[{prefix}] INTERRUPTED: {e}")
                    interrupted_error = str(e)
                    break

                with _state_lock:
                    _bulk_state["processed"] += 1

                if i % 20 == 0:
                    checkpoint()

            browser.close()
            checkpoint()

        with RUN_LOG_FILE.open("a") as f:
            f.write(json.dumps(summary, ensure_ascii=False) + "\n")

        if interrupted_error:
            _log(f"Stopped early: {interrupted_error}. Progress up to this point was saved.")
            with _state_lock:
                _bulk_state["error"] = interrupted_error
        else:
            _log(f"Done. Hearted {summary['hearted_count']}, "
                 f"skipped {summary['skipped_count']}, "
                 f"new prefixes {len(summary['newly_discovered_prefixes'])}")

    except Exception as e:
        _log(f"ERROR: {e}")
        with _state_lock:
            _bulk_state["error"] = str(e)
    finally:
        with _state_lock:
            _bulk_state["running"] = False
            _bulk_state["done"] = True


@app.route("/")
def index():
    # No caching -- this file gets edited and the server restarted often during
    # development; a stale cached copy in the browser would silently keep
    # showing an old version after a fix.
    resp = app.make_response(INDEX_HTML)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/summary")
def api_summary():
    # total_people comes from people_count.json, a deduplicated count built
    # by count_people.py (run it to refresh this number -- see README).
    # Summing sweep_state.json's per-prefix counts directly would badly
    # overcount, since the same person usually matches several prefixes.
    people_count = load_json(PEOPLE_COUNT_FILE, {}).get("count")
    if people_count is None:
        # Fallback before count_people.py has ever been run: the naive sum,
        # clearly inflated, but better than showing nothing.
        sweep = load_json(SWEEP_STATE_FILE, {"found": {}})
        people_count = sum(sweep.get("found", {}).values())
    hearted = load_daily_count(HEARTED_COUNT_FILE)
    return jsonify({"total_people": people_count, "hearted_count": hearted})


@app.route("/api/heart-all", methods=["POST"])
def api_heart_all():
    with _state_lock:
        if _bulk_state["running"]:
            return jsonify({"ok": False, "error": "already running"}), 409
    t = threading.Thread(target=run_bulk_heart, daemon=True)
    t.start()
    time.sleep(0.2)
    return jsonify({"ok": True})


@app.route("/api/heart-all/status")
def api_heart_all_status():
    with _state_lock:
        return jsonify(dict(_bulk_state))


@app.route("/api/heart-all/pause", methods=["POST"])
def api_heart_all_pause():
    with _state_lock:
        if not _bulk_state["running"]:
            return jsonify({"ok": False, "error": "not running"}), 409
        _bulk_state["paused"] = not _bulk_state["paused"]
        paused = _bulk_state["paused"]
    _log("Paused." if paused else "Resumed.")
    return jsonify({"ok": True, "paused": paused})


INDEX_HTML = """
<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Aster | Daily Cheer</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #0d0f11;
    --card: rgba(255,255,255,0.04);
    --card-border: rgba(255,255,255,0.06);
    --text: #ffffff;
    --text-dim: #9a9aa5;
    --pink: #ea65e0;
    --pink-solid: #ec4ea0;
    --teal: #6be2be;
    --pill-bg: rgba(255,255,255,0.08);
    --pill-bg-faint: rgba(255,255,255,0.04);
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: 'Poppins', sans-serif;
    background: var(--bg);
    color: var(--text);
  }

  .hero {
    position: relative;
    padding: 56px 32px 80px;
    background:
      radial-gradient(circle at 85% 10%, rgba(234,101,224,0.30), transparent 55%),
      linear-gradient(160deg, #1a0e2e 0%, #2a1240 40%, #4a123f 75%, #0c0510 100%);
    overflow: hidden;
  }
  .hero h1 {
    font-size: 48px; font-weight: 700; margin: 0 0 10px;
    background: linear-gradient(to right, #d1d5db, #ffffff, #d1d5db);
    -webkit-background-clip: text; background-clip: text; color: transparent;
  }
  .hero p { color: #cfc6d8; margin: 0; font-size: 15px; }

  .wrap { max-width: 680px; margin: -36px auto 48px; padding: 0 20px; position: relative; z-index: 2; }

  .card {
    background: var(--card);
    border: 1px solid var(--card-border);
    border-radius: 16px;
    padding: 32px;
    margin-bottom: 16px;
  }
  .card h2 { margin: 0 0 6px; font-size: 28px; font-weight: 600; }
  .card .sub { color: var(--text-dim); font-size: 14px; margin: 0 0 22px; line-height: 1.5; }

  .stats { display: flex; gap: 28px; margin-bottom: 22px; }
  .stat .num { font-size: 28px; font-weight: 700; }
  .stat .label { color: var(--text-dim); font-size: 12px; }

  .cta-row { display: flex; gap: 10px; }

  button.cta {
    width: 100%;
    font-family: 'Poppins', sans-serif;
    border: none;
    border-radius: 9999px;
    padding: 16px;
    font-size: 15px;
    font-weight: 600;
    cursor: pointer;
    background: var(--text);
    color: #0d0f11;
    display: flex; align-items: center; justify-content: center; gap: 8px;
  }
  button.cta:disabled { opacity: 0.5; cursor: wait; }

  button.cta.pause-cta {
    display: none;
    width: auto;
    flex: 0 0 auto;
    padding: 16px 22px;
    background: var(--pill-bg);
    color: var(--text);
  }
  button.cta.pause-cta.show { display: flex; }
  button.cta.pause-cta.is-paused { background: var(--teal); color: #04231d; }

  .progress-wrap { margin-top: 20px; display: none; }
  .progress-wrap.show { display: block; }
  .bar-track { background: #1d1e26; border-radius: 9999px; height: 8px; overflow: hidden; }
  .bar-fill { background: linear-gradient(90deg, var(--pink-solid), var(--teal)); height: 100%; width: 0%; transition: width 0.3s; }
  .progress-meta { display: flex; justify-content: space-between; font-size: 12px; color: var(--text-dim); margin-top: 8px; }

  .log {
    margin-top: 18px; max-height: 260px; overflow-y: auto;
    background: #0a0b0d; border-radius: 12px; padding: 12px 14px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12.5px; line-height: 1.6; color: #b8b8c4; display: none;
  }
  .log.show { display: block; }
  .log .heart-line { color: var(--pink-solid); }
  .log .new-line { color: var(--teal); }

  .done-badge, .error-badge {
    display: none; margin-top: 16px; padding: 12px 16px; border-radius: 12px; font-size: 14px;
  }
  .done-badge { background: rgba(107,226,190,0.12); color: var(--teal); font-weight: 600; }
  .error-badge { background: rgba(234,101,224,0.12); color: var(--pink); }
  .done-badge.show, .error-badge.show { display: block; }

  .privacy-note {
    font-size: 12px; color: var(--text-dim); margin-top: 18px;
    padding-top: 14px; border-top: 1px solid var(--card-border);
  }
</style>
</head>
<body>
  <div class="hero">
    <h1>Daily Cheer</h1>
    <p>A space to recognize and celebrate the colleagues around you</p>
  </div>

  <div class="wrap">
    <div class="card">
      <h2>Who Deserves a Heart Today?</h2>
      <p class="sub">Hearts every known colleague who hasn't been hearted yet today, in one click.</p>

      <div class="stats">
        <div class="stat"><div class="num" id="totalPeople">--</div><div class="label">people known</div></div>
        <div class="stat"><div class="num" id="heartedCount">--</div><div class="label">hearted today</div></div>
        <div class="stat"><div class="num" id="remainingCount">--</div><div class="label">remaining</div></div>
      </div>

      <div class="cta-row">
        <button class="cta" id="heartAllBtn">Heart Everyone</button>
        <button class="cta pause-cta" id="pauseBtn">Pause</button>
      </div>

      <div class="progress-wrap" id="progressWrap">
        <div class="bar-track"><div class="bar-fill" id="barFill"></div></div>
        <div class="progress-meta">
          <span id="progressText">0 / 0 prefixes checked</span>
          <span id="heartedText">0 hearted</span>
        </div>
      </div>

      <div class="done-badge" id="doneBadge"></div>
      <div class="error-badge" id="errorBadge"></div>
      <div class="log" id="log"></div>

      <p class="privacy-note">Counts only -- this page never shows or stores anyone's name or email.</p>
    </div>
  </div>

<script>
const HEART_SVG = '<svg class="ic" viewBox="0 0 24 24" width="14" height="14" style="flex-shrink:0"><path fill="#0d0f11" d="M11.645 20.91l-.007-.003-.022-.012a15.247 15.247 0 01-.383-.218 25.18 25.18 0 01-4.244-3.17C4.688 15.36 2.25 12.174 2.25 8.25 2.25 5.322 4.714 3 7.688 3A5.5 5.5 0 0112 5.052 5.5 5.5 0 0116.313 3c2.973 0 5.437 2.322 5.437 5.25 0 3.925-2.438 7.111-4.739 9.256a25.175 25.175 0 01-4.244 3.17 15.247 15.247 0 01-.383.219l-.022.012-.007.004-.003.001a.752.752 0 01-.704 0l-.003-.001z"/></svg>';
document.getElementById('heartAllBtn').innerHTML = HEART_SVG + ' Heart Everyone';

const btn = document.getElementById('heartAllBtn');
const pauseBtn = document.getElementById('pauseBtn');
const progressWrap = document.getElementById('progressWrap');
const barFill = document.getElementById('barFill');
const progressText = document.getElementById('progressText');
const heartedText = document.getElementById('heartedText');
const doneBadge = document.getElementById('doneBadge');
const errorBadge = document.getElementById('errorBadge');
const logEl = document.getElementById('log');
let polling = null;

async function loadSummary() {
  const res = await fetch('/api/summary');
  const data = await res.json();
  document.getElementById('totalPeople').textContent = data.total_people;
  document.getElementById('heartedCount').textContent = data.hearted_count;
  document.getElementById('remainingCount').textContent = Math.max(0, data.total_people - data.hearted_count);
}

btn.addEventListener('click', async () => {
  btn.disabled = true;
  doneBadge.classList.remove('show');
  errorBadge.classList.remove('show');
  progressWrap.classList.add('show');
  logEl.classList.add('show');
  logEl.innerHTML = '';
  await fetch('/api/heart-all', { method: 'POST' });
  pauseBtn.classList.add('show');
  polling = setInterval(poll, 800);
  poll();
});

pauseBtn.addEventListener('click', async () => {
  pauseBtn.disabled = true;
  const res = await fetch('/api/heart-all/pause', { method: 'POST' });
  const data = await res.json();
  if (data.ok) {
    pauseBtn.textContent = data.paused ? 'Resume' : 'Pause';
    pauseBtn.classList.toggle('is-paused', data.paused);
  }
  pauseBtn.disabled = false;
});

async function poll() {
  const res = await fetch('/api/heart-all/status');
  const s = await res.json();
  const pct = s.total ? Math.round((s.processed / s.total) * 100) : 0;
  barFill.style.width = pct + '%';
  progressText.textContent = `${s.processed} / ${s.total} prefixes checked` + (s.paused ? ' (paused)' : '');
  heartedText.textContent = `${s.hearted_count} hearted, ${s.skipped_count} already done`;
  logEl.innerHTML = s.log.map(line => {
    const cls = line.includes('hearted ') ? 'heart-line' : (line.includes('new prefix') ? 'new-line' : '');
    return `<div class="${cls}">${escapeHtml(line)}</div>`;
  }).join('');
  logEl.scrollTop = logEl.scrollHeight;
  loadSummary();  // live-refresh the "people known / hearted / remaining" stat tiles
  btn.innerHTML = s.running ? (s.paused ? 'Paused' : 'Running...') : (HEART_SVG + ' Heart Everyone');
  btn.disabled = s.running;
  pauseBtn.classList.toggle('show', s.running);
  pauseBtn.classList.toggle('is-paused', s.paused);
  pauseBtn.textContent = s.paused ? 'Resume' : 'Pause';
  if (s.done && !s.running) {
    pauseBtn.classList.remove('show');
    clearInterval(polling);
    if (s.error) {
      errorBadge.textContent = 'Error: ' + s.error;
      errorBadge.classList.add('show');
    } else {
      doneBadge.textContent = `Done! Hearted ${s.hearted_count}, skipped ${s.skipped_count} already-done, found ${s.newly_discovered.length} new prefixes.`;
      doneBadge.classList.add('show');
    }
    loadSummary();
  }
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

loadSummary();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5057, debug=False)
