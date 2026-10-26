#!/usr/bin/env python3
"""Restore BookHive read dates from a Goodreads library export.

After a Goodreads CSV import into BookHive, books that needed manual matching
ended up with finishedAt set to the import day (2026-10-25). This script finds
the buzz.bookhive.book records in your PDS carrying that date, matches each one
to its row in the Goodreads export, and rewrites finishedAt to the export's
Date Read. Nothing else in a record is changed.

Requirements: Python 3.9+, standard library only.

Credentials come from environment variables (use an app password, not your
account password):
    BSKY_HANDLE           e.g. cruftbox.com
    BSKY_APP_PASSWORD     xxxx-xxxx-xxxx-xxxx

Usage:
    python fix_bookhive_read_dates.py goodreads_library_export.csv             # dry run
    python fix_bookhive_read_dates.py goodreads_library_export.csv --apply --limit 3
    python fix_bookhive_read_dates.py goodreads_library_export.csv --apply

Dry run is the default and writes nothing to the PDS. Every run writes a report
(fix_read_dates_report.csv). An --apply run first saves the original records
to fix_read_dates_backup_<timestamp>.json, then updates each record with
swapRecord so a record changed elsewhere since it was read is left alone.

Records are left untouched when:
  - no export row matches, or more than one matches with different dates
  - the matching export row has no Date Read
  - the export's Date Read is the same as the bad date
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

COLLECTION = "buzz.bookhive.book"
HTTP_TIMEOUT = 30
WRITE_DELAY = 0.5  # seconds between writes


# ---------------------------------------------------------------- HTTP / PDS

def _request(method: str, url: str, *, params=None, body=None, token=None) -> dict:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise RuntimeError(f"{method} {url} -> HTTP {e.code}: {detail}") from None


def resolve_pds(handle: str) -> tuple[str, str]:
    """handle -> (did, PDS base URL)."""
    did = _request("GET", "https://public.api.bsky.app/xrpc/com.atproto.identity.resolveHandle",
                   params={"handle": handle})["did"]
    if did.startswith("did:plc:"):
        doc = _request("GET", f"https://plc.directory/{did}")
    elif did.startswith("did:web:"):
        doc = _request("GET", f"https://{did.removeprefix('did:web:')}/.well-known/did.json")
    else:
        raise RuntimeError(f"Unsupported DID method: {did}")
    for svc in doc.get("service", []):
        if svc.get("id") in ("#atproto_pds", f"{did}#atproto_pds"):
            return did, svc["serviceEndpoint"].rstrip("/")
    raise RuntimeError(f"No PDS endpoint in DID document for {did}")


class Pds:
    def __init__(self, handle: str, app_password: str):
        self.handle, self.password = handle, app_password
        self.did, self.base = resolve_pds(handle)
        self._login()

    def _login(self) -> None:
        s = _request("POST", f"{self.base}/xrpc/com.atproto.server.createSession",
                     body={"identifier": self.handle, "password": self.password})
        self.token = s["accessJwt"]

    def _call(self, method: str, nsid: str, **kw) -> dict:
        try:
            return _request(method, f"{self.base}/xrpc/{nsid}", token=self.token, **kw)
        except RuntimeError as e:
            if "ExpiredToken" not in str(e):
                raise
            self._login()
            return _request(method, f"{self.base}/xrpc/{nsid}", token=self.token, **kw)

    def list_books(self) -> list[dict]:
        out, cursor = [], None
        while True:
            params = {"repo": self.did, "collection": COLLECTION, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            page = self._call("GET", "com.atproto.repo.listRecords", params=params)
            out.extend(page.get("records", []))
            cursor = page.get("cursor")
            if not cursor or not page.get("records"):
                return out

    def put_book(self, rkey: str, record: dict, swap_cid: str) -> None:
        self._call("POST", "com.atproto.repo.putRecord", body={
            "repo": self.did, "collection": COLLECTION, "rkey": rkey,
            "record": record, "swapRecord": swap_cid,
        })


# ---------------------------------------------------------------- matching

def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def norm_title(title: str) -> str:
    t = _fold(title)
    t = re.sub(r"\s*\([^)]*\)\s*$", "", t)     # trailing "(Series, #3)" / "(Series Book 2)"
    t = t.split(":")[0]                            # drop subtitle
    t = re.sub(r"^(the|a|an)\s+", "", t.strip())
    return re.sub(r"[^a-z0-9]", "", t)


def norm_author(author: str) -> str:
    return re.sub(r"[^a-z]", "", _fold(author))


def clean_isbn(v: str) -> str:
    return re.sub(r"[^0-9Xx]", "", v or "").upper()


class Export:
    def __init__(self, path: Path):
        with path.open(encoding="utf-8-sig", newline="") as f:
            self.rows = list(csv.DictReader(f))
        self.by_gr, self.by_isbn, self.by_title = {}, {}, {}
        for r in self.rows:
            r["_isbn10"], r["_isbn13"] = clean_isbn(r.get("ISBN")), clean_isbn(r.get("ISBN13"))
            self.by_gr.setdefault(r["Book Id"].strip(), []).append(r)
            for i in (r["_isbn10"], r["_isbn13"]):
                if i:
                    self.by_isbn.setdefault(i, []).append(r)
            self.by_title.setdefault(norm_title(r["Title"]), []).append(r)

    def match(self, rec: dict) -> tuple[list[dict], str]:
        """Return (candidate rows, method)."""
        ids = rec.get("identifiers") or {}
        gr = str(ids.get("goodreadsId") or "").strip()
        if gr and gr in self.by_gr:
            return self.by_gr[gr], "goodreadsId"
        for key in ("isbn13", "isbn10"):
            i = clean_isbn(ids.get(key) or "")
            if i and i in self.by_isbn:
                return self.by_isbn[i], key
        rec_authors = [norm_author(a) for a in (rec.get("authors") or "").split("\t") if a.strip()]
        hits = [
            r for r in self.by_title.get(norm_title(rec.get("title") or ""), [])
            if not rec_authors or any(
                norm_author(r["Author"]) in a or a in norm_author(r["Author"]) for a in rec_authors)
        ]
        return hits, "title+author"


def export_date(row: dict) -> str | None:
    """Goodreads 'YYYY/MM/DD' -> 'YYYY-MM-DD', or None."""
    v = (row.get("Date Read") or "").strip()
    if not v:
        return None
    return datetime.strptime(v, "%Y/%m/%d").strftime("%Y-%m-%d")


# ---------------------------------------------------------------- main

def local_date(iso: str, offset_hours: float) -> str:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return dt.astimezone(timezone(timedelta(hours=offset_hours))).strftime("%Y-%m-%d")


def run(args) -> int:
    handle, password = os.environ.get("BSKY_HANDLE"), os.environ.get("BSKY_APP_PASSWORD")
    if not handle or not password:
        print("Set BSKY_HANDLE and BSKY_APP_PASSWORD.", file=sys.stderr)
        return 2

    export = Export(Path(args.csv))
    pds = Pds(handle, password)
    records = pds.list_books()
    print(f"PDS {pds.did}: {len(records)} BookHive records; export: {len(export.rows)} rows")

    bad = [r for r in records
           if r["value"].get("finishedAt")
           and local_date(r["value"]["finishedAt"], args.utc_offset) == args.bad_date]
    print(f"Records with finishedAt on {args.bad_date}: {len(bad)}")

    report, plan = [], []
    for rec in bad:
        v = rec["value"]
        rows, method = export.match(v)
        dates = sorted({export_date(r) for r in rows} - {None})
        line = {"title": v.get("title"), "authors": (v.get("authors") or "").replace("\t", "; "),
                "rkey": rec["uri"].rsplit("/", 1)[-1], "old_finishedAt": v["finishedAt"],
                "match": method if rows else "", "export_title": rows[0]["Title"] if rows else "",
                "new_date": "", "action": ""}
        if not rows:
            line["action"] = "skip: no match in export"
        elif len(dates) > 1:
            line["action"] = f"skip: ambiguous ({', '.join(dates)})"
        elif not dates:
            line["action"] = "skip: no Date Read in export"
        elif dates[0] == args.bad_date:
            line["action"] = "skip: export date equals bad date"
        else:
            line["new_date"] = dates[0]
            line["action"] = "update"
            plan.append((rec, dates[0], line))
        report.append(line)

    if args.limit is not None:
        for _, _, line in plan[args.limit:]:
            line["action"] = "update (deferred by --limit)"
        plan = plan[: args.limit]

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if args.apply and plan:
        backup = Path(f"fix_read_dates_backup_{stamp}.json")
        backup.write_text(json.dumps([r for r, _, _ in plan], indent=2), encoding="utf-8")
        print(f"Backed up {len(plan)} records to {backup}")

    done = failed = 0
    for rec, date, line in plan:
        new = dict(rec["value"])
        new["finishedAt"] = f"{date}T12:00:00.000Z"  # noon UTC: same calendar day across US zones
        if not args.apply:
            line["action"] = "would update"
            continue
        try:
            pds.put_book(line["rkey"], new, rec["cid"])
            line["action"] = "updated"
            done += 1
        except RuntimeError as e:
            line["action"] = f"FAILED: {e}"
            failed += 1
        time.sleep(WRITE_DELAY)

    out = Path("fix_read_dates_report.csv")
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(report[0].keys()) if report else ["title"])
        w.writeheader()
        w.writerows(sorted(report, key=lambda x: (x["action"], x["title"] or "")))

    counts: dict[str, int] = {}
    for line in report:
        key = line["action"].split(":")[0].split(" (")[0]
        counts[key] = counts.get(key, 0) + 1
    print("Summary:", ", ".join(f"{k}: {n}" for k, n in sorted(counts.items())) or "nothing to do")
    print(f"Report: {out}")
    if not args.apply:
        print("Dry run only. Re-run with --apply to write.")
    return 1 if failed else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", help="Goodreads library export CSV")
    ap.add_argument("--apply", action="store_true", help="write changes (default is a dry run)")
    ap.add_argument("--limit", type=int, help="update at most N records")
    ap.add_argument("--bad-date", default="2026-10-25", help="the wrong date to look for (YYYY-MM-DD)")
    ap.add_argument("--utc-offset", type=float, default=-7,
                    help="hours from UTC used to read finishedAt as a calendar date (PDT = -7)")
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
