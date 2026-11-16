#!/usr/bin/env python3
"""spec_hunter: find hardware repos (SystemVerilog / Verilog / VHDL) on GitHub and collect
every kind of specification document they contain, to compare spec styles.

Uses the official GitHub REST API (no HTML scraping). Steps:

  search    find repos by language and star count            -> out/repo_list.jsonl
  scan      list each repo's files, classify spec documents,  -> out/spec_files.csv
            link them to RTL modules                             out/repos.csv
  download  fetch the spec files themselves (text, optionally PDF/DOCX/XLSX) -> out/files/
  analyze   count section headings across downloaded specs   -> out/headings.csv
  all       search + scan + download + analyze

Run `python spec_hunter.py <step> -h` for options. See README.md.
"""
import argparse
import csv
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import quote

import requests

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
OUT = Path("out")

HDL_EXT = {".sv": "SystemVerilog", ".svh": "SystemVerilog", ".v": "Verilog", ".vh": "Verilog",
           ".vhd": "VHDL", ".vhdl": "VHDL"}
TEXT_EXT = {".md", ".rst", ".adoc", ".asciidoc", ".txt", ".tex", ".org", ".html"}
BINARY_DOC_EXT = {".pdf", ".doc", ".docx", ".odt", ".xlsx", ".xls", ".ods"}
DOC_DIRS = {"doc", "docs", "documentation", "spec", "specs", "specification", "manual", "datasheet"}
SPEC_WORDS = re.compile(r"spec|specification|datasheet|data_sheet|theory_of_operation|architecture|"
                        r"design|manual|user_?guide|programmers?_guide|interface|protocol|"
                        r"microarch|description|requirements?", re.I)
TESTPLAN_WORDS = re.compile(r"testplan|test_plan|vplan|verification_plan|verif_plan|dv_plan|coverage_plan", re.I)
REGISTER_EXT = {".hjson", ".rdl", ".ipxact", ".xml", ".yaml", ".yml", ".json", ".csv"}
REGISTER_WORDS = re.compile(r"(^|[_\-.])(regs?|csrs?|registers?)([_\-.]|$)|ipxact|ip-xact|memory_map|memmap|regmap|addrmap", re.I)
NON_HW_TOP = {"sw", "software", "firmware", "util", "utils", "tools", "scripts", "examples", "example"}
DOC_LIKE_EXT = TEXT_EXT | BINARY_DOC_EXT | {".hjson", ".yaml", ".yml", ".json", ".csv", ".xml"}
SKIP_DIRS = {".git", "node_modules", "third_party", "3rdparty", "vendor", "build", "sim_build", ".github"}
PROCESS_DIRS = {"contributing", "getting_started", "project_governance", "governance",
                "use_cases", "rust_for_c_devs", "blog", "news", "community", "legal",
                "onboarding", "tutorials"}
SPEC_SUFFIX = re.compile(r"[_\-.](spec|specification|doc|docs|datasheet|ds|manual|guide|readme|"
                         r"theory_of_operation|testplan|regs?|registers?)$", re.I)


# --------------------------------------------------------------------------- GitHub access
class GitHub:
    def __init__(self, token):
        self.s = requests.Session()
        self.s.headers.update({"Accept": "application/vnd.github+json",
                               "X-GitHub-Api-Version": "2022-11-28",
                               "User-Agent": "spec-hunter"})
        if token:
            self.s.headers["Authorization"] = f"Bearer {token}"
        self.last_search = 0.0

    def get(self, url, params=None, search=False):
        if search:  # search API: 30 requests/minute with a token, 10 without
            wait = 2.2 - (time.time() - self.last_search)
            if wait > 0:
                time.sleep(wait)
            self.last_search = time.time()
        r = None
        for attempt in range(6):
            try:
                r = self.s.get(url, params=params, timeout=(15, 60))
            except (requests.ConnectionError, requests.Timeout) as e:
                if attempt == 5:
                    raise
                sleep = min(2 ** attempt * 5, 120)
                print(f"  network error ({type(e).__name__}), retrying in {sleep} s", file=sys.stderr)
                time.sleep(sleep)
                continue
            if r.status_code in (403, 429) and ("rate limit" in r.text.lower() or r.status_code == 429):
                reset = int(r.headers.get("X-RateLimit-Reset", time.time() + 60))
                retry = int(r.headers.get("Retry-After", 0))
                sleep = max(retry, reset - time.time(), 5) + 1
                print(f"  rate limited, sleeping {int(sleep)} s", file=sys.stderr)
                time.sleep(sleep)
                continue
            if r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            return r
        return r


