"""Entry point: the selector (main.py) plus one guard for changes that break imports.

A patch can make a sqlglot module, or a test file, fail to import: an argument made required
that an SQL template parsed at class-definition time omits, an invalid regex at module level,
a helper that now raises while a dialect loads. Then every test of the affected test files
fails, including tests that never run the changed line, so line coverage alone under-selects.

main.py runs first. Then, for each patch for which it selected many tests and which touches
code that runs at import time, a forked child imports the whole patched package (about 0.2 s
of CPU). For every module that no longer imports, all tests
that imported it on the pinned commit are added (guard/blast_index.zip, a map of the pinned
commit), together with the full static selection of guard/evidence.py for that patch. Changed
test files are imported too; if one fails, all its tests are added.

Whatever goes wrong in the guard, main.py's answer is printed unchanged.
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("BR_ROOT", "/repo")
DATA = os.environ.get("BR_DATA", "/data")
GUARD_WALL_S = float(os.environ.get("BR_GUARD_WALL", "30"))   # total wall budget for all probes
PROBE_TIMEOUT_S = float(os.environ.get("BR_GUARD_PROBE_TIMEOUT", "10"))
MIN_SELECTED = int(os.environ.get("BR_GUARD_MIN_SELECTED", "30"))  # probe only commits with this many selected tests
BATCH_WALL_S = float(os.environ.get("BR_GUARD_BATCH_WALL", "165"))  # never probe past this point of the batch


def log(*parts):
    if os.environ.get("BR_GUARD_DEBUG"):
        print("guard:", *parts, file=sys.stderr)


def probe(patched, modules):
    """Import the patched package in a forked child and return the modules that fail to import.

    This process has never imported sqlglot, so the child starts clean; it only inherits the
    already loaded standard library (unittest included: sqlglot switches on strict argument
    checks when a test runner is loaded, as in the real run)."""
    import importlib
    import importlib.abc
    import importlib.util
    import select
    import signal
    import unittest  # noqa: F401

    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(read_fd)
            os.setsid()
            table = {}
            for rel, text in patched.items():
                name = rel[:-3].replace("/", ".")
                package = name.endswith(".__init__")
                if package:
                    name = name[: -len(".__init__")]
                table[name] = (os.path.join(ROOT, rel), text, package)

            class Finder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
                def find_spec(self, name, path=None, target=None):
                    entry = table.get(name)
                    if entry is None:
                        return None
                    return importlib.util.spec_from_file_location(
                        name, entry[0], loader=self,
                        submodule_search_locations=[os.path.dirname(entry[0])] if entry[2] else None)

                def create_module(self, spec):
                    return None

                def exec_module(self, module):
                    filename, text, _ = table[module.__spec__.name]
                    exec(compile(text, filename, "exec", dont_inherit=True), module.__dict__)

            sys.dont_write_bytecode = True
            sys.path.insert(0, ROOT)
            os.chdir(ROOT)
            os.environ["SKIP_INTEGRATION"] = "0"
            sys.meta_path.insert(0, Finder())
            sink = os.open(os.devnull, os.O_WRONLY)
            os.dup2(sink, 1)
            os.dup2(sink, 2)
            failed = []
            for name in modules:
                try:
                    importlib.import_module(name)
                except BaseException:  # noqa: BLE001
                    failed.append(name)
            os.write(write_fd, json.dumps(failed).encode())
        finally:
            os._exit(0)
    os.close(write_fd)
    data = b""
    deadline = time.time() + PROBE_TIMEOUT_S
    try:
        while True:
            left = deadline - time.time()
            if left <= 0 or not select.select([read_fd], [], [], left)[0]:
                raise TimeoutError("import probe timed out")
            chunk = os.read(read_fd, 65536)
            if not chunk:
                break
            data += chunk
    finally:
        os.close(read_fd)
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass
        os.waitpid(pid, 0)
    return json.loads(data.decode())


def guard_sets(only, deadline):
    """commit id -> set of test numbers that must be selected because imports break.
    only: the commits worth probing; deadline: wall-clock time by which all probes must end."""
    sys.path.insert(0, os.path.join(HERE, "guard"))
    import ast
    import evidence

    index = evidence.Index(os.path.join(HERE, "guard", "blast_index.zip"))
    import_lines = json.load(open(os.path.join(HERE, "guard", "import_lines.json")))
    catalog = json.load(open(os.path.join(DATA, "tests.json"), encoding="utf-8"))["tests"]
    number_of = {t["id"]: int(t["number"]) for t in catalog}
    by_file = {}
    for t in catalog:
        by_file.setdefault(t["file"], set()).add(int(t["number"]))
    modules = sorted(
        name[:-3].replace("/", ".").removesuffix(".__init__")
        for name in index.files
        if name.startswith("sqlglot/") and name.endswith(".py") and name != "sqlglot/__main__.py"
    )
    file_of = {name[:-3].replace("/", ".").removesuffix(".__init__"): name
               for name in index.files if name.endswith(".py")}

    def numbers(mask):
        out, bit = set(), 0
        while mask:
            if mask & 1:
                n = number_of.get(index.tests[bit])
                if n is not None:
                    out.add(n)
            mask >>= 1
            bit += 1
        return out

    result = {}
    started = time.time()
    commits_dir = os.path.join(DATA, "commits")
    for entry in sorted(os.listdir(commits_dir)):
        if not entry.endswith(".patch"):
            continue
        commit = entry[:-6]
        if commit not in only:
            continue
        try:
            text = open(os.path.join(commits_dir, entry), encoding="utf-8", errors="surrogateescape").read()
            patched, extra, need, broken = {}, [], False, set()
            for change in evidence.parse_patch(text):
                name = change["old"]
                if not name or name != change["new"] or not name.endswith(".py") or not change["hunks"]:
                    continue
                if not name.startswith(("sqlglot/", "tests/")) or index.src(name) is None:
                    continue
                try:
                    new_text, old_ranges, new_ranges = evidence.apply_hunks(index.src(name), change["hunks"])
                    new_tree = ast.parse(new_text)
                except (ValueError, SyntaxError):
                    continue  # main.py already treats unappliable and unparsable files as broken
                regions = evidence.diff_trees(index.tree(name), new_tree, old_ranges, new_ranges)
                if not regions:
                    continue
                patched[name] = new_text
                at_import = set(import_lines.get(name, ()))
                for kind, lo, hi, *_ in regions:
                    if kind == "lit" and index.literal_users(name, lo, hi) is not None:
                        continue  # a literal whose name is only read inside functions cannot break an import
                    if kind in ("imp", "lit") or any(line in at_import for line in range(lo, min(hi, lo + 5000) + 1)):
                        need = True
                if name.startswith("tests/"):
                    module = name[:-3].replace("/", ".")
                    if any(kind in ("imp", "lit") for kind, *_ in regions) and name in by_file:
                        extra.append(module)
                        need = True
            if not need or time.time() - started > GUARD_WALL_S or time.time() + PROBE_TIMEOUT_S > deadline:
                continue
            try:
                failed = probe(patched, modules + extra)
            except Exception as exc:  # hang or crash while importing: every changed file counts as broken
                log(commit, "probe failed", repr(exc)[:80])
                failed = [name[:-3].replace("/", ".").removesuffix(".__init__") for name in patched]
            for module in failed:
                name = file_of.get(module)
                if name is None:
                    continue
                broken |= numbers(index.touchers(name))
                broken |= by_file.get(name, set())
            if broken:
                # The package is damaged: besides the tests that cannot import, anything that
                # runs the changed code may fail in ways a half-imported package hides. Add the
                # full static selection for this patch (every test if it cannot be bounded).
                try:
                    broken |= numbers(evidence.select(index, text))
                    broken |= {number_of[t] for t in index.untraced if t in number_of}
                except Exception:
                    broken = set(number_of.values())
                log(commit, "modules no longer import:", failed[:6], "->", len(broken), "tests")
                result[commit] = broken
        except Exception as exc:  # noqa: BLE001
            log(commit, "skipped", repr(exc)[:120])
    return result, {int(t["number"]): int(float(t["seconds"]) * 1000 + 0.5) for t in catalog}, evidence.schedule


def main():
    started = time.time()
    proc = subprocess.run([sys.executable, os.path.join(HERE, "main.py")], stdout=subprocess.PIPE)
    out = proc.stdout
    if proc.returncode == 0 and os.environ.get("BR_NO_IMPORT_GUARD") != "1":
        try:
            rows = [json.loads(line) for line in out.decode("utf-8").splitlines()]
            # A broken import fails every test of whole test files: hundreds of tests. The
            # selector then already selects many of them, so only such commits are probed and
            # an ordinary batch pays nothing for the guard.
            suspects = {row["commit"] for row in rows if sum(len(w) for w in row["workers"]) >= MIN_SELECTED}
            if suspects:
                extra, ms, schedule = guard_sets(suspects, started + BATCH_WALL_S)
                if extra:
                    lines = []
                    for row in rows:
                        add = extra.get(row["commit"])
                        if add:
                            have = {n for worker in row["workers"] for n in worker}
                            if not add <= have:
                                row["workers"] = schedule([(n, ms[n]) for n in sorted(have | add)])
                        lines.append(json.dumps(row, separators=(",", ":")))
                    out = ("\n".join(lines) + "\n").encode("utf-8")
        except Exception as exc:  # noqa: BLE001
            log("skipped", repr(exc)[:200])
            out = proc.stdout
    sys.stdout.buffer.write(out)
    sys.stdout.flush()
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
