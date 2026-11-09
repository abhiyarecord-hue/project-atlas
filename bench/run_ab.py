"""A/B runner: baseline vs token-savior vs project-atlas on the same repo and prompts."""
import json, os, subprocess, sys, threading, time, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
CLAUDE = shutil.which("claude") or shutil.which("claude.cmd")
REPO = os.environ.get("AB_REPO", "ky")
TASKSETS = {
  "ky": {
    "T1_improve": "Read the source code of this project and propose exactly 3 concrete improvements, each with a short code sketch. Do not edit any files.",
    "T2_retry": "How does this library implement retries? Explain which HTTP methods and status codes are retried by default, how the delay between attempts is computed, and how the Retry-After header is handled. Cite file:line for each point. Do not edit any files.",
    "T3_impact": "If the signature of the function that merges headers changed, which functions and files would be affected? List its definition and every call site in the source code with file:line. Do not edit any files.",
  },
  "fa": {
    "T1_improve": "Read the source code of this project's main package and propose exactly 3 concrete improvements, each with a short code sketch. Do not edit any files.",
    "T2_depends": "How does FastAPI resolve dependencies declared with Depends()? Explain how the dependency tree is built from function signatures, how sub-dependencies are solved at request time, and how a dependency's result is cached within a single request (and how to disable that). Cite file:line for each point. Do not edit any files.",
    "T3_impact": "If the signature of the function that builds a Dependant from a callable (get_dependant) changed, which functions and files would be affected? List its definition and every call site in the fastapi/ package source (not tests or docs) with file:line. Do not edit any files.",
  },
}
TASKS = TASKSETS[REPO]
# All arms run with skills disabled so system prompts are identical; atlas works through its SessionStart hook only.
ARMS = {
    "base":  dict(cwd=f"{REPO}-base",  mcp="mcp-empty.json", extra=["--disable-slash-commands"]),
    "ts":    dict(cwd=f"{REPO}-ts",    mcp=f"mcp-ts-{REPO}.json" if REPO != "ky" else "mcp-ts.json", extra=["--disable-slash-commands"]),
    "atlas": dict(cwd=f"{REPO}-atlas", mcp="mcp-empty.json", extra=["--disable-slash-commands"] if REPO != "ky" else []),
    "atlas4": dict(cwd=f"{REPO}-atlas4", mcp="mcp-empty.json", extra=["--disable-slash-commands"]),
    # v3: + prompt auto-context + read guard hooks (session card hook comes from user settings)
    "atlas3": dict(cwd=f"{REPO}-atlas3", mcp="mcp-empty.json",
                   extra=["--disable-slash-commands", "--settings", os.path.join(HERE, "settings-v3.json")]),
}

def run(arm, task, rnd, outdir):
    a = ARMS[arm]
    cmd = [CLAUDE, "-p", TASKS[task], "--output-format", "json", "--model", "sonnet",
           "--permission-mode", "dontAsk",
           "--allowedTools", "Read,Grep,Glob,Skill,Bash(ls:*),Bash(find:*),Bash(cat:*),Bash(grep:*),Bash(wc:*),Bash(head:*),"
           "Bash(python:*),mcp__token-savior-recall",
           "--disallowedTools", "Edit,Write,NotebookEdit,WebFetch,WebSearch,Agent",
           "--strict-mcp-config",
           "--mcp-config", os.path.join(HERE, a["mcp"])] + a["extra"]
    t0 = time.time()
    p = subprocess.run(cmd, cwd=os.path.join(HERE, a["cwd"]), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=900)
    try:
        j = json.loads(p.stdout)
    except Exception:
        j = {"error": p.stdout[-2000:] + p.stderr[-2000:]}
    j["_wall"] = round(time.time() - t0, 1)
    with open(os.path.join(outdir, f"{arm}_{task}_r{rnd}.json"), "w", encoding="utf-8") as f:
        json.dump(j, f, ensure_ascii=False, indent=1)
    u = j.get("usage", {})
    tot = sum(u.get(k, 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"))
    print(f"{arm:6} {task:11} r{rnd} tokens={tot:>8} cost=${j.get('total_cost_usd',0):.4f} turns={j.get('num_turns')} {j['_wall']}s", flush=True)

def main():
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    arms = sys.argv[2].split(",") if len(sys.argv) > 2 else list(ARMS)
    tasks = sys.argv[3].split(",") if len(sys.argv) > 3 else list(TASKS)
    outdir = os.path.join(HERE, os.environ.get("AB_OUT", "results")); os.makedirs(outdir, exist_ok=True)
    def worker(arm):
        for r in range(1, rounds + 1):
            for t in tasks:
                try:
                    run(arm, t, r, outdir)
                except Exception as e:
                    print(f"{arm} {t} r{r} FAILED {e}", flush=True)
    th = [threading.Thread(target=worker, args=(a,)) for a in arms]
    [x.start() for x in th]; [x.join() for x in th]

if __name__ == "__main__":
    main()
