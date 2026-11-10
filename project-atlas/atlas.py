#!/usr/bin/env python3
"""project-atlas: a persistent, incremental, token-budgeted map of a codebase.

Claude uses this instead of reading the workspace. The index is rebuilt
incrementally (only changed files are re-parsed) and the knowledge Claude
learns is stored as notes pinned to file hashes, so understanding compounds
across sessions and goes stale automatically when code changes.

Stdlib only. Usage:  python atlas.py <command> [args]   (run `help`)
"""
import ast, fnmatch, hashlib, json, os, re, sys, time
from collections import Counter, defaultdict

VERSION = 2  # bumped: v1 indexes built from CRLF files have wrong/missing symbols and must be rebuilt
ADIR = ".atlas"
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "bower_components", "vendor", "dist", "build",
             "out", "target", ".next", ".nuxt", ".svelte-kit", ".cache", "__pycache__", ".venv", "venv",
             "env", ".env", ".tox", ".mypy_cache", ".pytest_cache", ".idea", ".vscode", "coverage",
             ".gradle", "Pods", "DerivedData", ".atlas", ".turbo", ".parcel-cache", "site-packages",
             ".terraform", ".dart_tool", ".expo"}
BIN_EXT = set(""".png .jpg .jpeg .gif .webp .bmp .ico .icns .svgz .tif .tiff .psd .ai .mp3 .mp4 .wav .ogg
.flac .mov .avi .mkv .webm .zip .gz .tgz .bz2 .xz .7z .rar .jar .war .class .exe .dll .so .dylib .o .a
.lib .pdb .bin .dat .db .sqlite .sqlite3 .woff .woff2 .ttf .otf .eot .pdf .doc .docx .xls .xlsx .ppt
.pptx .pyc .pyo .lock .min.js .map .wasm .apk .ipa .dmg .iso .npy .npz .pt .pth .onnx .h5 .pkl
.parquet""".split())
LANG = {".py": "py", ".pyw": "py", ".js": "js", ".mjs": "js", ".cjs": "js", ".jsx": "js", ".ts": "js",
        ".tsx": "js", ".mts": "js", ".cts": "js", ".vue": "js", ".svelte": "js", ".astro": "js",
        ".go": "go", ".rs": "rs", ".java": "java", ".kt": "kt", ".kts": "kt", ".scala": "java",
        ".cs": "java", ".c": "c", ".h": "c", ".cc": "c", ".cpp": "c", ".cxx": "c", ".hpp": "c",
        ".hh": "c", ".m": "c", ".mm": "c", ".php": "php", ".rb": "rb", ".swift": "swift",
        ".dart": "java", ".sh": "sh", ".bash": "sh", ".zsh": "sh", ".ps1": "ps", ".psm1": "ps",
        ".md": "md", ".mdx": "md", ".markdown": "md", ".rst": "md", ".html": "html", ".htm": "html",
        ".css": "css", ".scss": "css", ".sass": "css", ".less": "css", ".sql": "sql", ".yml": "yaml",
        ".yaml": "yaml", ".toml": "toml", ".json": "json", ".lua": "lua", ".ex": "rb", ".exs": "rb",
        ".r": "r", ".R": "r", ".jl": "lua", ".zig": "rs", ".sol": "java", ".proto": "proto",
        ".graphql": "proto", ".gql": "proto", ".tf": "toml", ".ini": "toml", ".cfg": "toml",
        ".xml": "xml", ".txt": "txt"}
MAX_PARSE = 1_500_000
MAX_SYMS = 400

# ---------------------------------------------------------------- helpers
def tok(s):
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)
    return [t for t in re.split(r"[^A-Za-z0-9ऀ-ॿ]+", s.lower()) if t]

def est(s):  # rough token estimate
    return max(1, len(s) // 4)

class Out:
    def __init__(self, budget):
        self.budget, self.used, self.lines, self.cut = budget, 0, [], False
    def add(self, line=""):
        if self.cut:
            return False
        t = est(line) + 1
        if self.used + t > self.budget:
            self.cut = True
            return False
        self.used += t
        self.lines.append(line)
        return True
    def flush(self, hint="narrow with a path/query or raise --budget"):
        if self.cut:
            self.lines.append(f"… [truncated at ~{self.budget} tokens; {hint}]")
        print("\n".join(self.lines))

def find_root(start):
    d = os.path.abspath(start)
    while True:
        if os.path.isdir(os.path.join(d, ADIR)):
            return d
        p = os.path.dirname(d)
        if p == d:
            return None
        d = p

def read_text(path):
    with open(path, "rb") as f:
        raw = f.read(MAX_PARSE + 1)
    if b"\x00" in raw[:4096]:
        return None
    return raw.decode("utf-8", errors="replace")

def load_json(p, default):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(p, obj, pretty=False):
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        if pretty:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        else:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, p)

# ---------------------------------------------------------------- ignore rules
def load_ignores(root):
    pats = []
    for name in (".gitignore", ".atlasignore"):
        p = os.path.join(root, name)
        if os.path.isfile(p):
            for ln in open(p, encoding="utf-8", errors="replace"):
                ln = ln.strip()
                if ln and not ln.startswith("#") and not ln.startswith("!"):
                    pats.append(ln.rstrip("/").lstrip("/"))
    return pats

def ignored(rel, name, pats):
    for p in pats:
        if "/" in p:
            if fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(rel, p + "/*") or rel.startswith(p + "/"):
                return True
        elif fnmatch.fnmatch(name, p):
            return True
    return False

def walk(root):
    pats = load_ignores(root)
    for d, dirs, files in os.walk(root):
        reld = os.path.relpath(d, root).replace("\\", "/")
        reld = "" if reld == "." else reld
        dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS and not x.startswith(".")
                         and not ignored((reld + "/" + x).lstrip("/"), x, pats))
        for fn in sorted(files):
            rel = (reld + "/" + fn).lstrip("/")
            low = fn.lower()
            if any(low.endswith(e) for e in BIN_EXT) or ignored(rel, fn, pats):
                continue
            yield rel

# ---------------------------------------------------------------- extraction
def _sig(line, n=110):
    s = " ".join(line.strip().split())
    return s if len(s) <= n else s[: n - 1] + "…"

def py_extract(text):
    syms, imps = [], []
    try:
        tree = ast.parse(text)
    except Exception:
        return None
    lines = text.splitlines()
    def doc1(node):
        d = ast.get_docstring(node)
        return (" - " + d.strip().splitlines()[0][:70]) if d else ""
    def visit(body, prefix, depth):
        for n in body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                kind = "class" if isinstance(n, ast.ClassDef) else ("method" if prefix else "fn")
                start = n.lineno - len(getattr(n, "decorator_list", []) or [])
                hdr = lines[n.lineno - 1] if n.lineno - 1 < len(lines) else ""
                syms.append([kind, prefix + n.name, start, getattr(n, "end_lineno", n.lineno),
                             _sig(hdr) + doc1(n)])
                if depth < 1:
                    visit(n.body, prefix + n.name + ".", depth + 1)
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                if isinstance(n, ast.ImportFrom):
                    if n.module:
                        imps.append("." * n.level + n.module)
                    else:
                        imps.extend("." * n.level + a.name for a in n.names)
                else:
                    imps.extend(a.name for a in n.names)
            elif isinstance(n, ast.Assign) and not prefix and n.targets and isinstance(n.targets[0], ast.Name) \
                    and n.targets[0].id.isupper():
                syms.append(["const", n.targets[0].id, n.lineno, getattr(n, "end_lineno", n.lineno),
                             _sig(lines[n.lineno - 1])])
    visit(tree.body, "", 0)
    return syms, imps