# --------------------------------------------------------------------------- step 1: search
def star_buckets(min_stars, top=200000):
    """GitHub search returns at most 1000 results per query, so split by star ranges."""
    edges = sorted({e for e in (min_stars, 10, 20, 50, 100, 200, 500, 1000, 5000) if e >= min_stars} | {top + 1})
    return [(lo, hi - 1) for lo, hi in zip(edges, edges[1:])]


def cmd_search(gh, args):
    OUT.mkdir(exist_ok=True)
    path = OUT / "repo_list.jsonl"
    seen = set()
    if path.exists():
        seen = {json.loads(l)["full_name"] for l in path.open()}
    added = 0
    with path.open("a") as f:
        for lang in args.lang:
            lang_added = 0                       # --max-repos is a limit per language
            for lo, hi in reversed(star_buckets(args.min_stars)):
                q = f'language:"{lang}" stars:{lo}..{hi} fork:false archived:false'
                if args.query:
                    q = f"{args.query} {q}"
                for page in range(1, 11):  # 10 pages x 100 = the 1000-result limit
                    r = gh.get(f"{API}/search/repositories", search=True,
                               params={"q": q, "sort": "stars", "order": "desc", "per_page": 100, "page": page})
                    if r.status_code != 200:
                        print(f"  search failed ({r.status_code}): {r.text[:200]}", file=sys.stderr)
                        break
                    items = r.json().get("items", [])
                    if page == 1 and r.json().get("total_count", 0) > 1000:
                        print(f"  note: {r.json()['total_count']} repos match '{q}'; GitHub returns at most 1000. "
                              f"Narrow it with --query (e.g. 'created:2020..2022' or 'topic:risc-v').", file=sys.stderr)
                    for it in items:
                        if lang_added >= args.max_repos:
                            break
                        if it["full_name"] in seen:
                            continue
                        seen.add(it["full_name"])
                        rec = {k: it.get(k) for k in ("full_name", "default_branch", "stargazers_count",
                                                      "language", "description", "pushed_at", "html_url")}
                        rec["license"] = (it.get("license") or {}).get("spdx_id")
                        rec["query_lang"] = lang
                        f.write(json.dumps(rec) + "\n")
                        added += 1
                        lang_added += 1
                    print(f"{lang:14} stars {lo}..{hi} page {page}: {len(items)} repos (total new {added})")
                    if len(items) < 100 or lang_added >= args.max_repos:
                        break
                if lang_added >= args.max_repos:
                    break
            print(f"{lang}: {lang_added} new repos")
    print(f"repo list: {path} (+{added} new, {len(seen)} total)")


