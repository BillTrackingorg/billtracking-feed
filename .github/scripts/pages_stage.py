#!/usr/bin/env python3
"""Stage what GitHub Pages serves from this repository, check it, and write the fingerprints.

Run by `.github/workflows/pages.yml`:

    python3 .github/scripts/pages_stage.py <out_dir>

from the root of a checkout of the commit being deployed. Exit 0: staged. Exit 2: refused,
deploy nothing. Exit 3: staged, but a legislature goes out without its fingerprint (below);
deploy, then turn the run red.

PROOF. The replay of this script over every commit of main's history, and each refusal shown
on a mutated scratch commit, are recorded in the message of the last commit that changed it
(`git log -1 -- .github/scripts/pages_stage.py`); a change to it runs both again.

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
     "files": [{"name": "us-2026-07.jsonl", "bytes": 55790, "posts": 41, "sha256": "…"}, …,
               {"name": "us-overrides.jsonl", "bytes": 586, "sha256": "…"}]}

`files` lists every month file of the legislature and its corrections sheet, sorted by name.
`posts` is the month file's non-blank lines that are not summary lines (the app's
`SUMMARY_LINE_TYPES`, `src/data/feed-schema.ts`); the app may still drop a line it cannot
adapt, so it is a count for planning a load, never a claim about the record. Readers ignore
fields they do not know, so a field can be added without a new `v`.

A FILE THE APP COULD NOT CHECK (exit 3: deployed, then red). A month file or corrections
sheet that is not UTF-8, or starts with a byte-order mark, leaves its legislature without a
fingerprint; every other legislature's is written. A fingerprint must list every file (the
app never asks for one it does not list), and the app checks a file by hashing the text its
decoder gave it: a browser's decoder drops a leading BOM, the phones' have not been checked,
and a byte that is not UTF-8 is replaced in ways that differ by decoder. A sha of the
decoded text could therefore match on one platform and never on another, which would
download that file again at every load. Without a fingerprint, the app reads that
legislature's files the way it did before fingerprints, on every platform. The error names
the source to fix: `feed/<name without the prefix>` in that legislature's bot repository,
which copies its `feed/*.jsonl` here at every publish, so a fix made in this repository is
undone by the next one.

HEADROOM. GitHub Pages sites are limited to 1 GB. At 70% of it the run carries a warning,
at 90% an error annotation; neither stops the deploy, because a stopped deploy is worse
than a full one (the hosting-splits row in the app's OPEN-ITEMS is the plan).

THE SEARCH FILES, IN SHADOW (the app's search design of 2026-10-29, §5; app OPEN-ITEMS
OI-0108). A second run, after the stage:

    python3 .github/scripts/pages_stage.py --derive <out_dir> <as_of> <work_dir> <tools_dir>

It copies the staged site to `<work_dir>/in`, so no builder is handed the folder that is
served (the guard read of 2026-10-29: a builder with write access could change a served
month's bytes on a green run); runs each builder (`<tools_dir>/bt-feed-<digest>.cjs`, the
app's own core compiled per derivation digest) on the copy, which writes its legislatures'
search files under `<work_dir>/out/search/`, proves them, and names each file's bytes and
sha256 in `<work_dir>/out/<digest>.json`; then, as `--add-derived <out_dir> <derived_dir>`
does alone, checks every file against that manifest, copies it into the staged site beside
the record (never over a file that differs), and adds to the legislature's fingerprint the
digest's entry:

    "derived": {"<digest>": {"list": "search/l/<h64>.json", "bytes": 511, "h64": "…", "corrections": 1}}

An entry it cannot check is left out with an error annotation (Class 1: the run stays green,
the record and the fingerprint's record rows deploy as staged); every entry is checked before
any file is placed, and a fingerprint is rewritten whole or not at all. No app or website reads
these files yet: today's readers pass over the `derived` block (the app's check:searchfiles
runs its client over it). Always exit 0, whatever a manifest holds. `pages_replay.py` replays
`--derive` itself, so the replay is the workflow's run.
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

    polities = sorted({m.group(1) for name in data_of if (m := MONTH.match(name))})
    for polity in polities:
        if f"{polity}-fingerprint.json" in recorded:
            print(f"::error::{polity}-fingerprint.json is committed to the repository; the deploy writes it. "
                  "Nothing deployed.")
            return 2

    # PLAIN UTF-8, NO BYTE-ORDER MARK, or the legislature goes out without its fingerprint
    # (the docstring says why). Its files still deploy; the run goes red after the deploy
    # (a hand edit saved by PowerShell 5.1 carries a BOM).
    unchecked = set()
    for name in sorted(data_of):
        m = MONTH.match(name) or SHEET.match(name)
        if not m:
            continue
        data = data_of[name]
        try:
            data.decode("utf-8", errors="strict")
            why = "starts with a byte-order mark" if data.startswith(b"\xef\xbb\xbf") else None
        except UnicodeDecodeError:
            why = "is not UTF-8"
        if why:
            polity = m.group(1)
            unchecked.add(polity)
            print(f"::error::{name} {why}, so the app could not check it against a fingerprint: "
                  f"{polity}-fingerprint.json is not deployed, and the app reads {polity}'s files without one. "
                  f"Fix the source, feed/{name[len(polity) + 1:]} in the {polity} bot's repository (save it as "
                  "UTF-8 without a byte-order mark): the bot copies its feed/*.jsonl here at every publish, so a "
                  "fix made in this repository is undone by the next one. Every other legislature is deployed.")

    for polity in polities:
        if polity in unchecked:
            print(f"{polity}-fingerprint.json: not written (a file above could not be checked)")
            continue
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
    return 3 if unchecked else 0


DERIVED_PATH = re.compile(r"^search/[lbrpc]/[0-9a-f]{16}\.jsonl?$")
DIGEST_MANIFEST = re.compile(r"^([0-9a-f]{16})\.json$")


def place(scratch, dest, data):
    """Write `data` at `dest` whole or not at all: written in `scratch` (beside the builders'
    files, never served), then moved into place, so a step stopped by its time limit leaves no
    half-written file in the site."""
    tmp = os.path.join(scratch, ".placing")
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, dest)


def tree_bytes(root):
    total = 0
    for base, _dirs, files in os.walk(root):
        for name in files:
            total += os.path.getsize(os.path.join(base, name))
    return total


def check_entry(derived, out, entry):
    """The files of one manifest entry, checked: (polity, block, [(src, dest, size)]), or the
    reason it cannot be used."""
    polity = entry.get("polity")
    if not isinstance(polity, str) or not re.match(r"^[a-z]+$", polity):
        return None, "an entry names no legislature"
    if not os.path.exists(os.path.join(out, f"{polity}-fingerprint.json")):
        return None, f"{polity} has no fingerprint in this deploy (the stage wrote none)"
    files = entry.get("files")
    if not isinstance(files, list) or not files:
        return None, f"{polity}'s entry lists no files"
    plan = []
    sizes = {}
    for f in files:
        path = f.get("path") if isinstance(f, dict) else None
        if not isinstance(path, str) or not DERIVED_PATH.match(path):
            return None, f"{polity}'s entry names a file outside search/: {path!r}"
        with open(os.path.join(derived, path), "rb") as fh:
            data = fh.read()
        if len(data) != f.get("bytes") or hashlib.sha256(data).hexdigest() != f.get("sha256"):
            return None, f"{path} is not the file its builder proved"
        dest = os.path.join(out, path)
        if os.path.exists(dest):
            with open(dest, "rb") as fh:
                if fh.read() != data:
                    return None, f"{path} would overwrite a different file"
        plan.append((data, dest))
        sizes[path] = len(data)
    if not isinstance(entry.get("list"), str) or entry["list"] not in sizes or sizes[entry["list"]] != entry.get("bytes"):
        return None, f"{polity}'s list is not among its files, or not of the size named"
    if not isinstance(entry.get("h64"), str) or not entry["list"].endswith(f"/{entry['h64']}.json"):
        return None, f"{polity}'s list is not named by its hash"
    if not isinstance(entry.get("corrections"), int) or entry["corrections"] < 0:
        return None, f"{polity}'s corrections count is not a count"
    block = {"list": entry["list"], "bytes": entry["bytes"], "h64": entry["h64"], "corrections": entry["corrections"]}
    return (polity, block, plan), None


def add_derived(out, derived):
    record_total = tree_bytes(out)
    blocks = {}
    added = 0
    accepted = []
    names = sorted(os.listdir(derived)) if os.path.isdir(derived) else []
    for name in names:
        m = DIGEST_MANIFEST.match(name)
        if not m:
            continue
        digest = m.group(1)
        try:
            with open(os.path.join(derived, name), encoding="utf-8") as fh:
                doc = json.load(fh)
            if not isinstance(doc, dict) or doc.get("v") != 1 or doc.get("dd") != digest \
                    or not isinstance(doc.get("entries"), list):
                raise ValueError("not a builder's manifest")
        except (OSError, ValueError) as e:
            print(f"::error::search files under digest {digest} are left out: {name} could not be read ({e}).")
            continue
        for entry in doc["entries"]:
            try:
                checked, why = check_entry(derived, out, entry if isinstance(entry, dict) else {})
            except Exception as e:   # a manifest is a builder's bytes: nothing in one may stop the deploy
                checked, why = None, f"its entry could not be read ({type(e).__name__}: {e})"
            if not checked:
                print(f"::error::search files under digest {digest} are left out of one fingerprint: {why}. "
                      "The record deploys as staged.")
                continue
            accepted.append((digest, checked))

    # Every entry is checked before any file is placed, so a manifest that fails part-way leaves no
    # file of it in the site unlisted. Placing is checked again, entry by entry: an entry placed
    # before may have written a file this one names with other bytes.
    def differs(dest, data):
        if not os.path.exists(dest):
            return False
        with open(dest, "rb") as fh:
            return fh.read() != data

    for digest, (polity, block, plan) in accepted:
        if any(differs(dest, data) for data, dest in plan):
            print(f"::error::search files under digest {digest} are left out of one fingerprint: {polity}'s entry "
                  "names a file another entry placed with other bytes. The record deploys as staged.")
            continue
        for data, dest in plan:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if not os.path.exists(dest):
                place(derived, dest, data)
                added += len(data)
        blocks.setdefault(polity, {})[digest] = block

    for polity in sorted(blocks):
        path = os.path.join(out, f"{polity}-fingerprint.json")
        with open(path, "rb") as fh:
            doc = json.loads(fh.read().decode("utf-8"))
        doc["derived"] = {d: blocks[polity][d] for d in sorted(blocks[polity])}
        place(derived, path, (json.dumps(doc, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8"))
        print(f"{polity}-fingerprint.json: derived {', '.join(sorted(blocks[polity]))}")

    total = record_total + added
    print(f"search files: {added:,} bytes added; the site is {total / SITE_LIMIT:.1%} of the 1 GB a Pages site may hold")
    for limit, kind in ((0.9, "error"), (0.7, "warning")):
        if total / SITE_LIMIT >= limit > record_total / SITE_LIMIT:
            print(f"::{kind}::With its search files the feed site is {total / SITE_LIMIT:.0%} of GitHub's 1 GB Pages "
                  "limit (the hosting splits in the app's OPEN-ITEMS).")
            break
    return 0


BUILDER = re.compile(r"^bt-feed-[0-9a-f]{16}\.cjs$")


def derive(out, asof, work, tools):
    """The search files' run, as the workflow makes it and `pages_replay.py` replays it: the staged
    site copied to `<work>/in`, each builder in `tools` deriving from the copy into `<work>/out`,
    then `add_derived` over what they wrote. A builder that fails leaves its files out."""
    src, dst = os.path.join(work, "in"), os.path.join(work, "out")
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(out, src)
    os.makedirs(dst)
    for name in sorted(n for n in os.listdir(tools) if BUILDER.match(n)) if os.path.isdir(tools) else []:
        sys.stdout.flush()
        if subprocess.run(["node", os.path.join(tools, name), "derive", src, asof, dst]).returncode != 0:
            print(f"::error::{name} did not finish; its search files are left out of this deploy, "
                  "and the record deploys as staged.")
    return add_derived(out, dst)


def never_fatal(run):
    """The search files never stop a deploy: anything that escapes is named, and the exit is 0."""
    try:
        return run()
    except Exception as e:
        print(f"::error::the search files are left out of this deploy ({type(e).__name__}: {e}); "
              "the record deploys as staged.")
        return 0


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--add-derived":
        sys.exit(never_fatal(lambda: add_derived(sys.argv[2], sys.argv[3])))
    if len(sys.argv) == 6 and sys.argv[1] == "--derive":
        sys.exit(never_fatal(lambda: derive(*sys.argv[2:6])))
    if len(sys.argv) != 2:
        print("usage: pages_stage.py <out_dir> | --add-derived <out_dir> <derived_dir>"
              " | --derive <out_dir> <as_of> <work_dir> <tools_dir>", file=sys.stderr)
        sys.exit(64)
    sys.exit(main(sys.argv[1]))