R = lambda p, f=0: re.compile(p, f | re.M)
JS_RULES = [
    ("fn", R(r"^[ \t]*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*[(<]")),
    ("class", R(r"^[ \t]*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)")),
    ("fn", R(r"^[ \t]*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=\n]+)?=\s*(?:async\s+)?(?:function\b|\([^)\n]*\)\s*(?::[^=\n]+)?=>|[A-Za-z_$][\w$]*\s*=>)")),
    ("type", R(r"^[ \t]*(?:export\s+)?(?:declare\s+)?(?:interface|type|enum)\s+([A-Za-z_$][\w$]*)")),
    ("const", R(r"^(?:export\s+)?const\s+([A-Z][A-Z0-9_]{2,})\s*=")),
    ("const", R(r"^(?:export\s+)?(?:const|let)\s+([a-z_$][\w$]*)\s*(?::[^=\n]+)?=\s*(?:\[|\{|new\b|['\"`\d]|Object\.|Symbol)")),
    ("method", R(r"^[ \t]+(?:public\s+|private\s+|protected\s+|static\s+|async\s+|readonly\s+|override\s+|get\s+|set\s+)*(?!if\b|for\b|while\b|switch\b|catch\b|return\b|function\b|else\b|do\b)(#?[A-Za-z_$][\w$]*)\s*\([^)\n]*\)\s*(?::\s*[^{\n]+)?\{\s*$")),
    ("fn", R(r"^[ \t]*(?:module\.)?exports\.([A-Za-z_$][\w$]*)\s*=")),
]
JS_IMP = [R(r"""^\s*import\s[^'"\n]*?from\s*['"]([^'"]+)['"]"""), R(r"""^\s*import\s*['"]([^'"]+)['"]"""),
          R(r"""require\(\s*['"]([^'"]+)['"]\s*\)"""), R(r"""import\(\s*['"]([^'"]+)['"]\s*\)""")]
