# goodreads-tools

I'm a reader who likes to track what I've read. These are a couple of tools I built for myself to scratch that itch, sharing them here in case they're useful to someone else.

Two features:

- **Year in Books report** — a stable script that pulls your Goodreads read shelf and renders a one-page editorial-style summary (HTML / PDF / web PNG / social PNG) covering the **last 12 months on a rolling basis** — no need to wait for Goodreads' traditional year-end recap.
- **Goodreads → StoryGraph sync** — a complete hack to mirror rated books from Goodreads into StoryGraph. StoryGraph doesn't have a public API, so the script drives a real browser session with Selenium. It mostly works.

Only tested on Windows 11, but should work on macOS and Linux with Python and Chrome installed.

> **Tip:** an LLM coding assistant (Claude Code, ChatGPT, Cursor, etc.) is genuinely helpful when installing or debugging this — especially the sync feature, which is browser-driven and can break in interesting ways on new machines. If something doesn't work, paste the error into your assistant of choice.

---

## Feature: Year in Books report

<p align="center">
  <img src="docs/year-in-books-preview.png" alt="Year in Books preview — the 9:16 social card showing masthead, stats, monthly chart, and full book list" width="360">
</p>

Pulls your Goodreads **read shelf** RSS feed, windows the books to the last 12 months from today, looks up genres via the Google Books API (with a Goodreads page-scrape fallback when Google Books comes up empty), then renders an editorial one-page report in several formats:

- `output/year_in_books.html` — the single source of truth, generated from a Jinja2 template
- `output/year_in_books.pdf` — Letter portrait, vector
- `output/year_in_books_web.png` — 1600px wide, full-page render for web embed
- `output/year_in_books_social.png` — 1080×1920 card (9:16), a dense poster format with masthead + stats + chart + the full book list. Sized for Instagram Stories and feed-based social platforms (Bluesky, Mastodon)

Books on your Goodreads shelf with no read date set are skipped (they have no place in a date-windowed view). A warning lists their titles after rendering so you can fix them on Goodreads if you want them included.

Rendering uses Playwright (headless Chromium) to print the HTML to PDF and screenshot it at two breakpoints, so PDF and web output stay perfectly consistent with what you see in a browser.

**Run from the command line:**

```bash
python goodreads_stats.py
```

Optional flags: `--user-id` (override config), `--output-dir DIR` (default `output`), `--config PATH` (default `config.json`).

No StoryGraph credentials are needed — only `goodreads_user_id`.

**Using the web UI:** click *Generate Year in Books*.

<p align="center">
  <img src="docs/web-ui-preview.png" alt="The local web UI after generating a Year in Books report — shows the Generate Year in Books button and the resulting HTML / PDF / Web PNG / Social Card PNG download links" width="500">
</p>

---

## Feature: Goodreads → StoryGraph sync

> This one is a complete hack. StoryGraph doesn't expose a public API, so the only way to push books into it programmatically is to drive a real browser session and click through the UI. The script uses Selenium to do exactly that. It works on my machine; your mileage may vary.

It mirrors books you've **rated** on Goodreads (entries in your updates feed of the form *"gave N stars to…"*) into your StoryGraph reading journal, with the correct completion date. Books you finish without rating won't be picked up.

**Run from the web UI:** click *Sync to StoryGraph*. Chrome will open and you can watch it work. The log streams to the page.

**Run from the command line:**

```bash
python book_sync.py
```

Either way, a `sync_log.txt` file in the project directory captures everything for later inspection.

---

## Prerequisites

- Python 3.8 or higher
- Google Chrome installed (used by both features)
- `pip` available on your `PATH`

## Install

```bash
git clone https://github.com/cruftbox/goodreads-tools.git
cd goodreads-tools
pip install -r requirements.txt
python -m playwright install chromium
```

The last step downloads a headless Chromium build (~150 MB) that the Year in Books report uses to render PDF/PNG output. It's a one-time setup and easy to miss — if the report fails with a Playwright error, this is probably why.

## Configure

Real credentials go in `config.local.json`, not `config.json`. `config.json` is tracked in git and should keep its placeholder values; `config.local.json` is gitignored and overrides `config.json` key-by-key when present, so your real values never end up in git history.

Create `config.local.json` next to `config.json`:

```json
{
    "goodreads_user_id": "YOUR_GOODREADS_USER_ID",
    "storygraph_email": "YOUR_STORYGRAPH_EMAIL",
    "storygraph_password": "YOUR_STORYGRAPH_PASSWORD"
}
```

- **`goodreads_user_id`** — required for both features. Go to your Goodreads profile; the URL looks like `https://www.goodreads.com/user/show/12345678-username`. The leading number is your user ID.
- **`storygraph_email`** / **`storygraph_password`** — only needed for the sync feature. If you only care about the Year in Books report, leave these out.

## Run

The easiest way is the local web UI:

```bash
python app.py
```

This opens `http://127.0.0.1:5000` with one button per feature. The server binds to `127.0.0.1` only — it isn't reachable from other machines on your network.

You can also run either feature directly from the command line — see the per-feature sections above.

## Troubleshooting

- **Year in Books fails with a Playwright / browser error.** You probably skipped `python -m playwright install chromium` during install. Run it.
- **Sync fails to log in.** Re-check `storygraph_email` / `storygraph_password` in `config.local.json`. Check `sync_log.txt` for the actual error. The sync script also drops screenshots (e.g. `login_error.png`, `book_error_*.png`) into the project directory when something goes wrong — those are usually the fastest path to diagnosis.
- **Sync sees zero books to add.** Confirm `goodreads_user_id` is correct, and that the books you expect are actually *rated* on Goodreads (not just finished).
- **Anything else.** Paste the error into an LLM coding assistant. Most install/runtime issues are environment-specific and fall well within what these assistants can debug.

## Safety note

`config.local.json` stores your StoryGraph password in plain text. It's already gitignored, so it won't be committed — keep it private regardless (e.g. don't attach it when sharing your project folder). The included `.gitignore` also excludes generated output.
