# billtracking-feed

**Published feed data for the [BillTracking](https://billtracking.org) app.**
One JSON line per delivered post, written by the two bots and served over HTTPS
to the mobile app.

## Why this repo exists separately

The bots need write access to publish here. Scoping their token to a repo that
holds nothing but data means a leaked or misused credential corrupts feed data —
recoverable from the bots' own state — instead of defacing billtracking.org.
That is the entire reason this is not part of the website repo.

## Layout

```
us-YYYY-MM.jsonl     one file per bot per month, append-only, one JSON object per line
eu-YYYY-MM.jsonl     (prefixed by bot since July 2026, so neither bot can overwrite the other's)
us-overrides.jsonl   each bot's public corrections sheet (app docs/CORRECTIONS.md)
eu-overrides.jsonl
paths/<ref>.json     per-bill path artifacts, both bots (US since 19 July 2026, EU since 23 July 2026)
2026-07.jsonl        the first July file from before the prefix, and overrides.jsonl, empty: both
overrides.jsonl      legacy, read by nothing, kept because nothing published ever leaves
```

Monthly sharding is deliberate: the app fetches the current month (~180 KB at
12 posts/day) and older months only when a reader scrolls back. It never needs
the whole archive.

## Record shape

Written by `feed_export.py` in both bot repos. Schema version is `v`.

```json
{"v":1,"bot":"us","type":"action","posted_at":"2026-07-17T18:22:00+00:00",
 "text":"...","reference":"H.R. 1329","family":"cloture_invoked",
 "label":"Cloture invoked","event_date":"2026-07-17","tweet_id":"..."}
```

Fields beyond `v`, `bot`, `type`, `posted_at` and `text` are optional — the
exporter drops empty values, so the shape varies by post type. **Consumers must
not assume any optional field is present.**

The app's contract with this data lives in `app/src/data/feed-schema.ts`.
Note it carries `family`, never `bucket`: the bucket is derived app-side from
decision D9, because that mapping drives notifications and belongs with the
product, not the transport.

## The two workflows here

`.github/workflows/render-site.yml` runs on every push to `main`: it waits until
GitHub Pages serves the push, then asks the website repo to render its `/p/` and
`/b/` pages, so a page follows its post by minutes instead of waiting for a cron
GitHub mostly drops (added 2026-10-13, app OPEN-ITEMS OI-0305). It holds one
secret, `SITE_RENDER_TOKEN`, which can only start or cancel workflow runs on the
website repo — it cannot write there, or here. If Pages has not served a push
within five minutes, the render is still asked for and the run then goes red, so
a stuck feed host reaches the owner by GitHub's failure mail. Its failure posture
is in the file's header.

`.github/workflows/pages.yml` deploys this repository to GitHub Pages once the
Pages source is "GitHub Actions" (until then it only builds and checks). It
serves every file as committed, after checking each against the commit's bytes,
and adds one generated file per legislature that is never committed:

```
us-fingerprint.json  {"v":1,"polity":"us","commit":"<sha>","files":[{"name":"us-2026-07.jsonl",
eu-fingerprint.json    "bytes":55790,"posts":41,"sha256":"…"}, …, {"name":"us-overrides.jsonl",…}]}
```

The app reads a legislature's fingerprint first and downloads only the month
files whose sha256 changed (app OPEN-ITEMS OI-0329). Rollback, and everything
else, is in the file's header.

When something looks wrong with the feed host (a GitHub email, old posts, a slow
feed), the app's `docs/FEED-HOST-RUNBOOK.md` starts from the symptom.

## This is published data, not a database

Everything here is the bots' public record of what the legislatures did; X carries a
paced selection of it (app DECISIONS D37). Nothing about a reader
appears in this repo — no accounts, no votes, no identifiers, no analytics.
It is a one-way archive of what the bots said.

## Editing by hand

Don't. It is machine-written and append-only. Corrections go through the
overrides mechanism in the bot repos, which records them publicly rather than
rewriting history.
