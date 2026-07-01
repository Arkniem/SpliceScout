#!/usr/bin/env python3
"""graphify.py -- build a static, MULTI-LANGUAGE, FUNCTION-LEVEL knowledge graph of SpliceScout.

Pure-stdlib analysis (no third-party deps). Covers every code file in the project AND what happens
inside each one:

  FILE LEVEL (the dependency graph)
    * nodes      = every .py / .sh file, tagged with component + layer + kind + loc
    * edges      = import | call | source | launch | xlang (cross-language), with weights

  INSIDE EACH FILE (the functionality)
    * module_doc = the file's own one/two-line summary (module docstring / bash header comment)
    * symbols    = every function / method / class, each with:
                     - signature (arg names)
                     - doc      (its docstring first line, or the `#` comment above it)
                     - line     (where it's defined)
                     - calls    (what it calls -- intra-file functions + cross-module `mod.func`)

Languages: Python 3 (ast, precise), Python 2 vendored AltAnalyze (regex fallback), bash (regex).

Outputs (next to this script):
  knowledge_graph.json  -- full graph incl. per-file module_doc + symbols
  internals.json        -- compact {file: {summary, symbols}} for the drill-down viewer
  knowledge_graph.dot   -- Graphviz file-dependency graph (clustered by component)
and prints a summary to stdout.

Run:  python knowledge_graph/graphify.py        (from the SpliceScout root)
"""
import ast
import json
import os
import re
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXCLUDE_DIRS = {"runs", "vendor", "__pycache__", ".git", "knowledge_graph", ".lsf"}
CODE_EXT = (".py", ".sh")

LAYER_RULES = [
    ("ui",            lambda n: n in ("server", "tray")),
    ("orchestration", lambda n: n in ("pipeline", "progress", "pipeline_paths")),
    ("acquire",       lambda n: n in ("fetch_5000_ncbi", "structured_extract")),
    ("ai_clean",      lambda n: n in ("prep_ai", "ai_clean", "merge_ai", "llm_providers")),
    ("build",         lambda n: n in ("build_final", "normalize_v2")),
    ("deep_dive",     lambda n: n in ("deepdive_select", "cellline_match", "group_assign", "cell_utils")),
    ("runtable",      lambda n: n.startswith("runtable")),
    ("cluster",       lambda n: n.endswith("_deploy")),
    ("support",       lambda n: True),
]
STAGE_OF_TEMPLATE = {"cluster_template": "cluster", "star_template": "star",
                     "bed_template": "bed", "psi_template": "psi",
                     "concordance_template": "concordance"}


def app_layer(stem):
    for label, test in LAYER_RULES:
        if test(stem):
            return label
    return "support"


def classify(relpath):
    parts = relpath.replace("\\", "/").split("/")
    top = parts[0]
    if "altanalyze" in parts:
        return "altanalyze", "bed", "py2"
    if top in STAGE_OF_TEMPLATE:
        return top, STAGE_OF_TEMPLATE[top], None
    if len(parts) == 1 and relpath.endswith(".py"):
        return "app", app_layer(parts[0][:-3]), "py3"
    if relpath.endswith(".py"):
        return (top if top in STAGE_OF_TEMPLATE else "app"), STAGE_OF_TEMPLATE.get(top, "support"), "py3"
    return "app", "support", None


def discover():
    files = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in filenames:
            if not fn.endswith(CODE_EXT):
                continue
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, ROOT).replace("\\", "/")
            comp, layer, kind = classify(rel)
            files.append({"id": rel, "name": fn, "path": path, "ext": fn.rsplit(".", 1)[-1],
                          "component": comp, "layer": layer, "kind_hint": kind})
    return files


