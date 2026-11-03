# Apollo + LinkedIn: simulated implementation tasks

## Test Coverage Matrix

Guidelines: `AGENTS.md`, `README.md`; Python standard `unittest`, isolated fakes and temporary state.

| Code Layer | Required Test Type | Coverage Expectation | Location Pattern | Run Command |
| --- | --- | --- | --- | --- |
| Simulated reader and executor | unit and isolated integration | AL-01 through AL-21, including separate OS processes contending for one store, account lock and replay | `_ferramentas/test_thunder_os_apollo_linkedin_simulation.py` | `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas python3 -m unittest _ferramentas.test_thunder_os_apollo_linkedin_simulation` |

## Gate Check Commands

| Gate Level | When to Use | Command |
| --- | --- | --- |
| Quick | Focused simulation changes | `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas python3 -m unittest _ferramentas.test_thunder_os_apollo_linkedin_simulation` |
| Boundary | Before commit | `python3 scripts/check_source_boundary.py` |

## Execution Plan

Complete T1, then T2. Both are confined to fake providers and focused validation.

## Task Breakdown

### T1: Simulated reader and executor

**What**: Implement the isolated task reader, eligibility, state and fake-only execution.
**Where**: `_ferramentas/thunder_os/apollo_linkedin_simulation.py`, `_ferramentas/test_thunder_os_apollo_linkedin_simulation.py`.
**Depends on**: none.
**Done when**: AL-01 through AL-21 have focused passing tests, including separate processes sharing one temporary official store, account-lock contention, missing/unavailable-store initialization failure and immediate task reread fail-closed; no live adapter or scheduler is wired.
**Tests**: unit and isolated integration.
**Gate**: Quick and Boundary.

### T2: Independent review

**What**: Verify diff and requirements against test assertions; report evidence and residual risk.
**Where**: `.specs/features/apollo-linkedin/validation.md` if the independent verifier writes a report.
**Depends on**: T1.
**Done when**: reviewer verifies the diff and focused test evidence.
**Tests**: independent review.
**Gate**: Quick and Boundary.

## Phase Execution Map

Phase 1: T1 → T2
