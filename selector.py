#!/usr/bin/env python3
"""RWX Blast Radius selector.

For each patch, pick the tests whose recorded execution on the clean pinned
sqlglot commit touches what the patch changes, then pack them onto workers.

Evidence comes from blast_index.zip, a map of the pinned commit only (built by
tools/): every catalog test was run alone in a fresh process tree and we kept
  * which lines it ran inside functions (call time),
  * which files it imported and which lines ran at import time,
  * which repository files it opened (fixtures, data, sources read as text).

Per changed file:
  * Python: the patch is applied to the base text and old/new ASTs are compared.
    Identical ASTs (comments, blank lines) change nothing. A difference inside
    a function body selects the tests that ran those lines; a difference in a
    signature selects the tests that ran the function; a difference in module
    or class level code selects every test that imported the file.
  * Anything else: the tests that opened the file.
Anything the selector cannot explain falls back to a wider set, in the end to
every test. No patch, edit kind or answer is looked up anywhere.

Stdout carries exactly one JSON line per commit; diagnostics go to stderr.
"""
import ast
import json
import os
import re
import sys
import zipfile
from pathlib import Path

DATA = Path(os.environ.get("BLAST_DATA", "/data"))
INDEX = Path(os.environ.get("BLAST_INDEX", Path(__file__).resolve().parent / "blast_index.zip"))
DEBUG = os.environ.get("BLAST_DEBUG") == "1"
CLASS_NARROWING = os.environ.get("BLAST_CLASS_NARROWING", "0") == "1"
EAGER_SHARE = float(os.environ.get("BLAST_EAGER_SHARE", "0.9"))
MAX_WORKERS = 8
WORKER_OVERHEAD_MS = 2000
FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
# Files whose change can alter how the whole suite is collected or configured.
GLOBAL_FILES = {"conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "setup.py"}


class Everything(Exception):
    """Raised when a change cannot be bounded: select every test."""


def log(*parts):
    if DEBUG:
        print(*parts, file=sys.stderr)


# --------------------------------------------------------------------------- index


class Index:
    def __init__(self, path):
        self.zip = zipfile.ZipFile(path)
        meta = json.loads(self.zip.read("meta.json"))
        self.tests = meta["tests"]
        self.opens = meta["opens"]
        self.files = set(meta["files"])
        self.cov_names = set(meta["cov"])
        self.untraced = meta.get("untraced", [])
        self._cov = {}
        self._src = {}
        self._tree = {}
        self._dir_opens = {}

    def cov(self, name):
        if name not in self._cov:
            if name in self.cov_names:
                raw = json.loads(self.zip.read("cov/" + name + ".json"))
                sets = [int(s, 16) for s in raw["sets"]]
                entry = {
                    "importers": int(raw["importers"], 16),
                    "implines": raw["implines"],
                    "lines": {int(k): sets[v] for k, v in raw["lines"].items()},
                    "classes": [(lo, hi, int(m, 16)) for lo, hi, m in raw.get("classes", ())],
                }
            else:
                entry = {"importers": 0, "implines": [], "lines": {}, "classes": []}
            self._cov[name] = entry
        return self._cov[name]

    def src(self, name):
        if name not in self._src:
            try:
                self._src[name] = self.zip.read("src/" + name).decode("utf-8")
            except KeyError:
                self._src[name] = None
        return self._src[name]

    def tree(self, name):
        if name not in self._tree:
            self._tree[name] = ast.parse(self.src(name))
        return self._tree[name]

    def openers(self, name):
        return int(self.opens.get(name, "0"), 16)

    def dir_openers(self, name):
        """Tests that opened anything in the nearest ancestor directory with opened files."""
        parts = name.split("/")[:-1]
        while parts:
            prefix = "/".join(parts) + "/"
            if prefix not in self._dir_opens:
                mask = 0
                for other, hexmask in self.opens.items():
                    if other.startswith(prefix):
                        mask |= int(hexmask, 16)
                self._dir_opens[prefix] = mask
            if self._dir_opens[prefix]:
                return self._dir_opens[prefix]
            parts.pop()
        return 0

    def touchers(self, name):
        """Every test with any recorded contact with a Python file."""
        entry = self.cov(name)
        mask = entry["importers"] | self.openers(name)
        for line_mask in entry["lines"].values():
            mask |= line_mask
        return mask

    def fn_range(self, name, lo, hi):
        lines = self.cov(name)["lines"]
        mask = 0
        if hi - lo < len(lines):
            for line in range(lo, hi + 1):
                mask |= lines.get(line, 0)
        else:
            for line, line_mask in lines.items():
                if lo <= line <= hi:
                    mask |= line_mask
        return mask

    def imp_range(self, name, lo, hi):
        """Tests affected by a change to import-time code on lines lo..hi.

        Normally that is every test that imported the file. When the file is
        imported by (nearly) every test, that says nothing, so a change that
        sits strictly inside one top-level class is narrowed to the tests that
        used that class: ran a method on an instance of it (or of a nested
        class or subclass), or ran a function line that names it.
        """
        entry = self.cov(name)
        if not any(lo <= line <= hi for line in entry["implines"]):
            return 0
        importers = entry["importers"]
        if CLASS_NARROWING and bin(importers).count("1") >= EAGER_SHARE * len(self.tests):
            for c_lo, c_hi, users in entry["classes"]:
                if c_lo < lo and hi <= c_hi and (lo, hi) != (c_lo, c_hi):
                    return users & importers
        return importers


# --------------------------------------------------------------------------- patches

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _path(raw):
    raw = raw.split("\t")[0].strip()
    if raw == "/dev/null":
        return None
    if raw.startswith('"'):
        raise Everything("quoted path")
    if raw.startswith(("a/", "b/")):
        raw = raw[2:]
    return raw


def parse_patch(text):
    """Return a list of file changes: {old, new, hunks, opaque}."""
    files = []
    current = None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            current = {"old": None, "new": None, "hunks": [], "opaque": False, "header": line}
            files.append(current)
            match = re.match(r"^diff --git a/(.*) b/(.*)$", line)
            if match and '"' not in line:
                current["old"], current["new"] = match.group(1), match.group(2)
                current["guess"] = True
            i += 1
            continue
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            if current is None or current["hunks"] or current.get("named"):
                current = {"old": None, "new": None, "hunks": [], "opaque": False}
                files.append(current)
            current["old"] = _path(line[4:])
            current["new"] = _path(lines[i + 1][4:])
            current["named"] = True
            i += 2
            continue
        if current is not None:
            if line.startswith(("rename from ", "copy from ")):
                current["old"] = line.split(" ", 2)[2]
                current["renamed"] = True
            elif line.startswith(("rename to ", "copy to ")):
                current["new"] = line.split(" ", 2)[2]
                current["renamed"] = True
            elif line.startswith("new file mode"):
                current["old"], current["created"] = None, True
            elif line.startswith("deleted file mode"):
                current["deleted"] = True
            elif line.startswith(("GIT binary patch", "Binary files ")):
                current["opaque"] = True
            elif line.startswith(("old mode", "new mode")):
                current["mode"] = True
            match = HUNK.match(line)
            if match:
                old_start = int(match.group(1))
                old_count = int(match.group(2) or 1)
                new_count = int(match.group(4) or 1)
                body = []
                i += 1
                seen_old = seen_new = 0
                while i < len(lines) and (seen_old < old_count or seen_new < new_count):
                    row = lines[i]
                    tag = row[:1]
                    if tag == "\\":
                        if body:
                            body[-1] = (body[-1][0], body[-1][1], True)
                    elif tag == "-":
                        body.append(("-", row[1:], False))
                        seen_old += 1
                    elif tag == "+":
                        body.append(("+", row[1:], False))
                        seen_new += 1
                    else:  # context; a fully empty line is an empty context line
                        body.append((" ", row[1:], False))
                        seen_old += 1
                        seen_new += 1
                    i += 1
                if i < len(lines) and lines[i].startswith("\\"):
                    if body:
                        body[-1] = (body[-1][0], body[-1][1], True)
                    i += 1
                current["hunks"].append((old_start, old_count, body))
                continue
        i += 1
    return files


def apply_hunks(base_text, hunks):
    """Apply hunks strictly. Returns (new_text, old_ranges, new_ranges)."""
    base = base_text.split("\n")
    out = []
    old_ranges, new_ranges = [], []
    cursor = 0  # index into base
    for old_start, old_count, body in hunks:
        start = old_start - 1 if old_count else old_start
        if start < cursor:
            raise ValueError("overlapping hunks")
        out.extend(base[cursor:start])
        cursor = start
        for tag, content, no_newline in body:
            if tag == "+":
                out.append(content)
                new_ranges.append((len(out) - 1, len(out) + 1))
                old_ranges.append((cursor, cursor + 1))
                if no_newline:
                    out.append(None)  # marker: file ends here without newline
            else:
                if cursor >= len(base) or base[cursor].rstrip("\r") != content.rstrip("\r"):
                    raise ValueError("context mismatch at old line %d" % (cursor + 1))
                if tag == " ":
                    out.append(base[cursor])
                else:
                    old_ranges.append((cursor + 1, cursor + 1))
                    new_ranges.append((len(out), len(out) + 1))
                cursor += 1
    out.extend(base[cursor:])
    out = [row for row in out if row is not None]
    return "\n".join(out), old_ranges, new_ranges


# --------------------------------------------------------------------------- AST diff


def _span(node):
    lo, hi = node.lineno, node.end_lineno or node.lineno
    for deco in getattr(node, "decorator_list", ()):
        lo = min(lo, deco.lineno)
    return lo, hi


def _dump(value):
    if isinstance(value, list):
        return "[" + ",".join(_dump(v) for v in value) + "]"
    if isinstance(value, ast.AST):
        return ast.dump(value)
    return repr(value)


def _touches(node, ranges):
    lineno = getattr(node, "lineno", None)
    if lineno is None:
        return True
    lo, hi = _span(node)
    for a, b in ranges:
        if a <= hi and lo <= b:
            return True
    return False


def diff_trees(old, new, old_ranges, new_ranges):
    """Regions of the OLD tree that differ: list of (kind, lo, hi), kind 'fn' or 'imp'."""
    regions = []

    def report(node, fn, stmt):
        if fn is None:
            if stmt is None:
                regions.append(("imp", 1, 10**9))
            else:
                regions.append(("imp",) + _span(stmt))
        elif isinstance(fn, ast.Lambda) or stmt is None or stmt is fn:
            regions.append(("fn",) + _span(fn))
        else:
            lo, hi = _span(stmt)
            body = getattr(stmt, "body", None)
            if (
                node is not stmt
                and isinstance(node, ast.expr)
                and isinstance(body, list)
                and body
                and isinstance(body[0], ast.stmt)
                and (node.end_lineno or node.lineno) < body[0].lineno
            ):
                hi = body[0].lineno - 1
            regions.append(("fn", lo, hi))

    def maybe(a, b, fn, stmt):
        if _touches(a, old_ranges) or _touches(b, new_ranges):
            walk(a, b, fn, stmt)

    def walk_list(la, lb, fn, stmt, parent):
        la = la if isinstance(la, list) else []
        lb = lb if isinstance(lb, list) else []
        if len(la) == len(lb):
            for a, b in zip(la, lb):
                if isinstance(a, ast.AST) and isinstance(b, ast.AST):
                    maybe(a, b, fn, stmt)
                elif _dump(a) != _dump(b):
                    report(parent, fn, stmt)
            return
        statements = all(isinstance(x, ast.stmt) for x in la + lb)
        if not statements:
            report(parent, fn, stmt)
            return
        da, db = [_dump(x) for x in la], [_dump(x) for x in lb]
        head = 0
        while head < len(da) and head < len(db) and da[head] == db[head]:
            head += 1
        tail = 0
        while tail < len(da) - head and tail < len(db) - head and da[-1 - tail] == db[-1 - tail]:
            tail += 1
        for gone in la[head:len(la) - tail]:
            report(gone, fn, gone)
        if len(lb) - tail > head:  # statements were inserted
            if isinstance(fn, FUNCS):
                regions.append(("fn",) + _span(fn))
            elif fn is not None:
                report(parent, fn, stmt)
            elif isinstance(parent, ast.stmt):
                regions.append(("imp",) + _span(parent))
            else:
                regions.append(("imp", 1, 10**9))

    def walk(a, b, fn, stmt):
        if type(a) is not type(b):
            report(a, fn, stmt)
            return
        if isinstance(a, ast.stmt):
            stmt = a
        if isinstance(a, FUNCS):
            if a.name != b.name or _dump(a.decorator_list) != _dump(b.decorator_list):
                report(a, fn, a)
                return
            if (
                _dump(a.args) != _dump(b.args)
                or _dump(a.returns) != _dump(b.returns)
                or _dump(getattr(a, "type_params", [])) != _dump(getattr(b, "type_params", []))
            ):
                regions.append(("fn",) + _span(a))
            walk_list(a.body, b.body, a, None, a)
            return
        if isinstance(a, ast.Lambda):
            if _dump(a.args) != _dump(b.args):
                regions.append(("fn",) + _span(a))
            walk(a.body, b.body, a, stmt)
            return
        for field in a._fields:
            va, vb = getattr(a, field, None), getattr(b, field, None)
            if isinstance(va, list) or isinstance(vb, list):
                walk_list(va, vb, fn, stmt, a)
            elif isinstance(va, ast.AST) and isinstance(vb, ast.AST):
                maybe(va, vb, fn, stmt)
            elif isinstance(va, ast.AST) or isinstance(vb, ast.AST):
                report(a, fn, stmt)
            elif va != vb or type(va) is not type(vb):
                report(a, fn, stmt)

    walk(old, new, None, None)
    return regions


# --------------------------------------------------------------------------- selection


def select_python(index, name, change):
    """Mask of tests affected by a change to a traced-or-known Python file."""
    base = index.src(name)
    try:
        new_text, old_ranges, new_ranges = apply_hunks(base, change["hunks"])
    except ValueError as exc:
        log("   cannot apply:", exc)
        if index.touchers(name) == 0:
            raise Everything("unappliable patch to untraced file")
        return index.touchers(name)
    try:
        new_tree = ast.parse(new_text)
    except (SyntaxError, ValueError) as exc:
        log("   new source does not parse:", exc)
        return index.touchers(name)
    regions = diff_trees(index.tree(name), new_tree, old_ranges, new_ranges)
    mask = index.openers(name)  # tests that read this source as text see any edit
    for kind, lo, hi in regions:
        if kind == "fn":
            part = index.fn_range(name, lo, hi)
        else:
            part = index.imp_range(name, lo, hi) | index.fn_range(name, lo, hi)
        log("   %s %d-%s -> %d tests" % (kind, lo, hi if hi < 10**9 else "end", bin(part).count("1")))
        mask |= part
    if not regions:
        log("   no AST change")
    return mask


def select_file(index, change):
    old, new = change["old"], change["new"]
    mask = 0
    if change["opaque"] and not change["hunks"]:
        log("   binary change")
    for name in {old, new} - {None}:
        base = name.rsplit("/", 1)[-1]
        if base in GLOBAL_FILES and "/" not in name.replace("tests/", "", 1):
            raise Everything("suite configuration file " + name)
    if old is None:  # created file
        if new is None:
            raise Everything("patch without paths")
        if new in index.files:
            raise Everything("creates a file that exists in the base: " + new)
        if new.endswith(".py") and new.startswith(("sqlglot/", "tests/")):
            raise Everything("new Python file " + new)
        if new.startswith("tests/"):
            return index.dir_openers(new)
        return 0
    if old not in index.files:
        raise Everything("unknown base file " + old)
    renamed = new is None or new != old or change.get("deleted")
    is_python = old.endswith(".py") and index.src(old) is not None
    if is_python:
        if renamed or change["opaque"]:
            mask |= index.touchers(old)
            if new and new != old and new.endswith(".py"):
                raise Everything("Python file moved to " + new)
        elif change["hunks"]:
            mask |= select_python(index, old, change)
        return mask
    if old.endswith(".py"):
        return 0 if not change["hunks"] and not renamed else index.openers(old)
    # data file: fixtures, csv, json, docs, configuration
    if not change["hunks"] and not renamed and not change["opaque"]:
        return 0  # mode change only
    mask = index.openers(old)
    if not mask and old.startswith("tests/"):
        mask = index.dir_openers(old)
    if new and new != old and new.startswith("tests/"):
        mask |= index.dir_openers(new)
    return mask


def select(index, patch_text):
    changes = parse_patch(patch_text)
    if not changes:
        if patch_text.strip():
            raise Everything("no file sections recognised")
        return 0
    mask = 0
    for change in changes:
        log("  file", change["old"], "->", change["new"], "hunks", len(change["hunks"]))
        if not change.get("named") and not change.get("renamed") and not change["opaque"] \
                and not change.get("mode") and not change.get("created") and not change.get("deleted"):
            if change["hunks"]:
                raise Everything("hunks without file names")
        part = select_file(index, change)
        log("  ->", bin(part).count("1"), "tests")
        mask |= part
    return mask


# --------------------------------------------------------------------------- schedule


def schedule(jobs):
    """jobs: list of (number, ms). LPT for each worker count, keep the cheapest."""
    if not jobs:
        return []
    ranked = sorted(jobs, key=lambda item: (-item[1], item[0]))
    total = sum(ms for _, ms in ranked)
    longest = ranked[0][1]
    best, best_cost = None, None
    for k in range(1, min(MAX_WORKERS, len(ranked)) + 1):
        floor = max(longest, -(-total // k)) + WORKER_OVERHEAD_MS * k
        if best_cost is not None and floor >= best_cost:
            continue
        workers = [[] for _ in range(k)]
        loads = [0] * k
        for number, ms in ranked:
            w = loads.index(min(loads))
            workers[w].append(number)
            loads[w] += ms
        used = [w for w in workers if w]
        cost = max(loads) + WORKER_OVERHEAD_MS * len(used)
        if best_cost is None or cost < best_cost:
            best, best_cost = used, cost
    return best


def main():
    catalog = json.loads((DATA / "tests.json").read_text(encoding="utf-8"))["tests"]
    patches = sorted((DATA / "commits").glob("*.patch"))
    all_jobs = [(int(t["number"]), int(float(t["seconds"]) * 1000 + 0.5)) for t in catalog]
    everything = None
    try:
        index = Index(INDEX)
        bit_of = {test_id: bit for bit, test_id in enumerate(index.tests)}
        untraced = set(index.untraced)
        job_by_bit, unknown = {}, []
        for test, job in zip(catalog, all_jobs):
            bit = bit_of.get(test["id"])
            if bit is None or test["id"] in untraced:
                unknown.append(job)  # no evidence for this test: never leave it out
            else:
                job_by_bit[bit] = job
    except Exception as exc:  # noqa: BLE001 - a broken index must not break the run
        print("index unavailable (%r): selecting every test" % (exc,), file=sys.stderr)
        index = None
    out = []
    cache = {}
    for patch in patches:
        jobs = None
        if index is not None:
            try:
                text = patch.read_text(encoding="utf-8", errors="surrogateescape")
                log("commit", patch.stem)
                mask = select(index, text)
                jobs = []
                if mask:
                    jobs = list(unknown)
                    bit = 0
                    while mask:
                        low = mask & -mask
                        job = job_by_bit.get(low.bit_length() - 1)
                        if job is not None:
                            jobs.append(job)
                        mask ^= low
            except Everything as exc:
                print("%s: every test (%s)" % (patch.stem, exc), file=sys.stderr)
            except Exception as exc:  # noqa: BLE001
                print("%s: every test (error %r)" % (patch.stem, exc), file=sys.stderr)
        if jobs is None:
            if everything is None:
                everything = schedule(all_jobs)
            workers = everything
        else:
            key = tuple(sorted(n for n, _ in jobs))
            if key not in cache:
                cache[key] = schedule(jobs)
            workers = cache[key]
        out.append(json.dumps({"commit": patch.stem, "workers": workers}, separators=(",", ":")))
    sys.stdout.write("".join(line + "\n" for line in out))
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as exc:  # noqa: BLE001 - last resort: valid all-tests output
        print("fatal %r: falling back to every test" % (exc,), file=sys.stderr)
        catalog = json.loads((DATA / "tests.json").read_text(encoding="utf-8"))["tests"]
        workers = schedule([(int(t["number"]), int(float(t["seconds"]) * 1000 + 0.5)) for t in catalog])
        for patch in sorted((DATA / "commits").glob("*.patch")):
            print(json.dumps({"commit": patch.stem, "workers": workers}, separators=(",", ":")))
        code = 0
    sys.exit(code)
