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
YYYY-MM.jsonl     one file per month, append-only, one JSON object per line
paths/<ref>.json  per-bill path artifacts (US first, EU to follow)
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

## This is published data, not a database

Everything here has already been posted publicly on X. Nothing about a reader
appears in this repo — no accounts, no votes, no identifiers, no analytics.
It is a one-way archive of what the bots said.

## Editing by hand

Don't. It is machine-written and append-only. Corrections go through the
overrides mechanism in the bot repos, which records them publicly rather than
rewriting history.
