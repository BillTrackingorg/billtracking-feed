#!/usr/bin/env python3
"""Replay the Pages build, search files included, over every commit of a history.

    python3 .github/scripts/pages_replay.py <feed clone> <rev> <work dir> [--jobs N] [--every K]

For each commit of `git rev-list <rev>`, in a scratch clone of its own (line endings as stored):
the stage (`pages_stage.py <site>`), then `pages_stage.py --derive <site> <commit month, UTC>
<work> <tools>`, the same two commands `.github/workflows/pages.yml` runs, with the scripts and
builders of THIS checkout (an old commit has none). Then it checks what a deploy would serve:
  - the stage's exit is 0 or 3, and each fingerprint's record rows are the ones the stage wrote;
  - every fingerprint is still one today's app accepts (`v` 1, its polity, every row a name,
    bytes and a 64-hex sha256), and each `derived` entry names a list that is served, of its
    size, whose every file is served under `search/`;
  - the run reported no error (a builder, or a file the stage could not take);
  - the staged record is byte for byte what it was before the search files' run;
  - every K-th commit (and the last), a second `--derive` over a copy of the staged site gives the
    same site, byte for byte, and the same exit.
Prints one line per failure and a summary; exit 0 only when nothing failed. The proof of a
change to the stage or a builder is this run's summary, quoted in that change's commit message.
"""

import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STAGE = os.path.join(HERE, "pages_stage.py")
TOOLS = os.path.join(os.path.dirname(HERE), "tools")
PROVEN = re.compile(r"^([a-z]+): (\d+) search files under digest ([0-9a-f]{16}), proven \((\d+) decoded, (\d+) months base ⊕ patch, (\d+) sets sufficient\)")


def run(args, cwd=None, env=None):
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")


def tree(root):
    out = {}
    for base, _dirs, files in os.walk(root):
        for name in files:
            p = os.path.join(base, name)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root).replace(os.sep, "/")] = fh.read()
    return out


def replay_one(clone, sha, builders, twice):
    site = os.path.join(clone, "_site")
    derived = os.path.join(clone, "_derived")
    for d in (site, site + "2", derived, derived + "2"):
        shutil.rmtree(d, ignore_errors=True)
    r = run(["git", "checkout", "-q", "-f", sha], cwd=clone)
    if r.returncode:
        return {"sha": sha, "fail": [f"checkout: {r.stderr.strip()}"]}
    run(["git", "clean", "-q", "-f", "-d", "-x", "-e", "_site", "-e", "_derived*"], cwd=clone)
    res = {"sha": sha, "fail": [], "prints": 0, "derived": 0, "files": 0, "months": 0, "sets": 0, "decoded": 0}
    st = run([sys.executable, STAGE, site], cwd=clone)
    res["stage"] = st.returncode
    if st.returncode not in (0, 3):
        res["fail"].append(f"stage exit {st.returncode}: {st.stdout.strip()[-300:]}")
        return res
    prints = sorted(n for n in os.listdir(site) if re.match(r"^[a-z]+-fingerprint\.json$", n))
    before = {n: json.load(open(os.path.join(site, n), encoding="utf-8")) for n in prints}
    staged = {p: b for p, b in tree(site).items() if not p.endswith("-fingerprint.json")}
    res["prints"] = len(prints)
    asof = run(["git", "show", "-s", "--date=format-local:%Y-%m", "--format=%cd", "HEAD"], cwd=clone,
               env={**os.environ, "TZ": "UTC"}).stdout.strip()
    if twice:
        shutil.copytree(site, site + "2")
    dv = run([sys.executable, STAGE, "--derive", site, asof, derived, TOOLS], cwd=clone)
    for line in (dv.stdout + dv.stderr).splitlines():
        m = PROVEN.match(line)
        if m:
            res["decoded"] += int(m.group(4))
            res["months"] += int(m.group(5))
            res["sets"] += int(m.group(6))
        if line.startswith("::error::"):
            res["fail"].append(f"derive: {line[9:300]}")
    if dv.returncode != 0:
        res["fail"].append(f"derive exit {dv.returncode}: {dv.stdout.strip()[-300:]}")
    after = {p: b for p, b in tree(site).items() if not p.endswith("-fingerprint.json") and not p.startswith("search/")}
    if after != staged:
        res["fail"].append("the staged record changed in the search files' run")
    for n in prints:
        doc = json.load(open(os.path.join(site, n), encoding="utf-8"))
        polity = n[: -len("-fingerprint.json")]
        if {k: v for k, v in doc.items() if k != "derived"} != before[n]:
            res["fail"].append(f"{n}: the record rows changed after the stage")
        ok = doc.get("v") == 1 and doc.get("polity") == polity and isinstance(doc.get("files"), list) and all(
            isinstance(f.get("name"), str) and isinstance(f.get("bytes"), int) and f["bytes"] >= 0
            and isinstance(f.get("sha256"), str) and re.match(r"^[0-9a-f]{64}$", f["sha256"]) for f in doc["files"])
        if not ok:
            res["fail"].append(f"{n}: not a fingerprint today's app accepts")
        block = doc.get("derived") or {}
        if len(block) != len(builders):
            res["fail"].append(f"{n}: {len(block)} derived entries for {len(builders)} builder(s)")
        for dd, e in block.items():
            lp = os.path.join(site, e["list"])
            if not os.path.exists(lp) or os.path.getsize(lp) != e["bytes"] or not e["list"].endswith(f"/{e['h64']}.json"):
                res["fail"].append(f"{n}: {dd}'s list is not served as named")
                continue
            lst = json.load(open(lp, encoding="utf-8"))
            res["derived"] += 1
            for m in lst["months"]:
                for kind, d, ext in (("base", "b", "json"), ("rows", "r", "json"), ("patch", "p", "json"),
                                     ("old", "c", "jsonl"), ("new", "c", "jsonl")):
                    if kind in m:
                        res["files"] += 1
                        if not os.path.exists(os.path.join(site, "search", d, f"{m[kind]}.{ext}")):
                            res["fail"].append(f"{n}: {m['m']}'s {kind} is not served")
    if twice:
        again = run([sys.executable, STAGE, "--derive", site + "2", asof, derived + "2", TOOLS], cwd=clone)
        res["twice"] = True
        if again.returncode != dv.returncode or tree(site + "2") != tree(site):
            res["fail"].append("a second --derive gave a different site or exit")
    return res