# --------------------------------------------------------------------------- step 2: scan
def classify(path, size):
    """Return the spec kind of a file path, or None if it is not a spec document."""
    p = Path(path)
    parts = [x.lower() for x in p.parts]
    if any(x in SKIP_DIRS for x in parts[:-1]):
        return None
    if any(x in PROCESS_DIRS for x in parts[:-1]):
        return None                          # project-process docs, not hardware specs
    if re.search(r"requirements.*\.txt$|^apt-|^pip-|^python-requirements", p.name.lower()):
        return None                          # package lists (e.g. apt-requirements.txt)
    name, ext = p.stem.lower(), p.suffix.lower()
    in_doc_dir = any(x in DOC_DIRS for x in parts[:-1])

    if parts[0] in NON_HW_TOP and ext not in {".hjson", ".rdl"}:
        return None                          # software, tool and script folders
    if TESTPLAN_WORDS.search(name) and ext in DOC_LIKE_EXT:
        return "testplan"
    if ext in {".rdl"} or "ipxact" in name or "ip-xact" in name:
        return "register_map"
    if (ext in REGISTER_EXT and REGISTER_WORDS.search(name)) or (ext == ".hjson" and "data" in parts):
        return "register_map"
    if ext in BINARY_DOC_EXT and (in_doc_dir or SPEC_WORDS.search(name)):
        return "document_binary"
    if ext in TEXT_EXT:
        if name in {"license", "copying", "changelog", "changes", "contributing", "authors",
                    "code_of_conduct", "security", "notice", "release_notes"}:
            return None
        if name == "readme" and len(parts) > 1 and size > 1500:
            return "readme_module"          # README inside a sub-directory: often a block description
        if name == "readme":
            return "readme_top"
        if in_doc_dir or SPEC_WORDS.search(name):
            return "prose_spec"
    return None


def module_key(name):
    """Normalise a file or directory name for matching specs to RTL modules."""
    name = name.lower()
    for _ in range(2):
        name = SPEC_SUFFIX.sub("", name)
    return re.sub(r"[^a-z0-9]", "", name)


GENERIC = {"top", "core", "pkg", "package", "defs", "types", "wrapper", "main", "index", "overview",
           "content", "contents", "soc", "cpu", "intro", "introduction", "readme", "doc", "docs", "common"}


def name_candidates(stem):
    """'soc_gpio_spec' -> ['socgpio', 'gpio']: the full name, then without leading words."""
    tokens = [t for t in re.split(r"[_\-.]", SPEC_SUFFIX.sub("", stem.lower())) if t]
    out = []
    for i in range(len(tokens)):
        k = "".join(tokens[i:])
        if len(k) >= 3 and k not in GENERIC:
            out.append(k)
    return out


def link_to_module(spec_path, modules_by_key, module_dirs):
    """Return (module, how) if a spec file belongs to a specific RTL module."""
    p = Path(spec_path)
    for k in name_candidates(p.stem):
        if k in modules_by_key:
            return modules_by_key[k], "same name"
    # OpenTitan style: hw/ip/<block>/doc/theory_of_operation.md  <->  hw/ip/<block>/rtl/<block>.sv
    for anc in list(p.parents)[:4]:
        ak = module_key(anc.name)
        if ak in GENERIC:
            continue
        if ak and ak in modules_by_key and str(anc) in module_dirs.get(ak, set()):
            return modules_by_key[ak], "block directory"
    return None, None


