---
name: lean-explore
description: >
  Search Lean Explore (leanexplore.com) — semantic, natural-language search over
  Mathlib + 8 other Lean 4 libraries (Batteries, CSLib, FLT, FormalConjectures, Init,
  Lean, PhysLean, Std) — to find existing declarations, lemmas, and definitions
  before proving or naming. Returns fully-qualified names, modules, one-line
  descriptions, and source links; fetch full source/docstring by id. Use when looking
  for a lemma by what it MEANS (not just an exact name), checking whether something is
  already formalized, or finding prior art across libraries. Complements the local
  mathlib_search.py (exact-name / ripgrep) with cross-library semantic search.
  Triggers: /lean-explore, "search lean explore", "is this in mathlib", "find a lemma for".
---

# Lean Explore — semantic search over Mathlib + 8 Lean libraries

Lean Explore (https://www.leanexplore.com) is a search engine for Lean 4 declarations
(arXiv:2506.11085). It indexes nine corpora — **Batteries, CSLib, FLT,
FormalConjectures, Init, Lean, Mathlib, PhysLean, Std** — and answers
**natural-language / concept** queries, not just exact names. This skill is a thin,
dependency-free client for its remote API.

## When to use it

- **Search-before-proving** — before writing a proof, look for an existing lemma by
  *what it says* ("a monotone bounded sequence converges", "card of a product set"),
  not just a guessed name.
- **"Is this already in Mathlib (or Batteries / Std / FLT / …)?"** — avoid
  re-formalizing; ground a `missing` node to a real declaration; pick the canonical
  name/namespace.
- **Prior art across libraries** — find how a concept is stated elsewhere when
  Mathlib doesn't have it directly.

The **worker** (`autoform-prove`), the **mathlib-checker**, and the **planner** can all
use it; results give you the exact `name` + `module` to cite or `import`.

## Setup (one-time)

It needs your own Lean Explore API key:

1. Sign in at https://www.leanexplore.com and create an API key.
2. `export LEANEXPLORE_API_KEY=<your-key>` (add it to your shell profile).

This key is **Lean Explore's own** — it is *not* the Claude Max / Anthropic billing
path, and nothing here touches `ANTHROPIC_API_KEY`. If the key is unset the skill
prints these instructions and exits.

## Usage

```bash
# semantic / concept / name search (default 10 results)
python3 ${CLAUDE_PLUGIN_ROOT}/skills/lean-explore/lean-explore-search.py "pigeonhole principle on finsets"

# restrict to one or more libraries
python3 ${CLAUDE_PLUGIN_ROOT}/skills/lean-explore/lean-explore-search.py "sunflower lemma" --packages Mathlib,Batteries

# full source + docstring for a specific hit (use the [id] from the search list)
python3 ${CLAUDE_PLUGIN_ROOT}/skills/lean-explore/lean-explore-search.py --id 12345

# raw API JSON (for scripting)
python3 ${CLAUDE_PLUGIN_ROOT}/skills/lean-explore/lean-explore-search.py "<query>" --json
```

Search prints a slim list — `[id] FullyQualified.Name  (Module)` + a one-line
description — to keep context small; drill into any hit with `--id <id>` for the full
source text, docstring, and source link.

## How it complements `mathlib_search.py`

| | `scripts/mathlib_search.py` | **lean-explore** (this skill) |
|---|---|---|
| where | **local** Mathlib checkout (ripgrep) | **remote** API |
| query | exact name / grep / `mathlib_find_name` | **semantic / natural-language** |
| scope | the installed Mathlib | **9 libraries** |
| offline | yes | no (needs the API + key) |

Use **lean-explore to *discover*** a declaration by meaning across libraries, then
`mathlib_search.py` to confirm it in the local checkout and grep its exact signature.

## API (for reference)

Remote API v2, Bearer-authenticated:

- `GET https://www.leanexplore.com/api/v2/search?q=<query>&limit=<n>[&packages=Mathlib,…]`
  → `{query, results: [{id, name, module, docstring, source_text, source_link,
  dependencies, informalization}], count, processing_time_ms}`
- `GET https://www.leanexplore.com/api/v2/declarations/<id>` → one such result.

For heavier / programmatic use, Lean Explore also ships an official package
(`pip install lean-explore`, an `ApiClient`, a CLI, and an MCP server
`lean-explore mcp serve`); this skill deliberately stays stdlib-only so it works with
no extra install.
