"""Run existing synthetic reader tests with isolated deterministic rate clocks.

No provider fixture is replaced. Read budgets remain enabled; only their clocks
and volatile scope are fresh per test. Dedicated budget tests assert pacing,
cache and 429 behavior explicitly. This runner is never part of runtime bundles.
Usage: PYTHONPATH=_ferramentas:<pinned-toques-dir> python3 scripts/test_hubspot_readers.py MODULE...
"""
import sys
import unittest
from test_thunder_os_hubspot_read_budget import IsolatedReadBudgetResult


if __name__ == '__main__':
    if len(sys.argv) < 2:
        raise SystemExit('Specify synthetic unittest modules; no discovery or provider access.')
    result = unittest.TextTestRunner(resultclass=IsolatedReadBudgetResult).run(
        unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:]))
    raise SystemExit(not result.wasSuccessful())
