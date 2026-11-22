"""How fast is this machine, relative to the one the CPU estimates were made on?

The verifier's estimates (select.Ctx.est) are CPU seconds on the reference machine. CPU spent
here is added to the score second for second, so whether running a test pays depends on this
machine's real speed. `factor()` is the current best guess of (CPU here) / (estimate): a short
fixed benchmark at start-up, then corrected by every verification whose CPU we measured.
"""
import os
import time

REFERENCE_S = 0.0  # CPU seconds the benchmark takes on the reference machine; set below
PRIOR_WEIGHT = 2.0  # the benchmark counts like this many estimated seconds of real evidence
LOW, HIGH = 0.6, 5.0

_bench = None
_est = 0.0
_actual = 0.0


def _work():
    # Interpreter-bound work of the kind the tests do: calls, attribute access, dicts, strings.
    class Node:
        __slots__ = ("kind", "args")

        def __init__(self, kind, args):
            self.kind = kind
            self.args = args

    total = 0
    table = {}
    for i in range(30000):
        node = Node("k%d" % (i % 97), {"this": i, "expression": i + 1})
        table[node.kind] = table.get(node.kind, 0) + node.args["this"]
        if isinstance(node, Node) and node.kind.startswith("k1"):
            total += len(node.kind)
    return total + len(table)


def _measure():
    best = None
    for _ in range(3):
        start = time.process_time()
        _work()
        spent = time.process_time() - start
        best = spent if best is None or spent < best else best
    return best


def factor():
    global _bench
    forced = os.environ.get("BR_PACE")  # test aid: pretend the machine has this speed
    if forced:
        return float(forced)
    if _bench is None:
        try:
            _bench = _measure() / REFERENCE_S if REFERENCE_S > 0 else 1.0
        except Exception:
            _bench = 1.0
    value = (PRIOR_WEIGHT * _bench + _actual) / (PRIOR_WEIGHT + _est)
    return min(HIGH, max(LOW, value))


def observe(estimated_s, cpu_s):
    """Record one finished verification: what we estimated and what it really cost."""
    global _est, _actual
    if estimated_s > 0 and cpu_s >= 0:
        _est += estimated_s
        _actual += cpu_s


REFERENCE_S = 0.0275  # measured on the machine the estimates were validated on