RULES = {
    "js": (JS_RULES, JS_IMP),
    "go": ([("fn", R(r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*[(\[]")),
            ("type", R(r"^type\s+([A-Za-z_]\w*)\s+"))],
           [R(r'^\s*(?:import\s+)?(?:[\w.]+\s+)?"([\w./-]+)"\s*$')]),
    "rs": ([("fn", R(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:const\s+)?(?:async\s+)?(?:unsafe\s+)?(?:extern\s+\"\w+\"\s+)?fn\s+([A-Za-z_]\w*)")),
            ("type", R(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait|union|type|mod)\s+([A-Za-z_]\w*)")),
            ("impl", R(r"^\s*impl(?:<[^>]*>)?\s+([\w:<>, ]+?)\s*(?:where|\{|$)"))],
           [R(r"^\s*(?:pub\s+)?use\s+([^;]+);")]),
    "java": ([("class", R(r"^\s*(?:@\w+\s+)*(?:(?:public|private|protected|internal|static|final|abstract|sealed|partial|data|open|override)\s+)*(?:class|interface|enum|record|struct|object|contract|library)\s+([A-Za-z_]\w*)")),
              ("method", R(r"^\s*(?:(?:public|private|protected|internal|static|final|abstract|override|virtual|async|synchronized|native|extern|unsafe|new)\s+)+[\w<>\[\],.?: ]+?\s+([A-Za-z_]\w*)\s*\([^;]*$")),
              ("method", R(r"^\s*(?:[\w<>\[\]?]+\s+)([a-z]\w*)\s*\([^;=]*\)\s*(?:async\s*)?\{\s*$"))],
             [R(r"^\s*(?:import|using)\s+(?:static\s+)?([\w.*]+)\s*;?")]),
    "kt": ([("class", R(r"^\s*(?:(?:public|private|internal|data|sealed|open|abstract|enum|inner|value)\s+)*(?:class|interface|object)\s+([A-Za-z_]\w*)")),
            ("fn", R(r"^\s*(?:(?:public|private|internal|override|suspend|inline|open|operator)\s+)*fun\s+(?:<[^>]+>\s*)?(?:[\w.<>]+\.)?([A-Za-z_]\w*)\s*\("))],
           [R(r"^\s*import\s+([\w.*]+)")]),
    "c": ([("fn", R(r"^(?!\s)(?!(?:if|for|while|switch|return|else|do|typedef|struct|enum)\b)[A-Za-z_][\w\s\*&:<>,~]*?\b([A-Za-z_~][\w:~]*)\s*\([^;{]*\)\s*(?:const\s*)?(?:noexcept\s*)?(?:override\s*)?\{?\s*$")),
           ("type", R(r"^\s*(?:typedef\s+)?(?:struct|class|enum|union|namespace)\s+([A-Za-z_]\w*)\s*(?::[^{]*)?\{?\s*$")),
           ("macro", R(r"^#define\s+([A-Za-z_]\w*)\("))],
          [R(r'^\s*#\s*(?:include|import)\s*[<"]([^>"]+)[>"]')]),
    "php": ([("fn", R(r"^\s*(?:(?:public|private|protected|static|abstract|final)\s+)*function\s+&?([A-Za-z_]\w*)")),
             ("class", R(r"^\s*(?:abstract\s+|final\s+)?(?:class|interface|trait|enum)\s+([A-Za-z_]\w*)"))],
            [R(r"^\s*use\s+([\w\\]+)"), R(r"""(?:require|include)(?:_once)?\s*\(?\s*['"]([^'"]+)['"]""")]),
    "rb": ([("fn", R(r"^\s*def\s+([\w.?!]+)")), ("class", R(r"^\s*(?:class|module|defmodule)\s+([\w:.]+)")),
            ("fn", R(r"^\s*defp?\s+([\w?!]+)"))],
           [R(r"""^\s*require(?:_relative)?\s+['"]([^'"]+)['"]"""), R(r"^\s*(?:import|alias|use)\s+([\w.]+)")]),
    "swift": ([("fn", R(r"^\s*(?:(?:public|private|internal|fileprivate|open|static|class|override|mutating|@\w+)\s+)*func\s+([A-Za-z_]\w*)")),
               ("class", R(r"^\s*(?:(?:public|private|internal|fileprivate|open|final)\s+)*(?:class|struct|enum|protocol|extension|actor)\s+([A-Za-z_][\w.]*)"))],
              [R(r"^\s*import\s+(\w+)")]),
    "sh": ([("fn", R(r"^\s*(?:function\s+)?([A-Za-z_][\w-]*)\s*\(\)\s*\{?"))], [R(r"^\s*(?:source|\.)\s+(\S+)")]),
    "ps": ([("fn", R(r"^\s*function\s+([\w-]+)", re.I))], [R(r"^\s*(?:Import-Module|\.)\s+(\S+)", re.I)]),
    "lua": ([("fn", R(r"^\s*(?:local\s+)?function\s+([\w.:]+)"))], [R(r"""require\s*\(?\s*['"]([^'"]+)['"]""")]),
    "r": ([("fn", R(r"^\s*([\w.]+)\s*(?:<-|=)\s*function\s*\("))], [R(r"^\s*(?:library|require|source)\(\s*['\"]?([\w./-]+)")]),
    "sql": ([("table", R(r"^\s*create\s+(?:or\s+replace\s+)?(?:temp(?:orary)?\s+)?(table|view|function|procedure|index|trigger|type)\s+(?:if\s+not\s+exists\s+)?([\w.\"`\[\]]+)", re.I))], []),
    "proto": ([("type", R(r"^\s*(?:message|service|enum|type|input|interface|rpc|query|mutation)\s+(\w+)"))], [R(r'^\s*import\s+"([^"]+)"')]),
    "yaml": ([("key", R(r"^([A-Za-z_][\w.-]*)\s*:"))], []),
    "toml": ([("key", R(r"^\[([^\]]+)\]")), ("key", R(r'^(?:resource|module|variable|output)\s+"([^"]+)"'))], []),
    "css": ([("rule", R(r"^(@media[^{]+|:root|[.#]?[A-Za-z][\w\-.#:\[\]=\"' >+~,*()]{0,80})\s*\{"))],
            [R(r"""^\s*@(?:import|use|forward)\s+(?:url\()?['"]([^'"]+)['"]""")]),
}
HTML_EXTRA = [("section", R(r"""<(?:section|main|nav|header|footer|aside|form|canvas|dialog|template|article|div)\b[^>]*\bid=["']([^"']+)["']""", re.I)),
              ("heading", R(r"<h[1-3][^>]*>\s*([^<]{2,80})", re.I)),
              ("title", R(r"<title>\s*([^<]{1,100})", re.I))]
HTML_IMP = [R(r"""<script\b[^>]*\bsrc=["']([^"']+)["']""", re.I), R(r"""<link\b[^>]*\bhref=["']([^"']+\.css[^"']*)["']""", re.I)]

def brace_end(lines, i, limit=3000):
    """End line of a block starting at line i: braces counted only outside (), strings and // comments."""
    depth, paren, seen = 0, 0, False
    for j in range(i, min(len(lines), i + limit)):
        s = re.sub(r"(['\"`])(?:\\.|(?!\1).)*\1", "", lines[j])
        s = re.sub(r"//.*$", "", s)
        for ch in s:
            if ch in "([":
                paren += 1
            elif ch in ")]":
                paren = max(0, paren - 1)
            elif ch == "{" and paren == 0:
                depth += 1; seen = True
            elif ch == "}" and paren == 0:
                depth -= 1
                if seen and depth <= 0:
                    return j + 1
        if not seen and paren == 0 and (s.rstrip().endswith(";") or (j - i >= 2 and not s.strip().endswith(("=>", "=", ",", "(")))):
            return j + 1
        if not seen and j - i >= 25:
            return i + 1
    return i + 1

def regex_extract(text, lang):
    lines = text.splitlines()
    if lang == "html":
        rules, imps_r = JS_RULES + HTML_EXTRA, HTML_IMP
    else:
        rules, imps_r = RULES.get(lang, ([], []))
    starts = [0]
    for ln in lines:
        starts.append(starts[-1] + len(ln) + 1)
    import bisect
    found = {}
    for kind, rx in rules:
        for m in rx.finditer(text):
            ln = bisect.bisect_right(starts, m.start()) - 1
            name = m.group(m.lastindex or 1) if lang != "sql" else m.group(1).lower() + " " + m.group(2)
            if not name or ln in found:
                continue
            if kind == "method" and name in ("constructor",) and lang == "js":
                pass
            found[ln] = [kind, name.strip(), ln + 1]
            if len(found) > MAX_SYMS * 3:
                break
    syms = []
    keys = sorted(found)
    brace = lang in ("js", "go", "rs", "java", "kt", "c", "php", "swift", "sh", "ps", "css", "html", "proto", "r")
    for idx, ln in enumerate(keys):
        kind, name, l1 = found[ln]
        if brace and kind not in ("section", "heading", "title", "key", "macro"):
            end = brace_end(lines, ln)
        else:
            end = (keys[idx + 1]) if idx + 1 < len(keys) else len(lines)
        syms.append([kind, name, l1, max(l1, end), _sig(lines[ln])])
    if lang in ("css", "yaml", "toml") and len(syms) > 80:
        syms = syms[:80]
    imps = []
    for rx in imps_r:
        imps += [m.group(1).strip() for m in rx.finditer(text)]
    return syms[:MAX_SYMS], list(dict.fromkeys(imps))[:150]

def md_extract(text):
    syms, lines, fence = [], text.splitlines(), False
    heads = []
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith("```"):
            fence = not fence
            continue
        if fence:
            continue
        m = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", ln)
        if m:
            heads.append((len(m.group(1)), m.group(2), i))
    for k, (lvl, title, i) in enumerate(heads):
        end = len(lines)
        for lvl2, _, j in heads[k + 1:]:
            if lvl2 <= lvl:
                end = j; break
        syms.append(["h%d" % lvl, title[:100], i + 1, end, ""])
    links = re.findall(r"\]\((?!https?:|#|mailto:)([^)\s]+)\)", text)
    return syms[:MAX_SYMS], list(dict.fromkeys(links))[:100]

def json_extract(text, rel):
    try:
        obj = json.loads(text)
    except Exception:
        return [], []
    syms = []
    if isinstance(obj, dict):
        lines = text.splitlines()
        for k in list(obj)[:60]:
            ln = next((i + 1 for i, l in enumerate(lines) if re.match(r'^\s{0,4}"%s"\s*:' % re.escape(k), l)), 1)
            v = obj[k]
            desc = (f"{{{len(v)} keys}}" if isinstance(v, dict) else f"[{len(v)}]" if isinstance(v, list)
                    else _sig(str(v), 60))
            syms.append(["key", k, ln, ln, desc])
    return syms, []

def extract(text, lang, rel):
    # Offsets are mapped to lines assuming one-character newlines; CRLF input would drift and crash.
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    if lang == "py":
        r = py_extract(text)
        if r is not None:
            return r
        return [], []
    if lang == "md":
        return md_extract(text)
    if lang == "json":
        return json_extract(text, rel)
    if lang in RULES or lang == "html":
        return regex_extract(text, lang)
    return [], []

# ---------------------------------------------------------------- index
def paths(root):
    a = os.path.join(root, ADIR)
    return a, os.path.join(a, "index.json"), os.path.join(a, "notes.json")

def scan(root, quiet=True):
    """Incremental update. Returns (index, delta dict)."""
    a, ip, np_ = paths(root)
    os.makedirs(a, exist_ok=True)
    gi = os.path.join(a, ".gitignore")
    if not os.path.exists(gi):
        open(gi, "w").write("index.json\n*.tmp\n")
    idx = load_json(ip, {})
    if idx.get("v") != VERSION:
        idx = {"v": VERSION, "files": {}}
    old = idx["files"]
    new, delta = {}, {"added": [], "modified": [], "deleted": []}
    t0, parsed = time.time(), 0
    for rel in walk(root):
        full = os.path.join(root, rel)
        try:
            st = os.stat(full)
        except OSError:
            continue
        prev = old.get(rel)
        if prev and prev["m"] == int(st.st_mtime) and prev["s"] == st.st_size:
            new[rel] = prev
            continue
        try:
            with open(full, "rb") as f:
                raw = f.read()
        except OSError:
            continue
        h = hashlib.sha1(raw).hexdigest()[:12]
        if prev and prev["h"] == h:
            prev["m"] = int(st.st_mtime)
            new[rel] = prev
            continue
        ext = os.path.splitext(rel)[1]
        lang = LANG.get(ext, LANG.get(ext.lower(), "other"))
        ent = {"h": h, "m": int(st.st_mtime), "s": st.st_size, "lang": lang, "l": 0, "sym": [], "imp": []}
        if b"\x00" not in raw[:4096]:
            text = raw.decode("utf-8", errors="replace")
            ent["l"] = text.count("\n") + 1
            if st.st_size <= MAX_PARSE:
                try:
                    ent["sym"], ent["imp"] = extract(text, lang, rel)
                except Exception:
                    pass
        else:
            ent["lang"] = "binary"
        parsed += 1
        new[rel] = ent
        (delta["modified"] if prev else delta["added"]).append(rel)
    delta["deleted"] = sorted(set(old) - set(new))
    changed = parsed or delta["deleted"]
    idx["files"] = new
    if changed or "built" not in idx:
        idx["built"] = int(time.time())
        save_json(ip, idx)
    delta["secs"] = round(time.time() - t0, 2)
    return idx, delta

def load(root, args):
    if getattr(args, "noscan", False):
        idx = load_json(paths(root)[1], None)
        if idx:
            return idx, {"added": [], "modified": [], "deleted": []}
    return scan(root)

def notes_load(root):
    n = load_json(paths(root)[2], {})
    n.setdefault("project", []); n.setdefault("files", {})
    return n

# ---------------------------------------------------------------- resolution
def resolve_file(idx, q):
    files = idx["files"]
    q = q.replace("\\", "/").lstrip("./")
    if q in files:
        return q
    cands = [f for f in files if f.endswith("/" + q) or f == q]
    if not cands:
        cands = [f for f in files if q.lower() in f.lower()]
    if len(cands) == 1:
        return cands[0]
    if cands:
        cands.sort(key=len)
        return cands[0]
    return None

def resolve_import(idx, src, imp):
    """Best-effort map an import string to an indexed file."""
    files = idx["files"]
    imp = imp.split(" as ")[0].strip().strip("{} ")
    if imp.startswith("http") or not imp:
        return None
    base = os.path.dirname(src)
    cands = []
    if imp.startswith("."):
        if "/" in imp or files[src]["lang"] != "py":
            p = os.path.normpath(os.path.join(base, imp)).replace("\\", "/")
        else:
            lvl = len(imp) - len(imp.lstrip("."))
            b = base
            for _ in range(lvl - 1):
                b = os.path.dirname(b)
            p = (b + "/" + imp.lstrip(".").replace(".", "/")).strip("/")
        cands.append(p)
    else:
        for pre in ("", "src/", "app/", "lib/"):
            cands.append(pre + imp.replace("::", "/").replace(".", "/") if files[src]["lang"] in ("py", "java", "kt", "rs") else pre + imp.lstrip("@/~"))
            cands.append(pre + imp.lstrip("@/~"))
    exts = ["", ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".vue", ".svelte", ".go", ".rs", ".java", ".kt",
            ".css", ".scss", ".h", ".hpp", ".rb", ".php", "/index.ts", "/index.tsx", "/index.js", "/__init__.py",
            "/mod.rs", ".md"]
    for c in cands:
        for e in exts:
            if (c + e) in files:
                return c + e
    return None

def graph(idx):
    out, rev = {}, defaultdict(set)
    for f, e in idx["files"].items():
        tgt = set()
        for imp in e.get("imp", []):
            r = resolve_import(idx, f, imp)
            if r and r != f:
                tgt.add(r); rev[r].add(f)
        out[f] = tgt
    return out, rev

# ---------------------------------------------------------------- commands
def cmd_init(root, args):
    idx, d = scan(root)
    fs = idx["files"]
    nsym = sum(len(e["sym"]) for e in fs.values())
    print(f"atlas ready: {len(fs)} files, {nsym} symbols indexed in {d['secs']}s → {root}/{ADIR}/")
    print("next: `atlas.py card`  (commit .atlas/notes.json to share learned knowledge)")

def manifests(root, fs):
    out = []
    if "package.json" in fs:
        p = load_json(os.path.join(root, "package.json"), {})
        if p:
            s = list((p.get("scripts") or {}).items())[:8]
            deps = list((p.get("dependencies") or {}))[:15]
            out.append(f"package.json: {p.get('name','')} | scripts: " + ", ".join(f"{k}=`{_sig(v,40)}`" for k, v in s))
            if deps:
                out.append("  deps: " + ", ".join(deps))
    for m in ("pyproject.toml", "requirements.txt", "go.mod", "Cargo.toml", "pom.xml", "build.gradle",
              "build.gradle.kts", "composer.json", "Gemfile", "pubspec.yaml", "Dockerfile", "docker-compose.yml",
              "Makefile", "CLAUDE.md", "README.md"):
        if m in fs:
            out.append(m)
    return out

ENTRY = re.compile(r"(^|/)(main|index|app|server|cli|__main__|manage|wsgi|asgi|program|lib|mod)\.\w+$", re.I)

def cmd_card(root, args):
    idx, d = load(root, args)
    fs, notes = idx["files"], notes_load(root)
    o = Out(args.budget or 1200)
    langs = Counter(); lines = Counter()
    for f, e in fs.items():
        langs[e["lang"]] += 1; lines[e["lang"]] += e["l"]
    tot = sum(lines.values())
    o.add(f"# ATLAS  {os.path.basename(root)}  - {len(fs)} files, {tot:,} lines, "
          f"{sum(len(e['sym']) for e in fs.values()):,} symbols (index {d['secs']}s)")
    o.add("langs: " + ", ".join(f"{k} {v}f/{lines[k]:,}L" for k, v in langs.most_common(8)))
    ch = d["added"] + d["modified"]
    if ch or d["deleted"]:
        o.add(f"CHANGED since last scan: +{len(d['added'])} ~{len(d['modified'])} -{len(d['deleted'])}: "
              + ", ".join((ch + ["-" + x for x in d["deleted"]])[:12]))
    for m in manifests(root, fs):
        o.add(m)
    # directory summary
    dirs = defaultdict(lambda: [0, 0])
    for f, e in fs.items():
        top = f.split("/")[0] if "/" in f else "."
        dirs[top][0] += 1; dirs[top][1] += e["l"]
    o.add("dirs: " + ", ".join(f"{k}/({v[0]}f,{v[1]:,}L)" for k, v in sorted(dirs.items(), key=lambda x: -x[1][1])[:14]))
    ent = [f for f in fs if ENTRY.search(f) and f.count("/") <= 2][:8]
    if ent:
        o.add("entry-ish: " + ", ".join(ent))
    _, rev = graph(idx)
    hubs = sorted(rev.items(), key=lambda x: -len(x[1]))[:8]
    if hubs and len(hubs[0][1]) > 1:
        o.add("hubs (most imported): " + ", ".join(f"{k}←{len(v)}" for k, v in hubs if len(v) > 1))
    big = sorted(fs.items(), key=lambda x: -x[1]["l"])[:6]
    o.add("largest: " + ", ".join(f"{k}({v['l']:,}L)" for k, v in big))
    if notes["project"]:
        o.add("## project knowledge")
        for n in notes["project"]:
            o.add("- " + n["t"])
    fn = notes["files"]
    if fn:
        o.add("## file knowledge (✱ = file changed since note; re-verify)")
        for f, rec in sorted(fn.items()):
            stale = "✱" if f not in fs or fs[f]["h"] != rec.get("h") else ""
            for n in rec["n"]:
                o.add(f"- {f}{stale}: {n}")
    sv = load_json(os.path.join(root, ADIR, "savings.json"), None)
    if sv and sv.get("tokens"):
        o.add(f"atlas has saved ~{sv['tokens']:,} tokens in this project since {sv['since']}")
    o.add("→ drill down: `find <query>` · `outline <file>` · `slice <file> <symbol>` · `map [dir]`")
    o.flush()

def cmd_map(root, args):
    idx, _ = load(root, args)
    pre = (args.target or "").replace("\\", "/").strip("/")
    o = Out(args.budget or 2500)
    cur = None
    for f, e in sorted(idx["files"].items()):
        if pre and not f.startswith(pre):
            continue
        dn = os.path.dirname(f)
        if dn != cur:
            cur = dn
            if not o.add(f"{dn or '.'}/"):
                break
        names = [s[1] for s in e["sym"] if s[0] not in ("method", "key", "rule", "heading", "section") and not s[0].startswith("h")
                 or s[0] in ("h1", "h2")]
        tail = (": " + ", ".join(names[:14]) + (f" +{len(names)-14}" if len(names) > 14 else "")) if names else ""
        if not o.add(f"  {os.path.basename(f)} [{e['l']}L]{tail}"):
            break
    o.flush("pass a subdirectory: `map src/api`")

def cmd_outline(root, args):
    idx, _ = load(root, args)
    o = Out(args.budget or 2000)
    for t in args.target.split(","):
        f = resolve_file(idx, t)
        if not f:
            print(f"no file matching '{t}'"); continue
        e = idx["files"][f]
        o.add(f"## {f}  [{e['lang']}, {e['l']} lines]")
        if e["imp"]:
            o.add("imports: " + ", ".join(e["imp"][:25]))
        _, rev = graph(idx)
        if rev.get(f):
            o.add("imported by: " + ", ".join(sorted(rev[f])[:15]))
        rec = notes_load(root)["files"].get(f)
        if rec:
            st = " (✱ file changed since)" if rec.get("h") != e["h"] else ""
            for n in rec["n"]:
                o.add(f"NOTE{st}: {n}")
        for k, name, a, b, sig in e["sym"]:
            ind = "  " * (name.count(".") if e["lang"] == "py" else 0)
            if k.startswith("h") and k[1:].isdigit():
                ind = "  " * (int(k[1:]) - 1); sig = ""
            if not o.add(f"{ind}{a}-{b} {k} {name}" + (f" | {sig}" if sig and sig != name else "")):
                break
    o.flush("use `slice` for code or `outline` a single file")

def find_symbol(idx, f, name):
    syms = idx["files"][f]["sym"]
    nl = name.lower()
    for pred in (lambda s: s[1] == name, lambda s: s[1].lower() == nl,
                 lambda s: s[1].split(".")[-1].lower() == nl, lambda s: nl in s[1].lower()):
        hit = [s for s in syms if pred(s)]
        if hit:
            return hit[0]
    return None

def _spec_range(idx, f, spec):
    if spec and re.fullmatch(r"\d+(-\d+)?", spec):
        a, _, b = spec.partition("-"); a = int(a); b = int(b or a + 40)
        return a, b, spec
    if spec:
        s = find_symbol(idx, f, spec)
        if not s:
            return None
        return s[2], s[3], f"{s[0]} {s[1]}"
    return 1, 80, "head"

def cmd_slice(root, args):
    """slice <file> <sym|a-b> [more specs…]   or batch:  slice fileA:sym fileB:10-80 fileC:12 …"""
    idx, _ = load(root, args)
    toks = [args.target] + list(args.rest)
    reqs, cur = [], None   # list of (file, spec)
    for t in toks:
        m = re.match(r"^(.*[^:\\/]):([^:\\/]+)$", t)
        if m and not re.match(r"^[A-Za-z]$", m.group(1)) and resolve_file(idx, m.group(1)):
            cur = resolve_file(idx, m.group(1)); reqs.append((cur, m.group(2)))
        elif cur is None or (resolve_file(idx, t) and "." in t and not reqs_has_spec_only(t)):
            f = resolve_file(idx, t)
            if not f:
                print(f"no file matching '{t}'"); return
            cur = f; reqs.append((f, None))
        else:
            if reqs and reqs[-1][0] == cur and reqs[-1][1] is None:
                reqs[-1] = (cur, t)
            else:
                reqs.append((cur, t))
    # resolve to ranges, merge overlaps per file
    ranges = defaultdict(list)
    for f, spec in reqs:
        r = _spec_range(idx, f, spec)
        if not r:
            print(f"symbol '{spec}' not in {f}. symbols: " + ", ".join(x[1] for x in idx["files"][f]["sym"][:40]))
            continue
        ranges[f].append(list(r))
    o = Out(args.budget or (4000 if len(reqs) <= 1 else 9000))
    last = ""
    for f, rs in ranges.items():
        rs.sort()
        merged = []
        for a, b, lab in rs:
            if merged and a <= merged[-1][1] + 3:
                merged[-1][1] = max(merged[-1][1], b); merged[-1][2] += ", " + lab
            else:
                merged.append([a, b, lab])
        lines = (read_text(os.path.join(root, f)) or "").splitlines()
        for a, b, lab in merged:
            o.add(f"## {f}:{a}-{b}  ({lab})")
            w = len(str(b))
            for i in range(a - 1, min(b, len(lines))):
                if not o.add(f"{i+1:>{w}}│{lines[i]}"):
                    last = f"{f}:{i+1}-{b}"
                    break
    o.flush(f"continue with `slice {last}`" if last else "")

def reqs_has_spec_only(t):
    return bool(re.fullmatch(r"\d+(-\d+)?", t))

def stem(t):
    for suf in ("ies", "ing", "ers", "ed", "es", "er", "s", "d"):
        if len(t) > len(suf) + 3 and t.endswith(suf):
            return t[: -len(suf)] + ("y" if suf == "ies" else "")
    return t

def score_sym(qt, qraw, f, s, w=None):
    name = s[1].lower(); nt = set(tok(s[1])); pt = set(tok(f)); sig = (s[4] or "").lower()
    ns = {stem(x) for x in nt}
    sc = 0
    if qraw == name or qraw == name.split(".")[-1]:
        sc += 20
    for t in qt:
        k = w.get(t, 1.0) if w else 1.0
        if t == name or t == name.split(".")[-1]:
            sc += 10 * k
        elif t in nt:
            sc += 6 * k
        elif stem(t) in ns:
            sc += 5 * k
        elif len(t) > 2 and t in name:
            sc += 3 * k
        elif len(t) > 2 and any(x.startswith(t) for x in nt):
            sc += 2 * k
        if t in pt:
            sc += 2 * k
        elif len(t) > 3 and t in sig:
            sc += 1 * k
    if sc and s[0] in ("class", "fn", "type", "table"):
        sc += 1
    return sc

def cmd_find(root, args):
    idx, _ = load(root, args)
    q = " ".join([args.target] + args.rest)
    qraw, qt = q.lower().strip(), tok(q)
    notes = notes_load(root)
    o = Out(args.budget or 1500)
    hits = []
    for f, e in idx["files"].items():
        fscore = sum(2 for t in qt if t in set(tok(f)))
        for s in e["sym"]:
            sc = score_sym(qt, qraw, f, s)
            if sc >= max(3, len(qt) * 2):
                hits.append((sc, f, s))
        if fscore >= 2 * len(qt) and qt:
            hits.append((fscore + 1, f, ["file", os.path.basename(f), 1, e["l"], f"{e['l']} lines"]))
    hits.sort(key=lambda x: (-x[0], x[1], x[2][2]))
    for f, rec in notes["files"].items():
        for n in rec["n"]:
            if sum(t in n.lower() for t in qt) >= max(1, len(qt) // 2 + 1):
                o.add(f"NOTE {f}: {n}")
    for n in notes["project"]:
        if sum(t in n["t"].lower() for t in qt) >= max(1, len(qt) // 2 + 1):
            o.add(f"NOTE project: {n['t']}")
    seen = 0
    for sc, f, s in hits[: args.k or 25]:
        o.add(f"{f}:{s[2]}-{s[3]} {s[0]} {s[1]}" + (f" | {s[4]}" if s[4] and s[4] != s[1] else ""))
        seen += 1
    if seen < 3:  # fall back to text search across indexed files
        rx = re.compile("|".join(re.escape(t) for t in ([qraw] if " " not in qraw else qt) or [qraw]), re.I)
        grep_into(root, idx, rx, o, limit=20, header="text matches:")
    if not o.lines:
        o.add("no matches")
    o.flush()

def cmd_ask(root, args):
    """One-shot: rank symbols for a question and print the code of the best ones (answer-ready context)."""
    idx, _ = load(root, args)
    q = " ".join([args.target] + args.rest)
    stop = {"how", "does", "the", "what", "which", "where", "is", "are", "a", "an", "of", "in", "to", "and",
            "or", "for", "this", "that", "with", "by", "be", "it", "its", "do", "if", "on", "when", "why", "who",
            "explain", "implement", "implemented", "library", "code", "file", "function", "functions"}
    stop |= getattr(args, "extra_stop", set())
    qt = list(dict.fromkeys(t for t in tok(q) if t not in stop and len(t) > 1)) or tok(q)
    qraw = q.lower().strip()
    o = Out(args.budget or 4000)
    hits = []
    wants_tests = any(t.startswith(("test", "spec")) for t in qt)
    # IDF-style weights: a word found in most file paths (e.g. the project's own name) says little.
    code_files = [f for f, e in idx["files"].items() if e["sym"] and e["lang"] not in ("md", "json", "yaml", "toml")]
    nfiles = max(1, len(code_files))
    w = {}
    for t in qt:
        df = sum(1 for f in code_files if t in f.lower())
        w[t] = 0.15 if df / nfiles > 0.3 else 1.0
    for f, e in idx["files"].items():
        if e["lang"] in ("md", "json", "yaml", "toml", "css", "html") and len(idx["files"]) > 5:
            continue
        for s in e["sym"]:
            sc = score_sym(qt, qraw, f, s, w)
            if not wants_tests and re.search(r"(^|/)(tests?([-_]\w+)?|__tests__|specs?|fixtures?|examples?|benchmarks?|docs?([-_]\w+)?|samples?|demos?)/|\.(test|spec)\.", f):
                continue
            if sc >= 4:
                hits.append((sc, f, s))
    hits.sort(key=lambda x: (-x[0], x[1], x[2][2]))
    global ASK_BEST
    ASK_BEST = hits[0][0] if hits else 0
    if getattr(args, "min_score", 0) and ASK_BEST < args.min_score:
        return
    notes = notes_load(root)
    for f, rec in notes["files"].items():
        for n in rec["n"]:
            if sum(t in n.lower() for t in qt) >= max(1, len(qt) // 2):
                o.add(f"NOTE {f}: {n}")
    for n in notes["project"]:
        if sum(t in n["t"].lower() for t in qt) >= max(1, len(qt) // 2):
            o.add(f"NOTE project: {n['t']}")
    o.add(f"# ask: {q}  (top matches; code shown for the best - rest listed)")
    taken, shown, texts, listed = [], 0, {}, []
    for sc, f, s in hits[:40]:
        a, b = s[2], s[3]
        if any(f == tf and ta <= a and b <= tb for tf, ta, tb in taken):
            continue
        big = b - a > (160 if shown < 2 else 120)
        if shown < (args.k or 6) and not big and o.used < o.budget * 0.8:
            if f not in texts:
                texts[f] = (read_text(os.path.join(root, f)) or "").splitlines()
            lines = texts[f]
            o.add(f"## {f}:{a}-{b} {s[0]} {s[1]}")
            for i in range(a - 1, min(b, len(lines))):
                if not o.add(f"{i+1}│{lines[i]}"):
                    break
            taken.append((f, a, b)); shown += 1
        else:
            listed.append(f"{f}:{a}-{b} {s[0]} {s[1]}" + (" (large: use outline/slice a-b)" if big else ""))
    if listed:
        o.add("## more candidates")
        for x in listed[:15]:
            o.add(x)
    if shown < 2 and not getattr(args, "nogrep", False):
        rx = re.compile("|".join(re.escape(t) for t in qt if len(t) > 2) or re.escape(qraw), re.I)
        grep_into(root, idx, rx, o, limit=25, header="## text matches")
    o.flush("ask a narrower question or `slice <file> a-b`")

# ---------------------------------------------------------------- live hooks (v3)
ASK_BEST = 0
PROMPT_STOP = set("""if changed change would could should which what where when why how who whom whose list every each
all any some site sites source sources edit edits editing not no do does did done make makes made give tell show
explain describe propose concrete improvement improvements short sketch sketches code project repo repository package
main read write files file please can you me my we our us i want need use using used also just only like then than
there here from into about after before between under over more most less few many much exactly point points cite
line lines number numbers affected affect call calls between within single request way ways thing things""".split())

def _hook_input():
    try:
        return json.loads(sys.stdin.read() or "{}")
    except Exception:
        return {}

def _savings_add(root, kind, tokens):
    p = os.path.join(root, ADIR, "savings.json")
    s = load_json(p, {"since": time.strftime("%Y-%m-%d"), "tokens": 0, "events": {}})
    s["tokens"] += int(tokens)
    s["events"][kind] = s["events"].get(kind, 0) + 1
    try:
        save_json(p, s)
    except Exception:
        pass

def cmd_prompt_hook(root, args):
    """UserPromptSubmit: put the code the prompt is about into context *before* Claude starts, so it needs fewer turns."""
    data = _hook_input()
    prompt = (data.get("prompt") or "").strip()
    r = find_root(data.get("cwd") or os.getcwd())
    if not r or len(prompt) < 12 or prompt.startswith("/"):
        return
    import io, contextlib, argparse
    a = argparse.Namespace(target=prompt[:600], rest=[], budget=args.budget or 2500, k=5, noscan=False,
                           extra_stop=PROMPT_STOP, min_score=12, nogrep=True)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            cmd_ask(r, a)
    except Exception:
        return
    out = buf.getvalue().strip()
    if out and out.count("\n## ") + out.startswith("## ") >= 1 and "│" in out:
        print("[project-atlas] Code most relevant to this prompt (auto-retrieved; verify, then extend with atlas slice/ask only if needed):")
        print(out)

GUARD_LINES = 250

def cmd_read_guard(root, args):
    """PreToolUse(Read): the first whole-file Read of a big indexed file gets its outline instead. A repeat Read is allowed."""
    data = _hook_input()
    ti = data.get("tool_input") or {}
    fp = ti.get("file_path") or ""
    if not fp or ti.get("limit") or ti.get("offset") or os.environ.get("ATLAS_GUARD", "1") == "0":
        return
    r = find_root(os.path.dirname(os.path.abspath(fp)))
    if not r:
        return
    rel = os.path.relpath(os.path.abspath(fp), r).replace("\\", "/")
    idx = load_json(paths(r)[1], None)
    e = idx and idx["files"].get(rel)
    if not e or e["l"] < GUARD_LINES or e["lang"] in ("md", "txt", "json", "other", "binary") or not e["sym"]:
        return
    seen_p = os.path.join(r, ADIR, "guard_seen.json")
    seen = load_json(seen_p, {})
    sid = data.get("session_id", "?")
    key = sid + "|" + rel
    if key in seen:
        return  # second full read in this session: allow (e.g. needed before Edit)
    seen = {k: v for k, v in seen.items() if time.time() - v < 86400}
    seen[key] = time.time()
    save_json(seen_p, seen)
    import io, contextlib, argparse
    a = argparse.Namespace(target=rel, rest=[], budget=1500, k=0, noscan=True)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cmd_outline(r, a)
    outline = buf.getvalue()
    saved = max(0, e["s"] // 4 - est(outline))
    _savings_add(r, "read_guard", saved)
    reason = (f"[project-atlas] {rel} has {e['l']} lines (~{e['s']//4:,} tokens). Outline below instead - "
              f"Read only the range you need with offset/limit (or atlas slice). Repeating the same full Read is allowed.\n{outline}")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": reason}}))

def cmd_savings(root, args):
    s = load_json(os.path.join(root, ADIR, "savings.json"), None)
    if not s:
        print("no savings recorded yet"); return
    usd = s["tokens"] / 1e6 * 3
    print(f"project-atlas saved ~{s['tokens']:,} input tokens here since {s['since']} "
          f"(≈ ${usd:,.2f} at $3/M) - events: " + ", ".join(f"{k}×{v}" for k, v in s["events"].items()))

def grep_into(root, idx, rx, o, limit=40, header=None, skip_def=None):
    n = 0
    if header:
        o.add(header)
    for f, e in idx["files"].items():
        if e["lang"] in ("binary",) or e["s"] > MAX_PARSE:
            continue
        text = read_text(os.path.join(root, f))
        if not text or not rx.search(text):
            continue
        for i, ln in enumerate(text.splitlines()):
            if rx.search(ln):
                if skip_def and skip_def(f, i + 1):
                    continue
                if not o.add(f"{f}:{i+1}: {_sig(ln, 140)}"):
                    return n
                n += 1
                if n >= limit:
                    o.add(f"… more (limit {limit}; use --k)")
                    return n
    return n

def cmd_refs(root, args):
    idx, _ = load(root, args)
    name = args.target.split(".")[-1]
    rx = re.compile(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])")
    defs = {(f, s[2]) for f, e in idx["files"].items() for s in e["sym"] if s[1].split(".")[-1] == name}
    o = Out(args.budget or 1500)
    for f, l in sorted(defs):
        o.add(f"DEF {f}:{l}")
    n = grep_into(root, idx, rx, o, limit=args.k or 40, skip_def=lambda f, l: (f, l) in defs)
    if not n and not defs:
        o.add("no references")
    o.flush()

def cmd_deps(root, args):
    idx, _ = load(root, args)
    f = resolve_file(idx, args.target)
    if not f:
        print("no such file"); return
    out, rev = graph(idx)
    o = Out(args.budget or 1000)
    e = idx["files"][f]
    o.add(f"## {f}")
    o.add("uses (local): " + (", ".join(sorted(out[f])) or "-"))
    ext = [i for i in e["imp"] if not resolve_import(idx, f, i)]
    o.add("uses (external): " + (", ".join(ext[:30]) or "-"))
    o.add("used by: " + (", ".join(sorted(rev.get(f, []))) or "-"))
    # 2-hop blast radius
    two = set()
    for x in rev.get(f, []):
        two |= rev.get(x, set())
    two -= set(rev.get(f, [])) | {f}
    if two:
        o.add("blast radius (2-hop): " + ", ".join(sorted(two)[:30]))
    o.flush()

def cmd_changed(root, args):
    _, d = scan(root)
    if not (d["added"] or d["modified"] or d["deleted"]):
        print("no changes since last scan"); return
    for k in ("added", "modified", "deleted"):
        if d[k]:
            print(f"{k}: " + ", ".join(d[k][:60]))

def cmd_note(root, args):
    idx, _ = load(root, args)
    text = " ".join(args.rest).strip()
    if not text:
        print('usage: note <file|project> "insight"'); return
    n = notes_load(root)
    if args.target in ("project", "-", "."):
        if not any(x["t"] == text for x in n["project"]):
            n["project"].append({"t": text, "at": time.strftime("%Y-%m-%d")})
        where = "project"
    else:
        f = resolve_file(idx, args.target)
        if not f:
            print(f"no file matching '{args.target}'"); return
        rec = n["files"].setdefault(f, {"h": idx["files"][f]["h"], "n": []})
        rec["h"] = idx["files"][f]["h"]
        if text not in rec["n"]:
            rec["n"].append(text)
        where = f
    save_json(paths(root)[2], n, pretty=True)
    print(f"noted on {where}")

def cmd_forget(root, args):
    n = notes_load(root)
    tgt = args.target
    if tgt == "project":
        n["project"] = [x for x in n["project"] if args.rest and args.rest[0].lower() not in x["t"].lower()] if args.rest else []
    else:
        idx, _ = load(root, args)
        f = resolve_file(idx, tgt) or tgt
        if args.rest and f in n["files"]:
            n["files"][f]["n"] = [x for x in n["files"][f]["n"] if args.rest[0].lower() not in x.lower()]
            if not n["files"][f]["n"]:
                del n["files"][f]
        else:
            n["files"].pop(f, None)
    save_json(paths(root)[2], n, pretty=True)
    print("forgotten")

def cmd_refresh(root, args):
    """Mark notes on a file as verified against the current version."""
    idx, _ = load(root, args)
    n = notes_load(root)
    f = resolve_file(idx, args.target)
    if f and f in n["files"]:
        n["files"][f]["h"] = idx["files"][f]["h"]
        save_json(paths(root)[2], n, pretty=True)
        print(f"notes on {f} marked current")
    else:
        print("no notes for that file")

def cmd_stats(root, args):
    idx, _ = load(root, args)
    fs = idx["files"]
    raw = sum(e["s"] for e in fs.values() if e["lang"] != "binary")
    nsym = sum(len(e["sym"]) for e in fs.values())
    print(f"files {len(fs)} | text bytes {raw:,} (~{raw//4:,} tokens if read whole) | symbols {nsym:,}")
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cmd_card(root, args)
    c = buf.getvalue()
    print(f"card ≈ {est(c):,} tokens → {raw//4 / max(1, est(c)):,.0f}x smaller than reading everything")

PROJECT_MARKERS = (".git", "package.json", "pyproject.toml", "setup.py", "requirements.txt", "go.mod", "Cargo.toml",
                   "pom.xml", "build.gradle", "build.gradle.kts", "composer.json", "Gemfile", "pubspec.yaml",
                   "CLAUDE.md", "Makefile", "CMakeLists.txt", "deno.json", "index.html", "README.md")

def auto_root(cwd):
    """Decide whether an un-indexed folder should be indexed automatically. Returns a reason or None."""
    home = os.path.abspath(os.path.expanduser("~"))
    cwd = os.path.abspath(cwd)
    if os.environ.get("ATLAS_AUTO", "1") == "0" or cwd in (home, os.path.dirname(home)) \
            or os.path.dirname(cwd) == cwd or os.path.exists(os.path.join(cwd, ".noatlas")):
        return None
    if any(os.path.exists(os.path.join(cwd, m)) for m in PROJECT_MARKERS):
        return "project"
    n = 0
    for rel in walk(cwd):
        if LANG.get(os.path.splitext(rel)[1]) not in (None, "txt", "other"):
            n += 1
            if n >= 10:
                return "files"
    return None

def _opted_out(cwd, root):
    """True if `.noatlas` exists in cwd or any folder between it and the index root."""
    d = os.path.abspath(cwd)
    stop = os.path.abspath(root) if root else d
    while True:
        if os.path.exists(os.path.join(d, ".noatlas")):
            return True
        p = os.path.dirname(d)
        if d == stop or p == d:
            return False
        d = p

def cmd_hook(root, args):
    if os.environ.get("ATLAS_AUTO", "1") == "0":
        return  # global off switch: applies to already-indexed projects too
    r = find_root(os.getcwd())
    if r and os.path.abspath(r) == os.path.abspath(os.path.expanduser("~")) and os.getcwd() != r:
        r = None  # never let a stray ~/.atlas capture every project
    if r and _opted_out(os.getcwd(), r):
        return  # `.noatlas` wins even inside an indexed parent
    if not r:
        if not auto_root(os.getcwd()):
            return  # not a project (home dir, drive root, opted out)
        count = sum(1 for _ in zip(range(40001), walk(os.getcwd())))
        if count > 40000:
            print("[project-atlas] Big folder (40k+ files) - not auto-indexed. Ask Claude to run `atlas.py init` here if wanted.")
            return
        r = os.getcwd()
        try:
            _, d = scan(r)
            print(f"[project-atlas] First visit: indexed this project automatically ({d['secs']}s). "
                  "Opt out with an empty `.noatlas` file.")
        except Exception as ex:
            print(f"[project-atlas] auto-index failed: {ex}")
            return
    try:
        args.budget = args.budget or 900
        me = os.path.abspath(__file__).replace("\\", "/")
        print(f"""[project-atlas] This project is indexed. Before Glob/Grep/whole-file Read, query it with Bash
(run from the project dir, no `cd`, no shell variables; no need to load the skill):
  python "{me}" ask <question words>   → best-matching code bodies in ONE call (start here)
  python "{me}" slice a.py:func b.py:120-180 c.ts:ClassName.method   → MANY ranges/symbols in ONE call
  python "{me}" find <words> | outline <file> | refs <symbol> | deps <file>
  python "{me}" note <file|project> "<non-obvious insight>"   (saves knowledge for future sessions)
COST RULE: every tool call re-sends the whole conversation, so the number of calls matters more than output size.
Plan first, then fetch everything you need together: after `ask`/`outline`, request ALL remaining ranges in a
single batched `slice` call (accept ~3–8k tokens of output) instead of many small calls. Aim for ≤3 calls per question.""")
        cmd_card(r, args)
    except Exception as ex:
        print(f"[project-atlas] card failed: {ex}")

def _settings_path(home=None):
    return os.path.join(home or os.path.expanduser("~"), ".claude", "settings.json")

def _hook_cmd():
    py = "python" if sys.platform == "win32" else (os.path.basename(sys.executable) or "python3")
    return f'{py} "{os.path.abspath(__file__).replace(chr(92), "/")}" hook'

def cmd_install_hook(root, args):
    """Add (or update) the SessionStart hook in ~/.claude/settings.json, keeping everything else."""
    sp = _settings_path(args.root)
    os.makedirs(os.path.dirname(sp), exist_ok=True)
    s = load_json(sp, None)
    if s is None:
        if os.path.exists(sp):
            print(f"{sp} is not valid JSON; fix it first, nothing changed"); return
        s = {}
    else:
        import shutil
        shutil.copy2(sp, sp + ".bak")
    base = _hook_cmd()[: -len(" hook")]
    # Default = session card only (the configuration that won the A/B). The experimental hooks
    # (prompt auto-context, read guard) measured worse on FastAPI and are opt-in: install-hook --target experimental
    wanted = {"SessionStart": ("startup|resume|clear|compact", base + " hook", 30)}
    if args.target == "experimental":
        wanted["UserPromptSubmit"] = (None, base + " prompt-hook", 15)
        wanted["PreToolUse"] = ("Read", base + " read-guard", 10)
    for event in ("SessionStart", "UserPromptSubmit", "PreToolUse"):
        groups = s.setdefault("hooks", {}).setdefault(event, [])
        for g in groups:
            g["hooks"] = [h for h in g.get("hooks", []) if "project-atlas" not in h.get("command", "")]
        groups[:] = [g for g in groups if g.get("hooks")]
        if event in wanted:
            matcher, cmd, to = wanted[event]
            g = {"hooks": [{"type": "command", "command": cmd, "timeout": to}]}
            if matcher:
                g = {"matcher": matcher, **g}
            groups.append(g)
        if not groups:
            del s["hooks"][event]
    save_json(sp, s, pretty=True)
    print(f"hooks installed in {sp}: " + ", ".join(wanted))

def cmd_uninstall_hook(root, args):
    sp = _settings_path(args.root)
    s = load_json(sp, None)
    if not s:
        print("no settings found"); return
    hooks = s.get("hooks", {})
    for event in list(hooks):
        groups = hooks[event]
        for g in groups:
            g["hooks"] = [h for h in g.get("hooks", []) if "project-atlas" not in h.get("command", "")]
        hooks[event] = [g for g in groups if g.get("hooks")]
        if not hooks[event]:
            del hooks[event]
    if "hooks" in s and not s["hooks"]:
        del s["hooks"]
    save_json(sp, s, pretty=True)
    print("project-atlas hooks removed")

HELP = """project-atlas - query a codebase without reading it.
  init                     build index in ./.atlas (incremental afterwards, automatic)
  card                     L0: project overview + changes + learned notes (~1k tokens)
  map [dir]                L1: tree with top-level symbol names per file
  outline <file[,file]>    L2: every symbol with line ranges + imports + notes
  slice <file> <sym|a-b>   L3: exact code of one symbol or line range
  ask <question words>     ONE call: ranked symbols + their code bodies (best first step)
  find <query words>       ranked symbol search (+ notes, + text fallback)
  refs <symbol>            definitions + every usage site
  deps <file>              local/external imports, importers, 2-hop blast radius
  changed                  files changed since last scan
  note <file|project> "…"  persist an insight (auto-flagged ✱ when file changes)
  refresh <file>           mark a file's notes as re-verified
  forget <file|project> [substr]
  stats                    token savings vs reading the whole tree
  install-hook / uninstall-hook   auto-load the card at every Claude Code session start
options: --budget N (token cap)  --k N (max results)  --root DIR  --noscan"""

def main(argv):
    import argparse
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("cmd", nargs="?", default="help")
    p.add_argument("target", nargs="?", default="")
    p.add_argument("rest", nargs="*")
    p.add_argument("--budget", type=int, default=0)
    p.add_argument("--k", type=int, default=0)
    p.add_argument("--root", default=None)
    p.add_argument("--noscan", action="store_true")
    args = p.parse_args(argv)
    cmds = {"init": cmd_init, "card": cmd_card, "map": cmd_map, "outline": cmd_outline, "slice": cmd_slice,
            "find": cmd_find, "ask": cmd_ask, "refs": cmd_refs, "deps": cmd_deps, "changed": cmd_changed, "note": cmd_note,
            "forget": cmd_forget, "refresh": cmd_refresh, "stats": cmd_stats, "hook": cmd_hook,
            "install-hook": cmd_install_hook, "uninstall-hook": cmd_uninstall_hook,
            "prompt-hook": cmd_prompt_hook, "read-guard": cmd_read_guard, "savings": cmd_savings}
    if args.cmd not in cmds:
        print(HELP); return
    if args.cmd == "hook":
        return cmd_hook(None, args)
    if args.cmd in ("prompt-hook", "read-guard"):
        try:
            return cmds[args.cmd](None, args)
        except Exception:
            return  # hooks must never break a session
    if args.cmd in ("install-hook", "uninstall-hook"):
        return cmds[args.cmd](None, args)  # --root here = alternate home dir (testing)
    root = os.path.abspath(args.root) if args.root else (find_root(os.getcwd()) or os.getcwd())
    if args.cmd != "init" and not os.path.isdir(os.path.join(root, ADIR)):
        print(f"(no .atlas yet - building index in {root})")
    if args.cmd in ("outline", "slice", "refs", "deps", "refresh", "find", "ask") and not args.target:
        print(HELP); return
    cmds[args.cmd](root, args)

if __name__ == "__main__":
    main(sys.argv[1:])