def scan_repo(gh, repo, branch):
    r = gh.get(f"{API}/repos/{repo}/git/trees/{quote(branch, safe='')}", params={"recursive": "1"})
    if r.status_code != 200:
        return None, f"tree {r.status_code}"
    data = r.json()
    files = [t for t in data.get("tree", []) if t["type"] == "blob"]

    hdl = [f for f in files if Path(f["path"]).suffix.lower() in HDL_EXT]
    langs = Counter(HDL_EXT[Path(f["path"]).suffix.lower()] for f in hdl)
    modules_by_key, module_dirs = {}, defaultdict(set)
    # project prefix shared by many module files, e.g. "neorv32_", "ibex_", "cv32e40p_"
    firsts = Counter(Path(f["path"]).stem.lower().split("_")[0] for f in hdl if "_" in Path(f["path"]).stem)
    prefix = next((w for w, n in firsts.most_common(1) if n >= 5 and n >= 0.3 * len(hdl)), None)
    for f in hdl:
        p = Path(f["path"])
        k = module_key(p.stem)
        keys = [k]
        if prefix and p.stem.lower().startswith(prefix + "_"):
            keys.append(module_key(p.stem[len(prefix) + 1:]))
        for kk in keys:
            if len(kk) >= 3 and kk not in GENERIC:
                modules_by_key.setdefault(kk, p.stem)
                for anc in list(p.parents)[1:5]:   # the block directory above rtl/ or src/
                    module_dirs[kk].add(str(anc))

    specs = []
    for f in files:
        kind = classify(f["path"], f.get("size", 0))
        if not kind:
            continue
        mod, how = link_to_module(f["path"], modules_by_key, module_dirs)
        specs.append({"repo": repo, "branch": branch, "path": f["path"], "kind": kind,
                      "ext": Path(f["path"]).suffix.lower(), "size": f.get("size", 0),
                      "module": mod or "", "linked_by": how or "",
                      "url": f"https://github.com/{repo}/blob/{branch}/{f['path']}"})
    summary = {"repo": repo, "branch": branch, "files": len(files), "hdl_files": len(hdl),
               "languages": "/".join(f"{l}:{n}" for l, n in langs.most_common()),
               "rtl_modules": len({Path(f["path"]).stem for f in hdl}), "spec_files": len(specs),
               "per_module_specs": sum(1 for s in specs if s["module"]),
               "modules_with_spec": len({s["module"] for s in specs if s["module"]}),
               "kinds": "/".join(f"{k}:{n}" for k, n in Counter(s["kind"] for s in specs).most_common()),
               "tree_truncated": data.get("truncated", False)}
    # score: favour repos where many modules have their own spec, and varied spec kinds
    summary["score"] = round(summary["modules_with_spec"] * 3 + len(set(s["kind"] for s in specs)) * 5
                             + min(summary["spec_files"], 50) * 0.5, 1)
    return (summary, specs), None


def cmd_scan(gh, args):
    OUT.mkdir(exist_ok=True)
    targets = []
    if args.seeds:
        for line in Path(args.seeds).read_text().splitlines():
            line = line.split("#")[0].strip()
            if line:
                targets.append({"full_name": line, "default_branch": None})
    if not args.seeds_only and (OUT / "repo_list.jsonl").exists():
        targets += [json.loads(l) for l in (OUT / "repo_list.jsonl").open()]

    cache = OUT / "cache"
    cache.mkdir(exist_ok=True)
    all_repos, all_specs, done = [], [], set()
    for i, t in enumerate(targets[: args.max_repos], 1):
        repo = t["full_name"]
        if repo in done:
            continue
        done.add(repo)
        cfile = cache / (repo.replace("/", "__") + ".json")
        if cfile.exists() and not args.refresh:
            summary, specs = json.loads(cfile.read_text())
        else:
            branch = t.get("default_branch")
            if not branch:
                r = gh.get(f"{API}/repos/{repo}")
                if r.status_code != 200:
                    print(f"[{i}] {repo}: not found ({r.status_code})")
                    continue
                branch = r.json()["default_branch"]
            res, err = scan_repo(gh, repo, branch)
            if err:
                print(f"[{i}] {repo}: skipped ({err})")
                continue
            summary, specs = res
            cfile.write_text(json.dumps([summary, specs]))
        summary.update({k: t.get(k) for k in ("stargazers_count", "license", "description") if t.get(k) is not None})
        if isinstance(summary.get("license"), dict):
            summary["license"] = summary["license"].get("spdx_id")
        all_repos.append(summary)
        all_specs += specs
        print(f"[{i}] {repo}: {summary['hdl_files']} HDL files, {summary['spec_files']} spec files, "
              f"{summary['modules_with_spec']} modules with own spec")

    all_repos.sort(key=lambda r: -r["score"])
    write_csv(OUT / "repos.csv", all_repos)
    write_csv(OUT / "spec_files.csv", all_specs)
    print(f"\nwrote {OUT/'repos.csv'} ({len(all_repos)} repos) and {OUT/'spec_files.csv'} ({len(all_specs)} files)")
    print("\nTop repos by per-module specs:")
    for r in all_repos[:15]:
        print(f"  {r['score']:7}  {r['repo']:45} modules with spec: {r['modules_with_spec']:4}  kinds: {r['kinds']}")


