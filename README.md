# spec_hunter

Finds SystemVerilog / Verilog / VHDL repositories on GitHub and collects every kind of
specification they contain (prose specs, per-module docs, register maps, testplans, PDFs),
so you can compare spec styles and choose a template for the spec agent.

It uses the official GitHub REST API, not HTML scraping, so it respects GitHub's rate limits
and terms of use.

## 1. Setup

```bash
python3 -m pip install -r requirements.txt
```

Create a GitHub token (needed: without it you get only 60 requests per hour):
1. GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → Generate.
2. Repository access: **Public repositories (read-only)**. No other permissions are needed.
3. Copy the token and set it:
   ```bash
   export GITHUB_TOKEN=github_pat_xxx          # Linux / macOS / WSL
   setx GITHUB_TOKEN github_pat_xxx            # Windows (open a new terminal afterwards)
   ```
   Or put it in a file named `.env` in this folder (the script reads it automatically):
   ```
   GITHUB_TOKEN=github_pat_xxx
   ```

## 2. Quick start: the curated repos first (about 2 minutes)

```bash
python3 spec_hunter.py scan --seeds seeds.txt --seeds-only
python3 spec_hunter.py download
python3 spec_hunter.py analyze
```

Then open `out/repos.csv`, `out/spec_files.csv` and `out/headings.csv`.

## 3. The full run: every hardware repo

```bash
# 1) find repos (search API: ~30 requests/minute, so this takes a while)
#    --max-repos is a limit PER LANGUAGE
python3 spec_hunter.py search --lang SystemVerilog VHDL Verilog --min-stars 5 --max-repos 5000

# 2) scan each repo's file list (1 API request per repo; 5000 requests/hour with a token)
python3 spec_hunter.py scan --seeds seeds.txt

# 3) download the spec files of the 300 best repos (text only)
python3 spec_hunter.py download --top-repos 300

#    ...or also the PDFs / Word / Excel specs
python3 spec_hunter.py download --top-repos 300 --binary

# ...or only the specs linked to an RTL module (the per-module specs)
python spec_hunter.py download --linked-only

# 4) count which section headings appear across specs
python3 spec_hunter.py analyze --min-repos 3
```

Or all four at once: `python3 spec_hunter.py all --seeds seeds.txt --top-repos 300`.

Every step can be stopped and re-run: `scan` caches each repo in `out/cache/`, and
`download` skips files it already has. Use `--out other_folder` to keep separate runs.

### Narrowing the search

GitHub returns at most 1000 results per query. The script splits by star ranges; if it
prints a "note: N repos match" message, narrow further with `--query`:

```bash
python3 spec_hunter.py search --lang SystemVerilog --query "topic:risc-v"
python3 spec_hunter.py search --lang Verilog --query "org:freecores"        # OpenCores mirrors, many PDF specs
python3 spec_hunter.py search --lang SystemVerilog --query "created:2023-01-01..2024-12-31"
```

## 4. What you get

| File | One row per | Key columns |
|---|---|---|
| `out/repo_list.jsonl` | repo found by `search` | name, stars, language, license |
| `out/repos.csv` | scanned repo, best first | `hdl_files`, `rtl_modules`, `spec_files`, **`modules_with_spec`**, `kinds`, `score` |
| `out/spec_files.csv` | spec file | `kind`, `path`, **`module`** (the RTL module it documents), `linked_by`, `url` |
| `out/files/<owner>__<repo>/...` | downloaded spec | the original file |
| `out/headings.csv` | section heading | in how many repos and documents it appears |
| `out/doc_outlines.csv` | downloaded text spec | its list of headings, i.e. its outline |

### Spec kinds

| kind | What it is | Examples |
|---|---|---|
| `prose_spec` | text docs in doc folders or named spec/design/datasheet/... | OpenTitan `theory_of_operation.md`, NEORV32 `soc_gpio.adoc`, PULP `doc/axi_xbar.md` |
| `readme_module` | a README inside a sub-folder (often describes that block) | `hw/ip/aes/README.md` |
| `readme_top` | the repository's main README | |
| `register_map` | machine-readable register / interface description | `.hjson` (OpenTitan), `.rdl` (SystemRDL), IP-XACT `.xml` |
| `testplan` | verification plans | `aes_testplan.hjson`, `vplan.xlsx` |
| `document_binary` | PDF / Word / Excel specs | OpenCores `*_spec.pdf`, `Caliptra_TestPlan.xlsx` |

### How a spec is linked to a module

`module` is filled when either:
- **same name**: the file is named after an RTL file, ignoring words like `spec`, `doc` and
  project prefixes: `doc/axi_xbar.md` → `axi_xbar.sv`; `soc_gpio.adoc` → `neorv32_gpio.vhd`;
- **block directory**: the spec sits in a block's folder: `hw/ip/aes/doc/theory_of_operation.md` → `hw/ip/aes/rtl/aes.sv`.

`modules_with_spec` in `repos.csv` counts the distinct modules that have at least one
linked spec. Sort by it to find repos with **a spec per module**.

## 5. Using the results to design the spec template

1. In `repos.csv`, keep repos with a high `modules_with_spec`.
2. In `spec_files.csv`, filter `module != ""` to read real per-module specs side by side.
3. `headings.csv` shows which sections appear across many projects (e.g. *overview*,
   *features*, *parameters*, *ports* / *interfaces*, *theory of operation*, *registers*,
   *clocking and reset*, *testplan*). Sections used by many repos are candidates for
   required fields; rare ones for optional fields.
4. `doc_outlines.csv` shows each document's full outline, useful for comparing
   OpenTitan-style vs datasheet-style vs README-style specs.

## Notes

- Matching is heuristic: expect some false links (e.g. a generic `mem_testplan` linked to a
  module named `mem`) and some misses. Check before relying on a single row.
- Respect each repository's license when you reuse its documents. Keep the `url` column
  as the source reference.
- Large monorepos (OpenTitan) can have `tree_truncated = True` if GitHub cuts the file list;
  clone them instead if you need every file.
