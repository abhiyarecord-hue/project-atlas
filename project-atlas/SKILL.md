---
name: project-atlas
description: Understand and navigate any codebase or large document set WITHOUT reading the workspace. Use at the start of work in any project, before any broad Read/Glob/Grep exploration, whenever asked "how does X work", "where is Y", "what does this project do", before editing unfamiliar code, and after learning something non-obvious about the code (to save it). Provides a persistent, incremental, token-budgeted index (.atlas/) with tiered zoom (card → map → outline → slice), ranked symbol search, references, dependency/blast-radius graph, change detection, and hash-pinned notes that accumulate understanding across sessions and auto-flag themselves stale when code changes.
---

# project-atlas

The atlas replaces "read the workspace to understand it". Every command prints
only what was asked, capped by a token budget, with exact `file:start-end`
line ranges so you never need to open a whole file.

Run: `python ~/.claude/skills/project-atlas/atlas.py <cmd> ...` (call it `A` below).
It finds the project root by walking up to the nearest `.atlas/`; the first
run in a new project builds it (seconds, stdlib only). Every later command
re-indexes only files whose mtime/size changed, so the index is always fresh.

## The zoom ladder - always descend, never jump to full reads

| level | command | typical cost | use for |
|---|---|---|---|
| L0 | `A card` | ~150–1k tok | first thing in a session: stack, dirs, entry points, hub files, what changed, all saved knowledge |
| L1 | `A map [dir]` | ≤2.5k tok | which file holds what (top symbol names per file) |
| L2 | `A outline <file>` | ≤2k tok | every symbol with line ranges, imports, importers, notes |
| L3 | `A slice <file> <symbol\|a-b>` or `A slice f1:sym f2:a-b …` | only that code | exact code; batch many ranges in one call |

**Start with `A ask <question words>`** - one call returns the best-matching code bodies (constants, functions, methods) for a question, plus a ranked list of further candidates. Most "how does X work / where is Y" questions are answerable from that single output. Every extra tool call replays the whole conversation, so fewer calls is the biggest saving.

Locate instead of browse:
- `A find <words>` - ranked symbol search (camelCase/snake aware) + matching notes; falls back to text search. Prefer this over Grep for "where is X".
- `A refs <symbol>` - definition plus every usage line (before renaming/changing a signature).
- `A deps <file>` - local + external imports, importers, 2-hop blast radius (before editing a shared file).
- `A changed` - what changed on disk since the last scan (someone else's edits, a git pull).

## Rules

0. Invoke it exactly as `python "<absolute path to atlas.py>" <cmd> ...` from the project directory: one atlas command per Bash call, no `cd ... &&`, no shell variables, no `~` (these trigger permission prompts / fail on Windows).
1. Start of a session in a project: run `A card` (skip if the SessionStart hook already printed it). Do not Glob/Read the tree to orient yourself.
2. To answer a question: `ask` first; then fetch everything else you need in ONE batched call:
   `A slice a.py:func b.py:120-180 c.ts:Class.method` (any mix of files, symbols and line ranges; overlaps merge).
   Each tool call re-sends the whole conversation, so 1 call with 6k tokens of code is far cheaper than 6 calls with 1k each. Aim for ≤3 calls per question. Use the `Read` tool only with an `offset`/`limit` taken from atlas line ranges, and only when you need to edit that range (Edit requires a prior Read of the file).
3. Budgets: add `--budget N` to widen or shrink any output; `--k N` for more results. If output says `[truncated]`, narrow (a dir, a file, a symbol) before widening.
4. **Compound knowledge - this is what makes the next session cheaper.** When you learn something not obvious from symbol names (an invariant, why code is shaped oddly, where a flow really starts, a gotcha, a convention), save it in one line:
   - `A note <file> "auth tokens are refreshed in interceptor, not in login()"`
   - `A note project "build: npm run dev; tests need DB_URL; never edit generated/"`
   Keep notes short, factual and specific; one insight per note. They appear in `card`, `outline` and `find`.
5. Staleness: a note is pinned to its file's content hash. When the file changes, it shows `✱`. Before trusting a ✱ note, `slice` the relevant code; then `A refresh <file>` if still true, or `A forget <file> "<substring>"` if not.
6. After you edit files, nothing to do - the next command re-indexes them. If you made an architectural change, update the matching note.
7. Commit `.atlas/notes.json` to share knowledge with the team; `index.json` is git-ignored automatically. Add paths to `.atlasignore` (gitignore syntax) to exclude them.

## Example

```
A card                                  # orient: 900 tokens instead of reading 500k
A find refresh token expiry             # → src/auth/session.ts:41-88 fn refreshIfExpired | ...
A slice src/auth/session.ts refreshIfExpired
A refs refreshIfExpired                 # who calls it
A note src/auth/session.ts "expiry checked with 30s skew; server clock is source of truth"
```

Languages: Python (AST-exact), JS/TS/JSX/Vue/Svelte, Go, Rust, Java/Kotlin/C#/Scala/Dart, C/C++, PHP, Ruby/Elixir, Swift, shell/PowerShell, Lua, R, SQL, GraphQL/Proto, HTML (ids, headings, inline JS), CSS, Markdown (heading tree - works for books and docs too), JSON/YAML/TOML keys.
