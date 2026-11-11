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
`posts` counts the month file's non-blank lines, less the lines that name a post (the app's
`LINE_TYPES`, `src/data/feed-schema.ts`: the summary lines and the AI summary lines); the app
may still drop a line it cannot adapt, so it is a count for planning a load, never a claim
about the record. Readers ignore fields they do not know, so a field can be added without a
new `v`.

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
digest) is handed a copy of the staged site of its own, `<work_dir>/<builder>/in`, and runs
there, never handed the folder that is served (the guard read of 2026-10-29: a builder with
write access could change a served month's bytes on a green run), writing its legislatures' search
files under `<work_dir>/<builder>/out/search/`, proving them, and naming each file's bytes and
sha256 in `<work_dir>/<builder>/out/<digest>.json`. Every copy is made before any builder runs
and checked against the stage's site just before its builder runs. The builders together get
BUILD_SECONDS, each killed at the deadline with its process group. Then, for the builders that
finished, as `--add-derived <out_dir> <derived_dir>` does for one folder, it checks every file against its manifest, copies it into
the staged site beside the record (never over a file that differs, each readable by everyone),
and adds to the legislature's fingerprint, as the stage wrote it, the digest's entry:

    "derived": {"<digest>": {"list": "search/l/<h64>.json", "bytes": 511, "h64": "…", "corrections": 1}}

An entry it cannot check is left out with an error annotation (Class 1: the run stays green,
the record and the fingerprint's record rows deploy as staged); every entry is checked before
any file is placed, and a fingerprint is rewritten whole or not at all. A builder that does not
finish, writes no manifest, or gives a fingerprinted legislature no set is named; so is a step
with no builder, and a manifest with two entries for one legislature (it takes neither). No
app or website reads these files yet: today's readers pass over the `derived` block (the app's
check:searchfiles runs its client over it). Exit 0, whatever a builder or a manifest does to
the search files.

THE SITE, VOUCHED FOR (`keep_record`; fail closed). Last, the site must be exactly what the
stage wrote with the files this run placed over it, byte for byte, nothing added (a file or a
folder, under `search/` too), no symbolic link, and every file and folder readable by everyone.
If not, or if the run stopped part-way, it is staged again from the commit, with no search
files, still exit 0. If even that does not give back what
the stage wrote, exit 2, and the workflow stops before the upload: nothing is deployed and the
live site keeps the last deploy. A cut of the step by its clock fails it the same way
(pages.yml), so no upload reads a site this step could not vouch for. The one gap: a process a
builder detaches from its own process group (each builder's group is killed when it ends) could
still write after the check; today's builder starts no process. `pages_replay.py` replays
`--derive` itself, so the replay is the workflow's run.
"""

import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

# The app's `LINE_TYPES` (src/data/feed-schema.ts): every line that names a post rather than being one.
# A new type there is added here.
LINE_TYPES = {"summary_fill", "summary_match", "summary_ai"}
MONTH = re.compile(r"^([a-z]+)-(\d{4})-(\d{2})\.jsonl$")
SHEET = re.compile(r"^([a-z]+)-overrides\.jsonl$")
SITE_LIMIT = 1024 ** 3
# What a new reader downloads before a first search, every legislature on (a guest's start): past it,
# the time to revisit server search (the owner, 2026-11-07; the app's OPEN-ITEMS OI-0167). It counts the
# legislatures this feed carries: once the feed host splits (OI-0339), the hosts' lines add up.
FIRST_DOWNLOAD_LINE = 50 * 1024 ** 2
# A month's own search files; a yearly bundle carries them verbatim.
MONTH_FILES = ("base", "rows", "patch", "new")


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
        if not (isinstance(rec, dict) and rec.get("type") in LINE_TYPES):
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


DERIVED_PATH = re.compile(r"search/[lbrpcu]/[0-9a-f]{16}\.jsonl?")
DIGEST_MANIFEST = re.compile(r"([0-9a-f]{16})\.json")
H64 = re.compile(r"[0-9a-f]{16}")
FINGERPRINT = re.compile(r"[a-z]+-fingerprint\.json")
BUILDER = re.compile(r"bt-feed-([0-9a-f]{16})\.cjs")

# THE SEARCH FILES' CLOCK: the builders together get BUILD_SECONDS, each killed at the deadline,
# so a slow builder costs only its search files. The step's own command is cut at 105 s by
# `timeout` (pages.yml), inside its two-minute limit, which stops only the step's shell; a cut
# fails the step, and the run stops before the upload. INVENTED: today both legislatures build
# in about 1 s.
BUILD_SECONDS = 90

# Every file this run placed in a site, by its absolute path: the sha256 of its bytes. The final
# check (`keep_record`) takes these and nothing else beside what the stage wrote.
PLACED = {}


def place(scratch, dest, data):
    """Write `data` at `dest` whole or not at all, readable by everyone as the stage's files are:
    written to a new file in `scratch` (created here, never one a builder could have left), then
    moved into place, so a step stopped part-way leaves no half-written file in the site."""
    fd, tmp = tempfile.mkstemp(dir=scratch, prefix=".placing-")
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.chmod(tmp, 0o644)   # mkstemp makes 0600, and Pages fails or refuses a file others cannot read
    os.replace(tmp, dest)
    PLACED[os.path.abspath(dest)] = hashlib.sha256(data).hexdigest()


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


def first_download(block, plan):
    """The bytes a device holding none of this legislature's search files asks for under one set: its
    list, each yearly bundle, and the files of every month no bundle carries."""
    sizes = {os.path.splitext(os.path.basename(dest))[0]: len(data) for data, dest in plan}
    listed = next(data for data, dest in plan if dest.replace(os.sep, "/").endswith(block["list"]))
    doc = json.loads(listed.decode("utf-8"))
    bundles = doc.get("bundles") or []
    total = len(listed) + sum(sizes[b["h"]] for b in bundles)
    for month in doc.get("months") or []:
        if not any(b["from"] <= month["m"] <= b["to"] for b in bundles):
            total += sum(sizes[month[k]] for k in MONTH_FILES if k in month)
    return total


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
        named = [e.get("polity") for e in doc["entries"] if isinstance(e, dict)]
        named = [p for p in named if isinstance(p, str)]
        twice = {p for p in named if named.count(p) > 1}
        for polity in sorted(twice):
            print(f"::error::search files under digest {digest} are left out of {polity}'s fingerprint: its manifest "
                  f"holds more than one entry for {polity}, so it takes none. The record deploys as staged.")
        for entry in doc["entries"]:
            polity = entry.get("polity") if isinstance(entry, dict) else None
            if isinstance(polity, str) and polity in twice:
                continue
            try:
                checked, why = check_entry(derived, out, entry if isinstance(entry, dict) else {})
            except Exception as e:
                checked, why = None, f"its entry could not be read ({type(e).__name__}: {e})"
            if not checked:
                print(f"::error::search files under digest {digest} are left out of one fingerprint: {why}. "
                      "The record deploys as staged.")
                continue
            accepted.append((digest, derived, *checked))
    return accepted


def add_derived(out, derived_dirs, expected=None, staged=None):
    """Place the checked files of every entry under `derived_dirs` and add each fingerprint's
    `derived` block. `expected`, the digest of each folder's builder: a builder that gave a
    fingerprinted legislature no set is named. `staged`, each fingerprint's bytes as the stage
    wrote them, read before any builder ran: a block is added to those bytes, never to a
    fingerprint a builder could have rewritten since."""
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
    downloads = {}
    added = 0
    for digest, derived, polity, block, plan in accepted:
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
        try:
            downloads[digest] = downloads.get(digest, 0) + first_download(block, plan)
        except Exception as e:   # a measure only: it never stops the deploy
            print(f"::warning::{polity}'s first download under digest {digest} could not be measured "
                  f"({type(e).__name__}: {e}).")

    prints = sorted(staged) if staged is not None else sorted(n for n in os.listdir(out) if FINGERPRINT.fullmatch(n))
    for name in prints:
        polity = name[: -len("-fingerprint.json")]
        for digest in sorted(set((expected or {}).values())):
            if digest not in blocks.get(polity, {}):
                print(f"::error::bt-feed-{digest}.cjs gave {polity} no search files this deploy (the lines above say "
                      "why); its record deploys as staged.")
        if polity not in blocks:
            continue
        path = os.path.join(out, name)
        if staged is not None:
            data = staged[name]
        else:
            with open(path, "rb") as fh:
                data = fh.read()
        doc = json.loads(data.decode("utf-8"))
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
    if downloads:
        heaviest = max(downloads.values())
        print(f"search files: a new reader's first download, every legislature on, is {heaviest / 1024 ** 2:.1f} MB")
        if heaviest >= FIRST_DOWNLOAD_LINE:
            print(f"::warning::A new reader's first download of search files, every legislature on, is "
                  f"{heaviest / 1024 ** 2:.0f} MB, past the 50 MB line: the time to revisit server search "
                  "(the app's OPEN-ITEMS OI-0167).")
    return 0


def site_state(root):
    """Everything under `root`, by its path relative to it: each file's sha256 and whether everyone
    may read it, each folder and whether everyone may list it. A symbolic link, to a file or a
    folder, or `root` itself one, is never what the stage wrote."""
    if os.path.islink(root):
        return {".": ("a symbolic link", False)}
    state = {}
    for base, dirs, files in os.walk(root):
        for n in dirs:
            p = os.path.join(base, n)
            rel = os.path.relpath(p, root).replace(os.sep, "/")
            mode = os.lstat(p).st_mode
            state[rel] = ("a symbolic link", False) if stat.S_ISLNK(mode) else \
                ("a folder", bool(mode & stat.S_IROTH and mode & stat.S_IXOTH))
        for n in files:
            p = os.path.join(base, n)
            rel = os.path.relpath(p, root).replace(os.sep, "/")
            if os.path.islink(p):
                state[rel] = ("a symbolic link", False)
                continue
            with open(p, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
            state[rel] = (digest, bool(os.stat(p).st_mode & stat.S_IROTH))
    return state


def end_group(proc):
    """Kill a builder and every process it left in its own process group (POSIX), so nothing it
    started writes after it ends. A process that left the group (setsid) is out of reach: today's
    builder starts none."""
    if hasattr(os, "killpg"):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    if proc.poll() is None:
        proc.kill()
    proc.wait()


def derive(out, asof, work, tools):
    """The search files' run, as the workflow makes it and `pages_replay.py` replays it. Each
    builder in `tools` is handed a copy of the staged site of its own, `<work>/<builder>/in`, made
    before any builder runs and checked against the stage's site just before its builder runs,
    never the folder that is served (one that reaches it by a path of its own is caught by
    `keep_record`), and writes into `<work>/<builder>/out`; it is killed at the deadline
    (BUILD_SECONDS, which the copies count against), with what it left running in its process
    group. Only the folders of builders that finished are placed (`add_derived`), onto the
    fingerprints as the stage wrote them."""
    started = time.monotonic()
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    builders = sorted(n for n in os.listdir(tools) if BUILDER.fullmatch(n)) if os.path.isdir(tools) else []
    if not builders:
        print(f"::error::no search-file builder in {tools}; no search files this deploy, and the record deploys as staged.")
        return 0
    staged = {}
    for name in sorted(n for n in os.listdir(out) if FINGERPRINT.fullmatch(n)):
        with open(os.path.join(out, name), "rb") as fh:
            staged[name] = fh.read()
    stage_state = site_state(out)
    node = subprocess.run(["node", "--version"], capture_output=True, text=True)
    print(f"search files: {len(builders)} builder(s), node {node.stdout.strip()}")
    for name in builders:
        shutil.copytree(out, os.path.join(work, name[:-4], "in"))
        os.makedirs(os.path.join(work, name[:-4], "out"))
    done, expected = [], {}
    for name in builders:
        own = os.path.join(work, name[:-4])
        left = BUILD_SECONDS - (time.monotonic() - started)
        if left <= 0:
            print(f"::error::{name} was not run: the {BUILD_SECONDS} s were spent before it. Its search files are left "
                  "out of this deploy, and the record deploys as staged.")
            continue
        if site_state(os.path.join(own, "in")) != stage_state:
            print(f"::error::{name} was not run: its copy of the staged site was changed before it ran (by a builder "
                  "before it). Its search files are left out of this deploy, and the record deploys as staged.")
            continue
        sys.stdout.flush()
        proc = subprocess.Popen(["node", os.path.abspath(os.path.join(tools, name)), "derive", "in", asof, "out"],
                                cwd=own, start_new_session=True)
        try:
            code = proc.wait(timeout=left)
        except subprocess.TimeoutExpired:
            code = f"stopped at the deadline, {BUILD_SECONDS} s after the step's start"
        finally:
            end_group(proc)
        if code != 0:
            print(f"::error::{name} did not finish ({code}); its search files are left out of this deploy, "
                  "and the record deploys as staged.")
            continue
        done.append(os.path.join(own, "out"))
        expected[done[-1]] = BUILDER.fullmatch(name).group(1)
    if done:
        add_derived(out, done, expected, staged)
    return 0


def site_is(out, before, placed):
    """The site is exactly what the stage wrote with `placed` over it, byte for byte (the folders
    that hold placed files included), and everyone may read every file and list every folder."""
    try:
        now = site_state(out)
    except Exception:
        return False
    expected = {p: (kind, True) for p, (kind, _readable) in before.items()}
    root = os.path.abspath(out)
    for path, digest in placed.items():
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        expected[rel] = (digest, True)
        while "/" in rel:
            rel = rel.rsplit("/", 1)[0]
            expected[rel] = ("a folder", True)
    return now == expected


def keep_record(out, run):
    """Run the search files' `run`, then vouch for the site in `out`: exactly what the stage wrote
    and what this run placed (PLACED), byte for byte, every file and folder readable by everyone.
    If not, or if the run stopped part-way, it is staged again from the commit, with no search files
    (exit 0). If even that does not give back what the stage wrote, exit 2: the workflow stops
    before the upload, and the live site keeps the last deploy. Fail closed, as a cut of the step
    by its clock does (pages.yml)."""
    PLACED.clear()
    try:
        before = site_state(out)
    except Exception as e:
        print(f"::error::the staged site could not be read ({type(e).__name__}: {e}), so nothing is deployed this "
              "run; the live site still serves the last deploy.")
        return 2
    try:
        run()
        whole = True
    except Exception as e:   # the search files never stop a deploy: named, and the site staged again
        print(f"::error::the search files' run stopped part-way ({type(e).__name__}: {e}).")
        whole = False
    if whole and site_is(out, before, PLACED):
        return 0
    print("::error::the site is not what the stage wrote and this run placed (the run stopped part-way, or a file "
          "or folder changed, added or left unreadable during it), so it is staged again from the commit, with no "
          "search files this deploy.")
    try:
        main(out)
    except Exception as e:
        print(f"::error::staging again failed ({type(e).__name__}: {e}).")
    if site_is(out, before, {}):
        return 0
    print("::error::the site could not be staged again as the stage wrote it, so nothing is deployed this run; the "
          "live site still serves the last deploy.")
    return 2


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--add-derived":
        sys.exit(keep_record(sys.argv[2], lambda: add_derived(sys.argv[2], [sys.argv[3]])))
    if len(sys.argv) == 6 and sys.argv[1] == "--derive":
        sys.exit(keep_record(sys.argv[2], lambda: derive(*sys.argv[2:6])))
    if len(sys.argv) != 2:
        print("usage: pages_stage.py <out_dir> | --add-derived <out_dir> <derived_dir>"
              " | --derive <out_dir> <as_of> <work_dir> <tools_dir>", file=sys.stderr)
        sys.exit(64)
    sys.exit(main(sys.argv[1]))
