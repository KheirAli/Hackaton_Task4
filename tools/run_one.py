#!/usr/bin/env python3
"""Run ONE catalog test of the clean checkout: run_one.py ROOT TEST_ID OUTCOME_FILE"""
import os
import sys
import unittest

if __name__ == "__main__":
    root, test_id, outcome_file = sys.argv[1:4]
    outcome_file = os.path.abspath(outcome_file)
    os.chdir(root)
    sys.path.insert(0, os.path.realpath(root))
    path, cls, method = test_id.split("::")
    dotted = path[:-3].replace("/", ".") + "." + cls + "." + method
    outcome, detail = "error", ""
    try:
        suite = unittest.defaultTestLoader.loadTestsFromName(dotted)
        with open(os.devnull, "w") as sink:
            result = unittest.TextTestRunner(stream=sink, verbosity=0).run(suite)
        if result.skipped:
            outcome = "skipped"
        elif result.wasSuccessful() and result.testsRun == 1:
            outcome = "passed"
        else:
            outcome = "failed"
            detail = "".join(t for _, t in result.failures + result.errors)[-1500:]
    except BaseException as exc:  # noqa: BLE001
        detail = repr(exc)
    with open(outcome_file, "w") as handle:
        handle.write(outcome + "\n" + detail)
