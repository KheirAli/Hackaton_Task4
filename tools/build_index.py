#!/usr/bin/env python3
"""Turn per-test traces of the CLEAN pinned checkout into the runtime index.

Usage: build_index.py SQLGLOT_ROOT TESTS_JSON RAW_DIR[,RAW_DIR...] OUT_ZIP [CLASS_INFO_JSON]

Several RAW_DIRs (independent trace runs, e.g. different hash seeds) are unioned.

The zip holds
  meta.json        test ids (bit order), per-file opener sets, list of base files
  cov/<file>.json  for each traced Python file: importer set, lines run at
                   import time, per-line sets of tests that ran the line
                   inside a function, and per top-level class the tests that
                   used it (see class_users below)
  src/<file>       base text of every Python file under sqlglot/ and tests/
Test sets are hex bitmasks over the test-id list. Nothing here depends on any
patch or answer: it is a map of the pinned commit only.
"""
import ast
import json
import os
import subprocess
import sys
import zipfile

root, tests_json, raw, out_zip = sys.argv[1:5]
class_info = sys.argv[5] if len(sys.argv) > 5 else None
root = os.path.realpath(root)
tests = json.load(open(tests_json))["tests"]
ids = [t["id"] for t in tests]

importers, implines, fn, opens, used = {}, {}, {}, {}, {}
bad = []
for bit, test, raw_dir in [(b, t, r) for b, t in enumerate(tests) for r in raw.split(",")]:
    rec = json.load(open(os.path.join(raw_dir, f"{test['number']}.json")))
    assert rec["id"] == test["id"]
    if rec["outcome"] != "passed" or not rec["procs"]:
        bad.append((test["id"], rec["outcome"]))
    mask = 1 << bit
    for name, lines in rec["imp"].items():
        importers[name] = importers.get(name, 0) | mask
        implines.setdefault(name, set()).update(lines)
    for name, lines in rec["fn"].items():
        per = fn.setdefault(name, {})
        for line in lines:
            per[line] = per.get(line, 0) | mask
    for name in rec["opens"]:
        opens[name] = opens.get(name, 0) | mask
    for key in rec.get("classes", ()):
        used[key] = used.get(key, 0) | mask

if bad:
    print(f"WARNING: {len(bad)} tests did not pass cleanly while tracing: {bad[:10]}", file=sys.stderr)

base_files = subprocess.run(["git", "-C", root, "ls-files"], capture_output=True, text=True, check=True).stdout.split("\n")
base_files = [f for f in base_files if f]



def class_users():
    """Per file: [first_line, last_line, mask] for top-level classes we can narrow on.

    A test "uses" a top-level class when it ran a repository method on an
    instance of the class, of a class nested in it or of a subclass of either,
    or when it ran a function line that mentions the class by name. Only
    classes whose construction the tracer can observe are listed (class_info).
    """
    if not class_info:
        return {}
    observable = set(json.load(open(class_info)))
    py = [f for f in base_files if f.endswith(".py") and f.startswith(("sqlglot/", "tests/"))]
    trees = {name: ast.parse(open(os.path.join(root, name), encoding="utf-8").read()) for name in py}
    named = {}  # identifier -> tests that ran a function line mentioning it
    funcs = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)

    def scan(node, name, inside):
        if inside:
            ident = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None
            if ident is not None:
                lines = fn.get(name, {})
                mask = 0
                for line in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                    mask |= lines.get(line, 0)
                if mask:
                    named[ident] = named.get(ident, 0) | mask
        for field, value in ast.iter_fields(node):
            deeper = inside or (isinstance(node, funcs) and field == "body")
            for child in value if isinstance(value, list) else [value]:
                if isinstance(child, ast.AST):
                    scan(child, name, deeper)

    for name, tree in trees.items():
        scan(tree, name, False)
    out = {}
    for name, tree in trees.items():
        if not name.startswith("sqlglot/"):
            continue
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            prefix = name + ":" + node.name
            family = [k for k in observable if k == prefix or k.startswith(prefix + ".")]
            if prefix not in observable:
                continue
            mask = named.get(node.name, 0)
            for key, users in used.items():
                if key == prefix or key.startswith(prefix + "."):
                    mask |= users
            lo = min([node.lineno] + [d.lineno for d in node.decorator_list])
            out.setdefault(name, []).append([lo, node.end_lineno, format(mask, "x")])
    return out


classes = class_users()

with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    def put(name, data):
        info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        z.writestr(info, data)

    put("meta.json", json.dumps({
        "format": 1,
        "tests": ids,
        "untraced": sorted({i for i, _ in bad}),
        "opens": {name: format(mask, "x") for name, mask in sorted(opens.items())},
        "files": base_files,
        "cov": sorted(set(importers) | set(fn)),
    }, separators=(",", ":")))
    for name in sorted(set(importers) | set(fn)):
        sets, index, lines = [], {}, {}
        for line, mask in sorted(fn.get(name, {}).items()):
            if mask not in index:
                index[mask] = len(sets)
                sets.append(format(mask, "x"))
            lines[str(line)] = index[mask]
        put(f"cov/{name}.json", json.dumps({
            "importers": format(importers.get(name, 0), "x"),
            "implines": sorted(implines.get(name, ())),
            "sets": sets,
            "lines": lines,
            "classes": classes.get(name, []),
        }, separators=(",", ":")))
    for name in base_files:
        if name.endswith(".py") and (name.startswith("sqlglot/") or name.startswith("tests/")):
            put(f"src/{name}", open(os.path.join(root, name), "rb").read())
print(f"wrote {out_zip}: {os.path.getsize(out_zip) / 1e6:.2f} MB, {len(ids)} tests, "
      f"{len(set(importers) | set(fn))} traced files, {len(opens)} opened files", file=sys.stderr)
