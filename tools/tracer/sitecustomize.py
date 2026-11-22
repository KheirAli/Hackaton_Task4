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
