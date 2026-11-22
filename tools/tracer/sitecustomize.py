"""Line/open tracer loaded into EVERY Python process of a traced test.

Activated by BLAST_TRACE_OUT (output prefix) and BLAST_TRACE_ROOT (checkout).
Being a sitecustomize module on PYTHONPATH, it also covers multiprocessing
"spawn" workers and `python -c` subprocesses started by tests.

Per process it writes <prefix>.<pid>.json with, for repository files only:
  imp   - lines run by module / class-body code (import time)
  fn    - lines run by function / lambda code (call time)
  opens - repository files opened, excluding plain imports
  classes - repository classes used: "file:QualName" for type(self) of every
          repository method that ran, together with its base classes
Build-time tool. It never reads patches or answers.
"""
import os
import sys


def _install():
    prefix = os.environ.get("BLAST_TRACE_OUT")
    root = os.environ.get("BLAST_TRACE_ROOT")
    if not prefix or not root or not hasattr(sys, "monitoring"):
        return
    import atexit
    import json

    root = os.path.realpath(root) + os.sep
    imp, fn, opens = set(), set(), set()
    codes = {}
    starts = {}
    used_types = set()
    getframe = sys._getframe
    mon = sys.monitoring
    tool = mon.COVERAGE_ID
    realpath = os.path.realpath

    def on_line(code, line):
        key = codes.get(code)
        if key is None:
            name = code.co_filename
            if name.startswith(root):
                key = (name[len(root):], bool(code.co_flags & 1))  # CO_OPTIMIZED: function-like
            else:
                key = False
            codes[code] = key
        if key:
            (fn if key[1] else imp).add((key[0], line))
        return mon.DISABLE

    def on_start(code, offset):
        # Which classes did this process use? Any call of a repository method
        # on an instance marks type(self) (and its bases). Classes themselves
        # (metaclass and classmethod calls, which also run at import) do not count.
        first = starts.get(code)
        if first is None:
            first = False
            if code.co_flags & 1 and code.co_filename.startswith(root):
                # CPython reports no LINE event for a function's own first line,
                # so a one-line lambda or `def f(): return x` would look unused.
                rel = code.co_filename[len(root):]
                fn.add((rel, code.co_firstlineno))
                for _, _, line in code.co_lines():
                    if line:
                        fn.add((rel, line))
                        break
            if code.co_argcount and code.co_filename.startswith(root):
                name = code.co_varnames[0]
                if name == "self":
                    first = name
            starts[code] = first
        if first is False:
            return mon.DISABLE
        try:
            obj = getframe(1).f_locals.get(first)
        except Exception:
            return None
        if obj is not None and not isinstance(obj, type):
            used_types.add(type(obj))
        return None

    def audit(event, args):
        if event != "open":
            return
        path = args[0]
        if isinstance(path, bytes):
            try:
                path = path.decode()
            except UnicodeDecodeError:
                return
        if not isinstance(path, str):
            return
        try:
            full = realpath(path)
        except Exception:
            return
        if not full.startswith(root):
            return
        rel = full[len(root):]
        if rel.endswith(".pyc") or "__pycache__" in rel:
            return
        if rel.endswith(".py"):
            frame = sys._getframe(1)
            while frame is not None:
                if frame.f_code.co_filename.startswith("<frozen importlib"):
                    return
                frame = frame.f_back
        opens.add(rel)

    # ---- optional comparison profile (BLAST_TRACE_CMP=1) -------------------
    # Every single-operator comparison inside a function of sqlglot/ is routed
    # through a probe that also evaluates the other comparison operators on
    # the same operands. Per site we keep which alternatives EVER gave a
    # different outcome in this process. If `a is b` and `a == b` agreed every
    # time a test reached the site, swapping one for the other cannot change
    # what that test does.
    cmp_sites, cmp_state = [], {}
    if os.environ.get("BLAST_TRACE_CMP") == "1":
        import ast
        import builtins
        import importlib.machinery
        import operator

        names = ["Is", "IsNot", "Eq", "NotEq", "Lt", "LtE", "Gt", "GtE"]
        funcs = [operator.is_, operator.is_not, operator.eq, operator.ne,
                 operator.lt, operator.le, operator.gt, operator.ge]
        full = (1 << len(funcs)) - 1

        def probe(site, own, left, right):
            state = cmp_state.get(site, 0)
            try:
                result = funcs[own](left, right)
            except BaseException:
                cmp_state[site] = full
                raise
            if state != full:
                if result is True or result is False:
                    for other in range(len(funcs)):
                        bit = 1 << other
                        if other == own or state & bit:
                            continue
                        try:
                            if funcs[other](left, right) is not result:
                                state |= bit
                        except Exception:
                            state |= bit
                else:
                    state = full
                cmp_state[site] = state
            return result

        builtins.__blast_cmp__ = probe

        class Rewriter(ast.NodeTransformer):
            def __init__(self, rel):
                self.rel, self.depth = rel, 0

            def _function(self, node):
                self.depth += 1
                self.generic_visit(node)
                self.depth -= 1
                return node

            visit_FunctionDef = visit_AsyncFunctionDef = visit_Lambda = _function

            def visit_Compare(self, node):
                key = (self.rel, node.lineno, node.col_offset)
                self.generic_visit(node)
                kind = type(node.ops[0]).__name__ if len(node.ops) == 1 else None
                if not self.depth or kind not in names:
                    return node
                cmp_sites.append(key)
                call = ast.Call(
                    func=ast.Name(id="__blast_cmp__", ctx=ast.Load()),
                    args=[ast.Constant(len(cmp_sites) - 1), ast.Constant(names.index(kind)),
                          node.left, node.comparators[0]],
                    keywords=[],
                )
                return ast.copy_location(call, node)

        plain_get_code = importlib.machinery.SourceFileLoader.get_code
        package = root + "sqlglot" + os.sep

        def get_code(self, fullname):
            path = self.get_filename(fullname)
            if path.startswith(package) and path.endswith(".py"):
                tree = ast.parse(self.get_data(path), path)
                tree = ast.fix_missing_locations(Rewriter(path[len(root):]).visit(tree))
                return compile(tree, path, "exec", dont_inherit=True)
            return plain_get_code(self, fullname)

        importlib.machinery.SourceFileLoader.get_code = get_code

    def dump():
        out = {"imp": {}, "fn": {}, "opens": sorted(opens)}
        classes = set()
        for used in list(used_types):
            for klass in getattr(used, "__mro__", ()):
                module = sys.modules.get(getattr(klass, "__module__", None))
                path = getattr(module, "__file__", None) or ""
                if path.startswith(root) and "<locals>" not in klass.__qualname__:
                    classes.add(path[len(root):] + ":" + klass.__qualname__)
        out["classes"] = sorted(classes)
        if cmp_sites:
            sites = {}
            for site, state in list(cmp_state.items()):
                rel, line, col = cmp_sites[site]
                sites.setdefault(rel, {})["%d:%d" % (line, col)] = state
            out["cmp"] = sites
        for key, source in (("imp", imp), ("fn", fn)):
            for name, line in list(source):
                out[key].setdefault(name, []).append(line)
        path = "%s.%d.json" % (prefix, os.getpid())
        with open(path + ".tmp", "w") as handle:
            json.dump(out, handle, separators=(",", ":"))
        os.replace(path + ".tmp", path)

    try:
        mon.use_tool_id(tool, "blast")
    except ValueError:
        return
    mon.register_callback(tool, mon.events.LINE, on_line)
    mon.register_callback(tool, mon.events.PY_START, on_start)
    mon.set_events(tool, mon.events.LINE | mon.events.PY_START)
    sys.addaudithook(audit)
    atexit.register(dump)
    try:  # multiprocessing workers leave through os._exit after running finalizers
        from multiprocessing import util

        util.Finalize(None, dump, exitpriority=-100)
    except Exception:
        pass


try:
    _install()
except Exception:
    pass