def write_csv(path, rows):
    if not rows:
        path.write_text("")
        return
    keys = list(dict.fromkeys(k for r in rows for k in r))
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


# --------------------------------------------------------------------------- step 3: download
def cmd_download(gh, args):
    rows = list(csv.DictReader((OUT / "spec_files.csv").open()))
    kinds = set(args.kinds.split(","))
    top = None
    if args.top_repos:
        repos = list(csv.DictReader((OUT / "repos.csv").open()))
        top = {r["repo"] for r in repos[: args.top_repos]}
    n = skipped = 0
    for row in rows:
        if row["kind"] not in kinds or (top and row["repo"] not in top):
            continue
        if args.linked_only and not row["module"]:
            continue
        if row["ext"] in BINARY_DOC_EXT and not args.binary:
            continue
        if int(row["size"] or 0) > args.max_kb * 1024:
            skipped += 1
            continue
        dest = OUT / "files" / row["repo"].replace("/", "__") / row["path"]
        if dest.exists():
            continue
        url = f"{RAW}/{row['repo']}/{quote(row['branch'], safe='')}/{quote(row['path'])}"
        try:
            r = gh.get(url)
        except (requests.ConnectionError, requests.Timeout) as e:
            print(f"  failed ({type(e).__name__}): {row['repo']}/{row['path']}")
            continue
        if r.status_code != 200:
            print(f"  failed {r.status_code}: {row['repo']}/{row['path']}")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.content)
        n += 1
        if n % 50 == 0:
            print(f"  {n} files downloaded")
        time.sleep(args.delay)
    print(f"downloaded {n} files into {OUT/'files'} (skipped {skipped} over {args.max_kb} KB)")