def job(repo, work, index, shas, builders, every, last):
    clone = os.path.join(work, f"j{index}")
    if not os.path.isdir(clone):
        run(["git", "clone", "-q", "--shared", "--no-checkout", "-c", "core.autocrlf=false", repo, clone])
    return [replay_one(clone, sha, builders, (i % every == 0) or sha == last) for i, sha in shas]


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 64
    repo, rev, work = os.path.abspath(argv[0]), argv[1], os.path.abspath(argv[2])
    jobs = int(argv[argv.index("--jobs") + 1]) if "--jobs" in argv else 4
    every = int(argv[argv.index("--every") + 1]) if "--every" in argv else 100
    builders = sorted(os.path.join(TOOLS, n) for n in os.listdir(TOOLS) if re.match(r"^bt-feed-[0-9a-f]{16}\.cjs$", n))
    if not builders:
        print("no builder in .github/tools/")
        return 64
    shas = run(["git", "rev-list", rev], cwd=repo).stdout.split()
    os.makedirs(work, exist_ok=True)
    indexed = list(enumerate(shas))
    with concurrent.futures.ThreadPoolExecutor(jobs) as ex:
        parts = list(ex.map(lambda k: job(repo, work, k, indexed[k::jobs], builders, every, shas[0]), range(jobs)))
    results = [r for p in parts for r in p]
    failed = [r for r in results if r["fail"]]
    for r in failed:
        for f in r["fail"]:
            print(f"{r['sha'][:9]}: {f}")
    total = lambda k: sum(r.get(k, 0) for r in results)
    print(json.dumps({
        "rev": rev, "commits": len(results), "builders": [os.path.basename(b) for b in builders],
        "stage0": sum(1 for r in results if r.get("stage") == 0), "stage3": sum(1 for r in results if r.get("stage") == 3),
        "fingerprints": total("prints"), "derivedEntries": total("derived"), "derivedFilesServed": total("files"),
        "decoded": total("decoded"), "monthsProven": total("months"), "setsProven": total("sets"),
        "deriveTwice": sum(1 for r in results if r.get("twice")), "failedCommits": len(failed),
    }))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
