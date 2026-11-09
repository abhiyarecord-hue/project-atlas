# project-atlas

A skill for Claude Code that cuts how many tool calls it needs to understand a codebase.

On FastAPI: same answers, 19 turns instead of 48, 44% fewer tokens, 20% lower cost.

## Why turns matter

Every tool call in Claude Code re-sends the whole conversation. So the number of calls usually costs more than the size of what gets read. I found that a tool which shrinks reads but makes the model call it more often ends up more expensive (see "What didn't work" below).

project-atlas keeps a small index of the repo: symbols with line ranges, imports, and a dependency graph. It's built the first time you open a project and updated incrementally after that. A session hook gives Claude a short project card and tells it to fetch everything it needs in one go:

```
atlas ask how are dependencies cached
atlas slice utils.py:solve_dependencies models.py:1-140 params.py:Depends
```

`ask` returns the code that best matches a question. `slice` returns any mix of symbols and line ranges from any files in a single call.

## Benchmark

Setup: `claude -p` with Sonnet, 3 tasks, 2 runs each, same system prompt in every arm. Repo: fastapi/fastapi (3,012 files).

| | Tokens | Cost | Turns |
|---|---|---|---|
| Plain Claude Code | 1,095,753 | $0.50 | 48 |
| token-savior (MCP) | 1,323,680 | $0.57 | 55 |
| project-atlas v2 | 797,870 | $0.45 | 32 |
| project-atlas v4 | 609,553 | $0.40 | 19 |

The hardest task asked how `Depends()` is resolved and cached. Plain Claude Code took 21 turns and 519k tokens; v4 took 6 turns and 226k tokens. All answers were correct in every arm: 5/5 call sites on the impact task, and all the key functions on the explanation task.

Cost falls less than tokens because output tokens and cache writes are roughly the same in every arm.

On a smaller repo (sindresorhus/ky), v2 used 29% fewer tokens than plain Claude Code and also beat token-savior.

Only 2 runs per setting, so differences under about 10% could be noise. The harness is in `bench/run_ab.py`. If you run it on your own repos, please open an issue with your numbers.

### What didn't work

v3 added two hooks: one injected relevant code into every prompt, the other blocked large whole-file reads. Claude responded with more small follow-up calls, and cost went up 35% over plain Claude Code. Both hooks are still in the code but off by default (`install-hook experimental`).

## Install

Needs Python 3.8+ and Claude Code. There are no dependencies, and nothing is sent anywhere.

```bash
git clone https://github.com/abhiyarecord-hue/project-atlas.git
cd project-atlas
sh install.sh                                            # macOS / Linux
powershell -ExecutionPolicy Bypass -File install.ps1     # Windows
```

This copies the skill to `~/.claude/skills/project-atlas/` and adds one `SessionStart` hook to `~/.claude/settings.json`. Your other settings are kept, and the old file is saved as `settings.json.bak`.

## Usage

There's nothing to run by hand. Open Claude Code in a project: the first time, it gets indexed (under a second for small repos, about 10 seconds for FastAPI). After that, each session starts with the project card.

A folder counts as a project if it has `.git`, `package.json`, `pyproject.toml`, `README.md` or a similar file, or at least 10 code files. Home directories, drive roots and folders with more than 40k files are skipped. To turn it off for one repo, add an empty `.noatlas` file. To turn it off everywhere, set `ATLAS_AUTO=0`.

Commands, if you want to use them yourself (`python ~/.claude/skills/project-atlas/atlas.py <cmd>`):

```
card                       project overview
ask <question>             best-matching code for a question
slice f1:sym f2:10-80 ...  exact code, many ranges at once
map [dir], outline <file>  symbols per file, with line ranges
find <words>, refs <sym>   symbol search, all usages
deps <file>                imports, importers, 2-hop impact
note <file|project> "..."  save a note; flagged when the file changes
savings, stats             tokens saved, index size
```

Notes live in `.atlas/notes.json`. Commit that file if you want to share them with your team; `index.json` is already gitignored.

Supported: Python (via `ast`), JS/TS (including Vue and Svelte), Go, Rust, Java, Kotlin, C#, Scala, Dart, C/C++, PHP, Ruby, Elixir, Swift, shell, PowerShell, Lua, R, SQL, GraphQL, Protobuf, HTML, CSS, Markdown, JSON, YAML and TOML.

## Uninstall

```bash
python ~/.claude/skills/project-atlas/atlas.py uninstall-hook
rm -rf ~/.claude/skills/project-atlas
```

## License

MIT