# --------------------------------------------------------------------------- step 4: analyze
HEADING_PATTERNS = [
    (re.compile(r"^(#{1,4})\s+(.+?)\s*#*$"), lambda m: (len(m[1]), m[2])),                # Markdown
    (re.compile(r"^(={1,4})\s+(.+)$"), lambda m: (len(m[1]), m[2])),                      # AsciiDoc
    (re.compile(r"^\\(sub)*section\*?\{(.+?)\}"), lambda m: (1 + len(m[0]) // 100, m[2])),  # LaTeX
]


def norm_heading(h):
    h = re.sub(r"<[^>]+>|\{\{.*?\}\}|\[|\]\(.*?\)|[`*_]", "", h)
    h = re.sub(r"^\s*[\d.]+\s*", "", h)               # drop numbering like "3.2"
    return re.sub(r"\s+", " ", h).strip().lower()


def cmd_analyze(gh, args):
    files_dir = OUT / "files"
    meta = {(r["repo"].replace("/", "__"), r["path"]): r for r in csv.DictReader((OUT / "spec_files.csv").open())}
    heading_docs = defaultdict(set)
    level_of = defaultdict(Counter)
    per_doc = []
    for p in files_dir.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in TEXT_EXT:
            continue
        repo_dir = p.relative_to(files_dir).parts[0]
        rel = str(Path(*p.relative_to(files_dir).parts[1:]))
        kind = meta.get((repo_dir, rel), {}).get("kind", "")
        heads = []
        lines = p.read_text(errors="ignore").splitlines()
        for i, line in enumerate(lines):
            got = None
            for rx, fn in HEADING_PATTERNS:
                if (m := rx.match(line)):
                    got = fn(m)
                    break
            # reStructuredText: a title line followed by ===== or -----
            if not got and p.suffix == ".rst" and i + 1 < len(lines) and line.strip() and \
                    re.fullmatch(r"([=\-~^])\1{2,}", lines[i + 1].strip()):
                got = ({"=": 1, "-": 2, "~": 3, "^": 4}[lines[i + 1].strip()[0]], line)
            if got:
                lvl, text = got
                t = norm_heading(text)
                if t and len(t) < 80:
                    heads.append(t)
                    heading_docs[t].add((repo_dir, rel))
                    level_of[t][lvl] += 1
        per_doc.append({"repo": repo_dir.replace("__", "/"), "path": rel, "kind": kind,
                        "headings": " | ".join(heads[:40])})
    total = len(per_doc)
    rows = sorted(({"heading": h, "documents": len(d), "share_of_docs": round(len(d) / max(total, 1), 3),
                    "repos": len({x[0] for x in d}), "usual_level": level_of[h].most_common(1)[0][0]}
                   for h, d in heading_docs.items() if len({x[0] for x in d}) >= args.min_repos),
                  key=lambda r: (-r["repos"], -r["documents"]))
    write_csv(OUT / "headings.csv", rows)
    write_csv(OUT / "doc_outlines.csv", per_doc)
    print(f"analyzed {total} text specs -> {OUT/'headings.csv'}, {OUT/'doc_outlines.csv'}")
    print("\nMost common section headings (across repos):")
    for r in rows[:30]:
        print(f"  {r['repos']:4} repos  {r['documents']:5} docs   {r['heading']}")


# --------------------------------------------------------------------------- main
def load_dotenv(path=".env"):
    """Read KEY=VALUE lines from a .env file next to where you run the script."""
    for f in (Path(path), Path(__file__).with_name(".env")):
        if f.exists():
            for line in f.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip().removeprefix("export ").strip(), v.strip().strip('"').strip("'"))
            break


def main():
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--token", default=os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"),
                    help="GitHub token (or set GITHUB_TOKEN in the environment or in a .env file)")
    ap.add_argument("--out", default="out", help="output folder (default: out)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="find repos by language")
    s.add_argument("--lang", nargs="+", default=["SystemVerilog", "VHDL", "Verilog"])
    s.add_argument("--min-stars", type=int, default=5)
    s.add_argument("--max-repos", type=int, default=3000, help="limit per language (default 3000)")
    s.add_argument("--query", default="", help='extra search words, e.g. "risc-v" or "topic:fpga"')

    c = sub.add_parser("scan", help="classify spec files in each repo")
    c.add_argument("--seeds", help="text file with owner/repo per line to scan first")
    c.add_argument("--seeds-only", action="store_true")
    c.add_argument("--max-repos", type=int, default=100000)
    c.add_argument("--refresh", action="store_true", help="ignore the cache")

    d = sub.add_parser("download", help="fetch spec files")
    d.add_argument("--kinds", default="prose_spec,readme_module,testplan,register_map,document_binary")
    d.add_argument("--binary", action="store_true", help="also fetch PDF/DOCX/XLSX")
    d.add_argument("--max-kb", type=int, default=2048)
    d.add_argument("--top-repos", type=int, default=0, help="only the N best repos from repos.csv")
    d.add_argument("--delay", type=float, default=0.05)
    d.add_argument("--linked-only", action="store_true",
                   help="only specs linked to an RTL module (the per-module specs)")

    a = sub.add_parser("analyze", help="count section headings across downloaded specs")
    a.add_argument("--min-repos", type=int, default=2, help="keep headings used in at least N repos")

    al = sub.add_parser("all", help="search + scan + download + analyze")
    for p in (s, c, d, a):
        for act in p._actions[1:]:
            if not any(act.dest == x.dest for x in al._actions):
                al._add_action(act)

    args = ap.parse_args()
    global OUT
    OUT = Path(args.out)
    if not args.token:
        print("warning: no GITHUB_TOKEN set - limited to 60 API requests per hour", file=sys.stderr)
    gh = GitHub(args.token)
    if args.cmd == "all":
        for f in (cmd_search, cmd_scan, cmd_download, cmd_analyze):
            f(gh, args)
    else:
        {"search": cmd_search, "scan": cmd_scan, "download": cmd_download, "analyze": cmd_analyze}[args.cmd](gh, args)


if __name__ == "__main__":
    main()