def _read(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def first_line(text, cap=150):
    if not text:
        return ""
    t = text.strip().replace("\n", " ").replace("\r", " ")
    t = re.split(r"(?<=[.!?])\s", t, maxsplit=1)[0]
    t = re.sub(r"\s+", " ", t).strip(" -#=*")
    return (t[:cap] + "…") if len(t) > cap else t


def leading_comment(lines, lineno):
    """The contiguous `#` comment block directly above a 1-based def line (skips decorators)."""
    i = lineno - 2
    out = []
    while i >= 0:
        s = lines[i].strip()
        if s.startswith("#"):
            out.append(s.lstrip("#").strip())
            i -= 1
            continue
        if s.startswith("@") and not out:   # skip decorators on the way up to the comment
            i -= 1
            continue
        break
    out.reverse()
    return first_line(" ".join(out))


def _name_of(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _name_of(node.value)
        return (base + "." + node.attr) if base else node.attr
    return None


def _sig(node):
    a = node.args
    parts = [ar.arg for ar in getattr(a, "posonlyargs", [])] + [ar.arg for ar in a.args]
    if a.vararg:
        parts.append("*" + a.vararg.arg)
    parts += [ar.arg for ar in a.kwonlyargs]
    if a.kwarg:
        parts.append("**" + a.kwarg.arg)
    return "(" + ", ".join(parts) + ")"


def _calls_in(node, own_names, alias):
    """What a function calls: intra-file functions (own_names) + aliased cross-module `mod.func`."""
    out = []
    for c in ast.walk(node):
        if not isinstance(c, ast.Call):
            continue
        nm = _name_of(c.func)
        if not nm:
            continue
        if "." in nm:
            head, attr = nm.split(".", 1)
            if head in alias:
                out.append(alias[head] + "." + attr)
            elif head == "self":
                out.append("self." + attr)
        elif nm in own_names:
            out.append(nm)
        elif nm in alias:
            out.append(alias[nm] + "." + nm)
    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq[:12]


def analyze_py3(rec, py_stems):
    src = _read(rec["path"])
    try:
        tree = ast.parse(src, filename=rec["path"])
    except SyntaxError:
        return None
    lines = src.split("\n")
    rec["kind"] = "py3"
    rec["loc"] = len(lines)
    rec["module_doc"] = first_line(ast.get_docstring(tree) or "")
    imp_int, alias = set(), {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                top = a.name.split(".")[0]
                if top in py_stems:
                    imp_int.add(top); alias[a.asname or top] = top
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")[0]
            if mod in py_stems:
                imp_int.add(mod)
                for a in node.names:
                    alias[a.asname or a.name] = mod
    # collect own top-level + method names first (for intra-file call resolution)
    own = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            own.add(node.name)
        elif isinstance(node, ast.ClassDef):
            for b in node.body:
                if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    own.add(b.name)

    def fdoc(node):
        d = ast.get_docstring(node)
        return first_line(d) if d else leading_comment(lines, node.lineno)

    symbols = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append({"name": node.name, "kind": "function", "args": _sig(node),
                            "line": node.lineno, "doc": fdoc(node),
                            "calls": _calls_in(node, own, alias)})
        elif isinstance(node, ast.ClassDef):
            symbols.append({"name": node.name, "kind": "class", "args": "",
                            "line": node.lineno, "doc": first_line(ast.get_docstring(node) or
                                                                   leading_comment(lines, node.lineno)),
                            "calls": []})
            for b in node.body:
                if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append({"name": node.name + "." + b.name, "kind": "method",
                                    "args": _sig(b), "line": b.lineno, "doc": fdoc(b),
                                    "calls": _calls_in(b, own, alias)})
    rec["imports_internal"] = sorted(imp_int)
    rec["symbols"] = symbols
    rec["_alias"] = alias
    rec["_src"] = src
    return rec


RE_PY_DEF = re.compile(r"^(\s*)def\s+(\w+)\s*\(([^)]*)", re.M)
RE_PY_CLASS = re.compile(r"^\s*class\s+(\w+)", re.M)
RE_PY_IMP = re.compile(r"^\s*(?:from\s+(\w+)|import\s+(\w+))", re.M)


def _header_comment(lines):
    """Top-of-file comment block (after an optional shebang) -> file summary."""
    out, started = [], False
    for ln in lines[:25]:
        s = ln.strip()
        if s.startswith("#!"):
            continue
        if s.startswith("#"):
            out.append(s.lstrip("#").strip()); started = True
        elif started or s == "":
            if started:
                break
        else:
            break
    return first_line(" ".join(out))


def analyze_py_regex(rec, py_stems):
    src = _read(rec["path"])
    lines = src.split("\n")
    rec["kind"] = rec.get("kind_hint") or "py2"
    rec["loc"] = len(lines)
    rec["module_doc"] = _header_comment(lines)
    imp = set()
    for a, b in RE_PY_IMP.findall(src):
        m = a or b
        if m in py_stems:
            imp.add(m)
    rec["imports_internal"] = sorted(imp)
    syms = []
    for m in RE_PY_DEF.finditer(src):
        line = src.count("\n", 0, m.start()) + 1
        syms.append({"name": m.group(2), "kind": "function",
                     "args": "(" + re.sub(r"\s+", " ", m.group(3)).strip() + ")",
                     "line": line, "doc": leading_comment(lines, line), "calls": []})
    for m in RE_PY_CLASS.finditer(src):
        line = src.count("\n", 0, m.start()) + 1
        syms.append({"name": m.group(1), "kind": "class", "args": "", "line": line,
                     "doc": leading_comment(lines, line), "calls": []})
    rec["symbols"] = sorted(syms, key=lambda s: s["line"])
    rec["_src"] = src
    return rec


RE_SH_FUNC = re.compile(r"^\s*(?:function\s+)?([A-Za-z_][\w]*)\s*\(\s*\)\s*\{", re.M)
RE_SH_SOURCE = re.compile(r"(?:^|\s)(?:source|\.)\s+[\"']?[^\"'\s]*?([\w.\-]+\.sh)", re.M)
RE_SH_DOTSH = re.compile(r"([\w.\-]+\.sh)")
RE_SH_DOTPY = re.compile(r"([\w.\-]+\.py)")


def analyze_sh(rec):
    src = _read(rec["path"])
    lines = src.split("\n")
    rec["kind"] = "sh"
    rec["loc"] = len(lines)
    rec["module_doc"] = _header_comment(lines)
    syms = []
    for m in RE_SH_FUNC.finditer(src):
        line = src.count("\n", 0, m.start()) + 1
        syms.append({"name": m.group(1), "kind": "function", "args": "()",
                     "line": line, "doc": leading_comment(lines, line), "calls": []})
    rec["symbols"] = sorted(syms, key=lambda s: s["line"])
    rec["_sh_sourced"] = set(RE_SH_SOURCE.findall(src))
    refs = {}
    for name in RE_SH_DOTSH.findall(src):
        if name != rec["name"]:
            refs[name] = refs.get(name, 0) + 1
    rec["_sh_refs"] = refs
    rec["_py_refs"] = set(RE_SH_DOTPY.findall(src))
    return rec


def main():
    files = discover()
    by_id = {f["id"]: f for f in files}
    py_stems = {f["name"][:-3] for f in files if f["ext"] == "py"}
    sh_in_dir, sh_by_name, py_by_stem = {}, {}, {}
    for f in files:
        d = os.path.dirname(f["id"])
        if f["ext"] == "sh":
            sh_in_dir[(d, f["name"])] = f["id"]
            sh_by_name.setdefault(f["name"], []).append(f["id"])
        else:
            py_by_stem.setdefault(f["name"][:-3], []).append(f["id"])

    for f in files:
        if f["ext"] == "sh":
            analyze_sh(f)
        elif analyze_py3(f, py_stems) is None:
            analyze_py_regex(f, py_stems)

    def resolve_sh(ref, from_id):
        d = os.path.dirname(from_id)
        if (d, ref) in sh_in_dir:
            return sh_in_dir[(d, ref)]
        hits = sh_by_name.get(ref, [])
        if len(hits) == 1:
            return hits[0]
        same = [h for h in hits if os.path.dirname(h) == d]
        return same[0] if same else (hits[0] if hits else None)

    def resolve_py(ref, prefer=None):
        stem = ref[:-3] if ref.endswith(".py") else ref
        hits = py_by_stem.get(stem, [])
        if len(hits) == 1:
            return hits[0]
        if prefer:
            same = [h for h in hits if by_id[h]["component"] == prefer]
            if same:
                return same[0]
        return hits[0] if hits else None

    edges, seen = [], {}

    def add_edge(s, d, t, ex=None):
        if not d or s == d:
            return
        k = (s, d, t)
        if k not in seen:
            seen[k] = {"src": s, "dst": d, "type": t, "weight": 0, "examples": set()}
            edges.append(seen[k])
        seen[k]["weight"] += 1
        if ex and len(seen[k]["examples"]) < 5:
            seen[k]["examples"].add(ex)

    fn_home = {}
    for f in files:
        if f.get("kind") == "py3":
            for s in f["symbols"]:
                if s["kind"] == "function":
                    fn_home.setdefault(s["name"], set()).add(f["id"])
    for f in files:
        for stem in f.get("imports_internal", []):
            add_edge(f["id"], resolve_py(stem, f["component"]), "import")
        if f.get("kind") == "py3":
            alias = f.get("_alias", {})
            try:
                tree = ast.parse(f["_src"])
            except Exception:
                tree = None
            if tree:
                for call in ast.walk(tree):
                    if not isinstance(call, ast.Call):
                        continue
                    nm = _name_of(call.func)
                    if not nm:
                        continue
                    dst_stem = None
                    if "." in nm:
                        head, attr = nm.split(".", 1)
                        if head in alias:
                            dst_stem = alias[head]
                    elif nm in alias:
                        dst_stem = alias[nm]
                    elif nm in fn_home and f["id"] not in fn_home[nm] and len(fn_home[nm]) == 1:
                        add_edge(f["id"], next(iter(fn_home[nm])), "call", nm)
                        continue
                    if dst_stem:
                        add_edge(f["id"], resolve_py(dst_stem, f["component"]), "call")
        if f.get("kind") == "sh":
            for ref in f.get("_sh_refs", {}):
                add_edge(f["id"], resolve_sh(ref, f["id"]),
                         "source" if ref in f.get("_sh_sourced", set()) else "launch", ref)
            for pref in f.get("_py_refs", set()):
                add_edge(f["id"], resolve_py(pref), "xlang", pref)

    for f in files:
        if f["component"] == "app" and f["name"].endswith("_deploy.py"):
            tdir = f["name"][:-10] + "_template"
            src = f.get("_src", "")
            if any(by_id[x]["component"] == tdir for x in by_id):
                for shref in set(RE_SH_DOTSH.findall(src)):
                    tgt = resolve_sh(shref, tdir + "/x")
                    if tgt:
                        add_edge(f["id"], tgt, "xlang", shref)
                entry = sh_in_dir.get((tdir, "watchdog.sh"))
                if entry:
                    add_edge(f["id"], entry, "xlang", tdir + "/")

    for f in files:
        for k in ("_alias", "_src", "_sh_sourced", "_sh_refs", "_py_refs", "kind_hint", "path"):
            f.pop(k, None)
    for e in edges:
        e["examples"] = sorted(e["examples"])

    indeg = {f["id"]: 0 for f in files}
    outdeg = {f["id"]: 0 for f in files}
    imported_by = {f["id"]: [] for f in files}
    for e in edges:
        outdeg[e["src"]] += 1
        indeg[e["dst"]] += 1
        if e["type"] in ("import", "source"):
            imported_by[e["dst"]].append(e["src"])

    by_comp = {}
    for f in files:
        by_comp.setdefault(f["component"], []).append(f["id"])
    total_syms = sum(len(f["symbols"]) for f in files)

    graph = {
        "project": "SpliceScout",
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "file_count": len(files),
        "symbol_count": total_syms,
        "edge_count": len(edges),
        "edge_types": {t: sum(1 for e in edges if e["type"] == t)
                       for t in ("import", "call", "source", "launch", "xlang")},
        "components": {c: sorted(v) for c, v in sorted(by_comp.items())},
        "files": {f["id"]: f for f in files},
        "edges": edges,
        "degree": {f["id"]: {"out": outdeg[f["id"]], "in": indeg[f["id"]]} for f in files},
        "imported_by": imported_by,
    }
    with open(os.path.join(HERE, "knowledge_graph.json"), "w", encoding="utf-8") as fh:
        json.dump(graph, fh, indent=1)

    # compact internals for the drill-down viewer
    internals = {}
    for f in files:
        internals[f["id"]] = {
            "summary": f.get("module_doc", ""),
            "symbols": [{"n": s["name"], "a": s["args"], "k": s["kind"][0],
                         "d": s["doc"], "c": s["calls"], "l": s["line"]} for s in f["symbols"]],
        }
    with open(os.path.join(HERE, "internals.json"), "w", encoding="utf-8") as fh:
        json.dump(internals, fh, separators=(",", ":"))

    palette = {"app": "#7F77DD", "cluster_template": "#EF9F27", "star_template": "#378ADD",
               "bed_template": "#1D9E75", "psi_template": "#D4537E", "altanalyze": "#E24B4A"}
    dot = ["digraph SpliceScout {", "  rankdir=LR; node [shape=box style=filled fontname=Helvetica fontsize=10];"]
    for comp, ids in graph["components"].items():
        dot.append('  subgraph "cluster_%s" { label="%s"; color="#ccc";' % (comp, comp))
        for i in ids:
            dot.append('    "%s" [fillcolor="%s" label="%s"];' % (i, palette.get(comp, "#eee"), by_id[i]["name"]))
        dot.append("  }")
    for e in edges:
        st = {"call": "style=dotted", "source": 'color="#888"', "launch": 'color="#888" style=dashed',
              "xlang": 'color="#E24B4A" penwidth=2'}.get(e["type"], "")
        dot.append('  "%s" -> "%s" [%s];' % (e["src"], e["dst"], st))
    dot.append("}")
    with open(os.path.join(HERE, "knowledge_graph.dot"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(dot))

    docd = sum(1 for f in files for s in f["symbols"] if s["doc"])
    print("SpliceScout FUNCTION-LEVEL knowledge graph  (%s)" % graph["generated"])
    print("  files: %d   symbols: %d (%d documented)   edges: %d  %s"
          % (graph["file_count"], total_syms, docd, graph["edge_count"], graph["edge_types"]))
    print("\n  by component (files / symbols):")
    for c, ids in graph["components"].items():
        print("    %-16s %2d files / %3d symbols" % (c, len(ids), sum(len(by_id[i]["symbols"]) for i in ids)))
    print("\n  biggest files by symbol count:")
    for i in sorted(by_id, key=lambda x: len(by_id[x]["symbols"]), reverse=True)[:8]:
        print("    %-30s %3d symbols  (%d loc)" % (i, len(by_id[i]["symbols"]), by_id[i]["loc"]))
    print("\n  wrote knowledge_graph.json + internals.json + knowledge_graph.dot to %s" % HERE.replace("\\", "/"))


if __name__ == "__main__":
    sys.exit(main())
