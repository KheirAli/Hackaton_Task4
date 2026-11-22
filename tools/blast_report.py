"""pytest plugin: write the node ids of failing tests (and files that fail to import)."""
import json
import os

_failed, _broken = set(), set()


def pytest_runtest_logreport(report):
    if report.failed:
        _failed.add(report.nodeid)


def pytest_collectreport(report):
    if report.failed:
        _broken.add(report.nodeid)


def pytest_sessionfinish(session):
    if os.environ.get("PYTEST_XDIST_WORKER"):
        return
    with open(os.environ["BLAST_REPORT"], "w") as handle:
        json.dump({"failed": sorted(_failed), "broken": sorted(_broken)}, handle)
