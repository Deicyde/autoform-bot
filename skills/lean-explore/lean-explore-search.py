#!/usr/bin/env python3
"""Search Lean Explore (https://www.leanexplore.com) from the autoform workflow.

Semantic + name search over Mathlib and 8 other Lean 4 libraries (Batteries, CSLib,
FLT, FormalConjectures, Init, Lean, PhysLean, Std). A thin, dependency-FREE client
for Lean Explore's remote API v2 (stdlib only — no `pip install`): one GET to search,
one to fetch a declaration's full detail by id.

Auth is the user's OWN Lean Explore API key in ``LEANEXPLORE_API_KEY`` — unrelated to
the Claude Max / Anthropic billing path.

Usage::

  lean-explore-search.py "<query>" [--packages Mathlib,Batteries] [--limit 10] [--json]
  lean-explore-search.py --id <declaration_id> [--json]

Get a key: sign in at https://www.leanexplore.com, create an API key, then
``export LEANEXPLORE_API_KEY=<key>``.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://www.leanexplore.com/api/v2"
PACKAGES = ["Batteries", "CSLib", "FLT", "FormalConjectures", "Init", "Lean", "Mathlib", "PhysLean", "Std"]
_TIMEOUT = 20
# Lean Explore sits behind Cloudflare, which rejects the default `Python-urllib`
# User-Agent with Error 1010 ("blocked based on your browser's signature") before
# the key is ever checked. A normal browser UA passes that browser-signature check.
_USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _bold_title(informalization: str | None) -> str:
    """Lean Explore informalizations lead with a ``**Bold Title.**`` header — pull it
    out for a one-line description, else fall back to the first line."""
    if not informalization:
        return ""
    m = re.match(r"\s*\*\*(.+?)\*\*", informalization)
    return (m.group(1) if m else informalization.strip().split("\n", 1)[0]).strip()


def _ssl_context() -> ssl.SSLContext:
    """A verifying TLS context, using ``certifi``'s CA bundle if it's importable
    (it is under the plugin's uv env via httpx) — so HTTPS works even where the
    system Python has no CA bundle. Falls back to the default verifying context."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _get(url: str, key: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT, context=_ssl_context()) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Search Lean Explore (Mathlib + 8 Lean libraries).")
    ap.add_argument("query", nargs="?", help="natural-language / concept / name search query")
    ap.add_argument("--id", type=int, help="fetch one declaration's full detail instead of searching")
    ap.add_argument("--packages", default="",
                    help="comma-separated package filter; one or more of: " + ", ".join(PACKAGES))
    ap.add_argument("--limit", type=int, default=10, help="max results (default 10)")
    ap.add_argument("--json", action="store_true", help="print the raw API JSON")
    a = ap.parse_args(argv)

    key = os.environ.get("LEANEXPLORE_API_KEY", "").strip()
    if not key:
        _die("LEANEXPLORE_API_KEY is not set.\n"
             "  Get a key: sign in at https://www.leanexplore.com, create an API key, then\n"
             "  export LEANEXPLORE_API_KEY=<your-key>", 2)
    if not a.query and a.id is None:
        ap.error("give a search query, or --id <declaration_id>")

    if a.id is not None:
        url = f"{API_BASE}/declarations/{a.id}"
    else:
        params: dict[str, str | int] = {"q": a.query, "limit": a.limit}
        if a.packages:
            params["packages"] = a.packages
        url = f"{API_BASE}/search?{urllib.parse.urlencode(params)}"

    try:
        data = _get(url, key)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore") if hasattr(e, "read") else ""
        if "1010" in body or "browser's signature" in body.lower():
            _die("Lean Explore's Cloudflare edge blocked this request (Error 1010 — browser "
                 "signature), before the key was checked. The request needs a browser "
                 "User-Agent — this is a client bug, not a key problem.", 1)
        if e.code in (401, 403):
            _die(f"Lean Explore rejected the API key ({e.code}) — check LEANEXPLORE_API_KEY.", 2)
        if e.code == 404 and a.id is not None:
            _die(f"declaration {a.id} not found", 1)
        _die(f"Lean Explore API error {e.code}: {body[:300]}", 1)
    except urllib.error.URLError as e:
        hint = ""
        if "CERTIFICATE_VERIFY" in str(e.reason):
            hint = ("\n  (TLS cert verification failed — this Python has no CA bundle. Fix: "
                    "`pip install certifi` / run under the plugin's uv env, run this Python's "
                    "'Install Certificates.command' on macOS, or set SSL_CERT_FILE.)")
        _die(f"could not reach Lean Explore ({e.reason}).{hint}", 1)
    except (json.JSONDecodeError, ValueError) as e:
        _die(f"unexpected response from Lean Explore: {e}", 1)

    if a.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0

    if a.id is not None:                                   # full detail for one declaration
        print(f"[{data.get('id')}] {data.get('name')}   ({data.get('module')})")
        if data.get("informalization"):
            print(f"  {_bold_title(data['informalization'])}")
        if data.get("docstring"):
            print(f"  docstring: {data['docstring'].strip()[:600]}")
        if data.get("source_link"):
            print(f"  source: {data['source_link']}")
        if data.get("source_text"):
            print("  ---\n  " + data["source_text"].strip().replace("\n", "\n  "))
        return 0

    results = data.get("results", [])                      # slim list; --id for full detail
    head = f"{data.get('count', len(results))} result(s) for {a.query!r}"
    if data.get("processing_time_ms"):
        head += f" · {data['processing_time_ms']} ms"
    if a.packages:
        head += f" · packages={a.packages}"
    print(head)
    if not results:
        print("  (none — try broader/different terms, or drop --packages)")
        return 0
    for r in results:
        desc = _bold_title(r.get("informalization")) or (r.get("docstring") or "").strip().split("\n", 1)[0]
        line = f"  [{r.get('id')}] {r.get('name')}"
        if r.get("module"):
            line += f"   ({r['module']})"
        print(line)
        if desc:
            print(f"        {desc[:160]}")
    print("\n→ full source/docstring for any hit:  lean-explore-search.py --id <id>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
