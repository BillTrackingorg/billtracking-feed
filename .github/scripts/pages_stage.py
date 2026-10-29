#!/usr/bin/env python3
"""Stage what GitHub Pages serves from this repository, check it, and write the fingerprints.

Run by `.github/workflows/pages.yml` (and, unchanged, by the replay that proved it):

    python3 .github/scripts/pages_stage.py <out_dir>

from the root of a checkout of the commit being deployed.

WHAT IT STAGES. Every tracked file except `.github/`, byte for byte: exactly what the
branch build served before the switch to Actions (`.github/` was never served; checked on
the live host 2026-10-29).

THE BYTE CHECK (Class 2 of the app's loudness doctrine: stop, red, nothing deployed). Each
staged file's git blob id is computed from the staged bytes and compared with the blob the
commit records. A difference means the checkout altered a file (line endings, a filter,
a partial clone); the app's own month cache would then hold bytes the repository never
published, so nothing is deployed and the run goes red.

THE FINGERPRINTS. For each legislature with a month file, `<polity>-fingerprint.json`:

    {"v": 1, "polity": "us", "commit": "<sha>",
     "files": [{"name": "us-2026-07.jsonl", "bytes": 55790, "posts": 61, "sha256": "…"}, …,
               {"name": "us-overrides.jsonl", "bytes": 586, "sha256": "…"}]}

`files` lists every month file of the legislature and its corrections sheet, sorted by name.
`posts` is the month file's non-blank lines that are not summary lines (the app's
`SUMMARY_LINE_TYPES`, `src/data/feed-schema.ts`); the app may still drop a line it cannot
adapt, so it is a count for planning a load, never a claim about the record. Readers ignore
fields they do not know, so a field can be added without a new `v`.

HEADROOM. GitHub Pages sites are limited to 1 GB. At 70% of it the run carries a warning,
at 90% an error annotation; neither stops the deploy, because a stopped deploy is worse
than a full one (the hosting-splits row in the app's OPEN-ITEMS is the plan).
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

# The app's `SUMMARY_LINE_TYPES` (src/data/feed-schema.ts). A new type there is added here.
SUMMARY_LINE_TYPES = {"summary_fill", "summary_match"}
MONTH = re.compile(r"^([a-z]+)-(\d{4})-(\d{2})\.jsonl$")
SHEET = re.compile(r"^([a-z]+)-overrides\.jsonl$")
SITE_LIMIT = 1024 ** 3


def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


def blob_id(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def count_posts(data):
    n = 0
    for line in data.decode("utf-8").split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            n += 1          # an unreadable line is still a line the app will try, and drop
            continue
        if not (isinstance(rec, dict) and rec.get("type") in SUMMARY_LINE_TYPES):
            n += 1
    return n


def main(out):
    commit = git("rev-parse", "HEAD").decode().strip()
    recorded = {}
    for row in git("ls-tree", "-r", "-z", "HEAD").split(b"\0"):
        if not row:
            continue
        meta, path = row.split(b"\t", 1)
        mode, kind, oid = meta.decode().split(" ")
        path = path.decode("utf-8")
        if kind != "blob" or path.startswith(".github/"):
            continue
        if mode == "120000":
            print(f"::error::{path} is a symbolic link; Pages serves files only. Nothing deployed.")
            return 2
        recorded[path] = oid

    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(out)
    mismatched = []
    total = 0
    data_of = {}
    for path, oid in sorted(recorded.items()):
        with open(path, "rb") as f:
            data = f.read()
        if blob_id(data) != oid:
            mismatched.append(path)
            continue
        dest = os.path.join(out, path)
        os.makedirs(os.path.dirname(dest) or out, exist_ok=True)
        with open(dest, "wb") as f:
            f.write(data)
        total += len(data)
        if "/" not in path:
            data_of[path] = data

    if mismatched:
        print(f"::error::{len(mismatched)} file(s) in this checkout differ from the bytes commit "
              f"{commit[:9]} records, so the site is not deployed: {', '.join(mismatched[:10])}. "
              "The checkout altered them (line endings, a filter); the live site still serves the last deploy.")
        return 2

    # CLEAN UTF-8, NO BYTE-ORDER MARK. The app checks a file it holds as text against the
    # fingerprint's sha256 by encoding that text again: a BOM, which a browser drops, or a
    # byte that is not UTF-8, which it replaces, would make the two never agree, and a
    # device would keep its old copy of that file for good. So such a file is refused here,
    # red, before anything deploys (a hand edit saved by PowerShell 5.1 carries a BOM).
    unclean = []
    for name in sorted(data_of):
        if MONTH.match(name) or SHEET.match(name):
            data = data_of[name]
            try:
                data.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                unclean.append(f"{name} (not UTF-8)")
                continue
            if data.startswith(b"\xef\xbb\xbf"):
                unclean.append(f"{name} (starts with a byte-order mark)")
    if unclean:
        print(f"::error::{', '.join(unclean)}: a month file or corrections sheet must be plain UTF-8 with no "
              "byte-order mark, or the app cannot check it against its fingerprint. Nothing deployed; the live "
              "site still serves the last deploy. Save the file again as UTF-8 without a BOM.")
        return 2

    polities = sorted({m.group(1) for name in data_of if (m := MONTH.match(name))})
    for polity in polities:
        files = []
        for name in sorted(data_of):
            month, sheet = MONTH.match(name), SHEET.match(name)
            if (month and month.group(1) == polity) or (sheet and sheet.group(1) == polity):
                data = data_of[name]
                entry = {"name": name, "bytes": len(data)}
                if month:
                    entry["posts"] = count_posts(data)
                entry["sha256"] = hashlib.sha256(data).hexdigest()
                files.append(entry)
        doc = {"v": 1, "polity": polity, "commit": commit, "files": files}
        name = f"{polity}-fingerprint.json"
        if name in recorded:
            print(f"::error::{name} is committed to the repository; the deploy writes it. Nothing deployed.")
            return 2
        blob = (json.dumps(doc, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
        with open(os.path.join(out, name), "wb") as f:
            f.write(blob)
        total += len(blob)
        print(f"{name}: {len(files)} files, commit {commit[:9]}")

    share = total / SITE_LIMIT
    print(f"staged {len(recorded)} files, {total:,} bytes ({share:.1%} of the 1 GB a Pages site may hold)")
    if share >= 0.9:
        print(f"::error::The feed site is {share:.0%} of GitHub's 1 GB Pages limit. Deploying anyway; "
              "the hosting splits in the app's OPEN-ITEMS are now due.")
    elif share >= 0.7:
        print(f"::warning::The feed site is {share:.0%} of GitHub's 1 GB Pages limit; plan the hosting split "
              "(the app's OPEN-ITEMS).")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: pages_stage.py <out_dir>", file=sys.stderr)
        sys.exit(64)
    sys.exit(main(sys.argv[1]))
