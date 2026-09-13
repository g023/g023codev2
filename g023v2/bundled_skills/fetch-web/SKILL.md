---
name: fetch-web
description: >
  Read the public web from this harness without a real browser. Use when
  you need a page, docs, or search. Covers web_search vs fetch_url vs
  cache_mode, and when JS-only pages cannot be read.
tags: [browser, fetch, web, search]
---

# Public web without a browser

This harness has no headless Chrome. Do not `run_shell` curl/wget for a URL you can `fetch_url`.

## Which tool

- `web_search` — you do not have a URL yet. Search only.
- `fetch_url` — you already have an `http`/`https` URL. HTML is stripped to prose (`extract=text` default). `markdown`, `links`, and `raw` are available.
- `run_shell` curl — not for this. `fetch_url` is the contract (browser-like headers, cache, extract).

## Cache

A **global** URL cache (`cache_mode`):

- `auto` (default) — reuse a copy younger than `max_age` (default 3600s).
- `cache` — store only; on miss it tells you to re-call with `fresh`.
- `fresh` — always hit the network.

The store does not TTL-evict. You choose freshness. After a network failure, a stored copy may still exist; re-call with `cache`.

## Limits

Timeout default 15s, max 30s. Returned text default 20,000 chars (cap 256 KiB). Raise `max_chars` with `cache_mode=cache` to read more without another GET.

No JavaScript runs. A SPA shell, bot wall, or login wall will not yield the real page — say so and stop retrying the same URL. Authenticated or private docs are out of reach here.

## Discipline

One URL per call. Prefer `extract=text` unless you need links or markdown. Do not dump a huge `raw` body into history if prose would do.
---
