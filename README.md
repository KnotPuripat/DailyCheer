# Daily Cheer Autopilot

Finds your colleagues on [Aster](https://aster.arise.tech)'s Daily Cheer page and sends
them a heart — so the whole company doesn't have to be rediscovered by hand every day.

Runs entirely on **your own machine**, against **your own** Chrome + wallet. There is no
shared server and no shared account — everyone who wants this runs their own local copy.

📖 **[Read the architecture & flow explainer](https://KnotPuripat.github.io/DailyCheer/)**
for a diagram of how it works before you dig into the code.

## How it works, in short

1. You connect your wallet once, by hand, in a normal Chrome window.
2. A one-time sweep (`full_sweep.py`) discovers which search terms return people.
3. From then on, `daily_cheer_bot.py` (or the web dashboard) re-checks those terms and
   hearts anyone it finds — skipping people it already hearted today, since the site
   resets who's heart-able once every day.

It never opens a new browser tab (that would re-trigger a wallet signature prompt it
can't approve for you), never touches MetaMask itself, and only clicks a Heart button
after confirming — live, on the page — that it isn't already disabled.

## Requirements

- macOS (uses `--remote-debugging-port` with a real, already-installed Chrome)
- Python 3.10+
- [Google Chrome](https://www.google.com/chrome/) with your Aster wallet already set up
- A few Python packages: `pip3 install playwright flask`
  then `playwright install chromium` (one-time, downloads a browser binary used only for
  automation internals — your real Chrome is what actually runs)

## Setup

### 1. Launch Chrome with a debug port

Close all Chrome windows first, then:

```bash
open -a "Google Chrome" --args --remote-debugging-port=9222 \
  --user-data-dir="$HOME/Library/Application Support/Google/Chrome"
```

This reuses your normal Chrome profile (bookmarks, extensions, logins — including
MetaMask) rather than a throwaway one.

### 2. Connect your wallet

In that Chrome window, open `https://aster.arise.tech/daily-cheer/`, click **Connect
wallet**, and approve the MetaMask prompts. This is the only step the scripts here will
never do for you.

### 3. Clone and install

```bash
git clone https://github.com/KnotPuripat/DailyCheer.git
cd DailyCheer
pip3 install playwright flask
playwright install chromium
```

### 4. Discover who's on Aster (once)

```bash
python3 full_sweep.py
```

This tries ~17,500 three-letter search terms against Aster's own search box and records
which ones return people — read-only, it never clicks Heart. Takes a few hours the first
time; safe to stop and re-run later, it picks up where it left off.

### 5. Heart everyone, every day

Either from a terminal:

```bash
python3 daily_cheer_bot.py
```

or with a small local dashboard:

```bash
python3 web_app.py
# then open http://127.0.0.1:5057
```

Run it once a day — hearting resets daily on Aster's side, so running it more than once
a day just confirms there's nothing new to do.

## Files

| File | What it does |
|---|---|
| `full_sweep.py` | One-time, read-only discovery of search terms that return people (and how many — never names) |
| `daily_cheer_bot.py` | The core bot — re-checks known terms, hearts anyone new, self-heals if the page navigates away mid-run |
| `web_app.py` | Local Flask dashboard (`localhost:5057`) with a "Heart Everyone" button, live progress, and pause/resume — counts only, never a name list |

All state (which search terms work, how many people today, how many hearted) lives in
`~/.aster_daily_cheer/` on your machine — nothing here is shared or synced between
people running their own copy.

## Safety boundaries

- **Never opens a new browser tab.** Always attaches to a tab you already have open.
- **Never touches your wallet.** Connecting, signing, approving — always you, always
  manual.
- **Checks before it clicks.** A disabled Heart button is read as "already hearted" and
  skipped.
- **The dashboard is localhost-only.** Bound to `127.0.0.1`; nothing on your network can
  reach it.
- **Stores counts, never names.** Nothing this project writes to disk, prints to a
  console, or shows on the dashboard ever contains anyone's name or email — only how
  many people a search term matched and how many have been hearted. The live page is
  read to decide what to click, but that text never gets written down anywhere.
