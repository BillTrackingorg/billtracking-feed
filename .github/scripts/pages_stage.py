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

Each builder (`<tools_dir>/bt-feed-<digest>.cjs`, the app's own core compiled per derivation
digest) gets a copy of the staged site of its own, `<work_dir>/<builder>/in`, and runs there,
with no path to the folder that is served (the guard read of 2026-10-29: a builder with write
access could change a served month's bytes on a green run), writing its legislatures' search
files under `<work_dir>/<builder>/out/search/`, proving them, and naming each file's bytes and
sha256 in `<work_dir>/<builder>/out/<digest>.json`. The builders together get BUILD_SECONDS,
each killed at the deadline. Then, for the builders that finished, as `--add-derived <out_dir>
<derived_dir>` does for one folder, it checks every file against its manifest, copies it into
the staged site beside the record (never over a file that differs), and adds to the
legislature's fingerprint the digest's entry:

    "derived": {"<digest>": {"list": "search/l/<h64>.json", "bytes": 511, "h64": "…", "corrections": 1}}

An entry it cannot check is left out with an error annotation (Class 1: the run stays green,
the record and the fingerprint's record rows deploy as staged); every entry is checked before
any file is placed, and a fingerprint is rewritten whole or not at all. A builder that does not
finish, writes no manifest, or gives a fingerprinted legislature no set is named; so is a step
with no builder. Last, the site outside `search/` is checked against what the stage wrote, and
if anything differs it is staged again from the commit, with no search files. No app or website
reads these files yet: today's readers pass over the `derived` block (the app's
check:searchfiles runs its client over it). Always exit 0, whatever a builder or a manifest
does. `pages_replay.py` replays `--derive` itself, so the replay is the workflow's run.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

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


DERIVED_PATH = re.compile(r"search/[lbrpc]/[0-9a-f]{16}\.jsonl?")
DIGEST_MANIFEST = re.compile(r"([0-9a-f]{16})\.json")
H64 = re.compile(r"[0-9a-f]{16}")
FINGERPRINT = re.compile(r"[a-z]+-fingerprint\.json")
BUILDER = re.compile(r"bt-feed-([0-9a-f]{16})\.cjs")

# THE SEARCH FILES' CLOCK: the builders together get BUILD_SECONDS, each killed at the deadline,
# and the step's own command is cut at 105 s by `timeout` (pages.yml) before its two-minute
# limit, because Actions' limit stops only the step's shell and would leave python and node
# writing into the site while the upload reads it. INVENTED: today both legislatures build in
# about 1 s.
BUILD_SECONDS = 90


def place(scratch, dest, data):
    """Write `data` at `dest` whole or not at all: written to a new file in `scratch` (created
    here, never one a builder could have left), then moved into place, so a step stopped part-way
    leaves no half-written file in the site."""
    fd, tmp = tempfile.mkstemp(dir=scratch, prefix=".placing-")
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.replace(tmp, dest)


def tree_bytes(root):
    total = 0
    for base, _dirs, files in os.walk(root):
        for name in files:
            total += os.path.getsize(os.path.join(base, name))
    return total


def is_count(v):
    return type(v) is int and v >= 0


def check_entry(derived, out, entry):
    """The files of one manifest entry, checked: (polity, block, [(data, dest)]), or the reason it
    cannot be used."""
    polity = entry.get("polity")
    if not isinstance(polity, str) or not re.fullmatch(r"[a-z]+", polity):
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
        if not isinstance(path, str) or not DERIVED_PATH.fullmatch(path):
            return None, f"{polity}'s entry names a file outside search/: {path!r}"
        with open(os.path.join(derived, path), "rb") as fh:
            data = fh.read()
        if not is_count(f.get("bytes")) or len(data) != f["bytes"] or hashlib.sha256(data).hexdigest() != f.get("sha256"):
            return None, f"{path} is not the file its builder proved"
        dest = os.path.join(out, path)
        if os.path.exists(dest):
            with open(dest, "rb") as fh:
                if fh.read() != data:
                    return None, f"{path} would overwrite a different file"
        plan.append((data, dest))
        sizes[path] = len(data)
    h64 = entry.get("h64")
    if not isinstance(h64, str) or not H64.fullmatch(h64) or entry.get("list") != f"search/l/{h64}.json":
        return None, f"{polity}'s list is not named by its hash"
    if entry["list"] not in sizes or not is_count(entry.get("bytes")) or sizes[entry["list"]] != entry["bytes"]:
        return None, f"{polity}'s list is not among its files, or not of the size named"
    if not is_count(entry.get("corrections")):
        return None, f"{polity}'s corrections count is not a count"
    block = {"list": entry["list"], "bytes": entry["bytes"], "h64": h64, "corrections": entry["corrections"]}
    return (polity, block, plan), None


def read_manifests(derived, out, only=None):
    """Every entry the builders' manifests in `derived` hold, checked: [(digest, polity, block,
    plan)], each failure named. `only`, a digest, is the one manifest a builder of that digest
    may write."""
    accepted = []
    names = sorted(os.listdir(derived)) if os.path.isdir(derived) else []
    for name in names:
        m = DIGEST_MANIFEST.fullmatch(name)
        if not m or (only and m.group(1) != only):
            continue
        digest = m.group(1)
        try:
            with open(os.path.join(derived, name), encoding="utf-8") as fh:
                doc = json.load(fh)
            if not isinstance(doc, dict) or doc.get("v") != 1 or doc.get("dd") != digest \
                    or not isinstance(doc.get("entries"), list):
                raise ValueError("not a builder's manifest")
        except Exception as e:   # a manifest is a builder's bytes: nothing in one may stop the deploy
            print(f"::error::search files under digest {digest} are left out: {name} could not be read "
                  f"({type(e).__name__}: {e}).")
            continue
        seen = set()
        for entry in doc["entries"]:
            try:
                checked, why = check_entry(derived, out, entry if isinstance(entry, dict) else {})
            except Exception as e:
                checked, why = None, f"its entry could not be read ({type(e).__name__}: {e})"
            if checked and checked[0] in seen:
                checked, why = None, f"a second entry for {checked[0]} in one manifest"
            if not checked:
                print(f"::error::search files under digest {digest} are left out of one fingerprint: {why}. "
                      "The record deploys as staged.")
                continue
            seen.add(checked[0])
            accepted.append((digest, derived, *checked))
    return accepted


def add_derived(out, derived_dirs, expected=None):
    """Place the checked files of every entry under `derived_dirs` and add each fingerprint's
    `derived` block. `expected`, the digest of each folder's builder: a builder that gave a
    fingerprinted legislature no set is named."""
    record_total = tree_bytes(out)
    accepted = []
    for d in derived_dirs:
        got = read_manifests(d, out, expected.get(d) if expected else None)
        if expected and not os.path.exists(os.path.join(d, f"{expected[d]}.json")):
            print(f"::error::bt-feed-{expected[d]}.cjs finished without writing its manifest; its search files are "
                  "left out of this deploy, and the record deploys as staged.")
        accepted.extend(got)

    # Every entry is checked before any file is placed, so a manifest that fails part-way leaves no
    # file of it in the site unlisted. Placing is checked again, entry by entry: an entry placed
    # before may have written a file this one names with other bytes.
    def differs(dest, data):
        if not os.path.exists(dest):
            return False
        with open(dest, "rb") as fh:
            return fh.read() != data

    blocks = {}
    added = 0
    for digest, derived, polity, block, plan in accepted:
        if digest in blocks.get(polity, {}):
            print(f"::error::search files under digest {digest} are left out of one fingerprint: a second set for "
                  f"{polity}. The record deploys as staged.")
            continue
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

    prints = sorted(n for n in os.listdir(out) if FINGERPRINT.fullmatch(n))
    for name in prints:
        polity = name[: -len("-fingerprint.json")]
        for digest in sorted(set((expected or {}).values())):
            if digest not in blocks.get(polity, {}):
                print(f"::error::bt-feed-{digest}.cjs gave {polity} no search files this deploy (the lines above say "
                      "why); its record deploys as staged.")
        if polity not in blocks:
            continue
        path = os.path.join(out, name)
        with open(path, "rb") as fh:
            doc = json.loads(fh.read().decode("utf-8"))
        doc["derived"] = {d: blocks[polity][d] for d in sorted(blocks[polity])}
        place(derived_dirs[0], path, (json.dumps(doc, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8"))
        print(f"{name}: derived {', '.join(sorted(blocks[polity]))}")

    total = record_total + added
    print(f"search files: {added:,} bytes added; the site is {total / SITE_LIMIT:.1%} of the 1 GB a Pages site may hold")
    for limit, kind in ((0.9, "error"), (0.7, "warning")):
        if total / SITE_LIMIT >= limit > record_total / SITE_LIMIT:
            print(f"::{kind}::With its search files the feed site is {total / SITE_LIMIT:.0%} of GitHub's 1 GB Pages "
                  "limit (the hosting splits in the app's OPEN-ITEMS).")
            break
    return 0


def record_state(out):
    """What the stage put in the site, to know it is unchanged: every file outside `search/` by
    its sha256, and each fingerprint by its content less the `derived` block."""
    state = {}
    for base, _dirs, files in os.walk(out):
        for n in files:
            p = os.path.join(base, n)
            rel = os.path.relpath(p, out).replace(os.sep, "/")
            if rel.startswith("search/"):
                continue
            with open(p, "rb") as fh:
                data = fh.read()
            if FINGERPRINT.fullmatch(rel):
                doc = json.loads(data.decode("utf-8"))
                doc.pop("derived", None)
                state[rel] = json.dumps(doc, sort_keys=True)
            else:
                state[rel] = hashlib.sha256(data).hexdigest()
    return state


def derive(out, asof, work, tools):
    """The search files' run, as the workflow makes it and `pages_replay.py` replays it. Each
    builder in `tools` gets a copy of the staged site of its own, `<work>/<builder>/in`, and runs
    there, with no path to the folder that is served, writing into `<work>/<builder>/out`; it is
    killed at the deadline (BUILD_SECONDS). Only the folders of builders that finished are placed
    (`add_derived`). Then the site outside `search/` is checked against what the stage wrote; if
    anything differs, the site is staged again from the commit, with no search files."""
    started = time.monotonic()
    before = record_state(out)
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    builders = sorted(n for n in os.listdir(tools) if BUILDER.fullmatch(n)) if os.path.isdir(tools) else []
    if not builders:
        print(f"::error::no search-file builder in {tools}; no search files this deploy, and the record deploys as staged.")
        return 0
    node = subprocess.run(["node", "--version"], capture_output=True, text=True)
    print(f"search files: {len(builders)} builder(s), node {node.stdout.strip() or '(not found)'}")
    done, expected = [], {}
    for name in builders:
        digest = BUILDER.fullmatch(name).group(1)
        own = os.path.join(work, name[:-4])
        src, dst = os.path.join(own, "in"), os.path.join(own, "out")
        left = BUILD_SECONDS - (time.monotonic() - started)
        if left <= 0:
            print(f"::error::{name} was not run: the builders before it used the {BUILD_SECONDS} s. Its search files "
                  "are left out of this deploy, and the record deploys as staged.")
            continue
        shutil.copytree(out, src)
        os.makedirs(dst)
        sys.stdout.flush()
        try:
            code = subprocess.run(["node", os.path.abspath(os.path.join(tools, name)), "derive", "in", asof, "out"],
                                  cwd=own, timeout=left).returncode
        except subprocess.TimeoutExpired:
            code = f"stopped at {BUILD_SECONDS} s"
        if code != 0:
            print(f"::error::{name} did not finish ({code}); its search files are left out of this deploy, "
                  "and the record deploys as staged.")
            continue
        done.append(dst)
        expected[dst] = digest
    if done:
        add_derived(out, done, expected)
    if record_state(out) != before:
        print("::error::the staged record changed during the search files' run, so the site is staged again from the "
              "commit, with no search files this deploy.")
        main(out)
    return 0


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
        sys.exit(never_fatal(lambda: add_derived(sys.argv[2], [sys.argv[3]])))
    if len(sys.argv) == 6 and sys.argv[1] == "--derive":
        sys.exit(never_fatal(lambda: derive(*sys.argv[2:6])))
    if len(sys.argv) != 2:
        print("usage: pages_stage.py <out_dir> | --add-derived <out_dir> <derived_dir>"
              " | --derive <out_dir> <as_of> <work_dir> <tools_dir>", file=sys.stderr)
        sys.exit(64)
    sys.exit(main(sys.argv[1]))
