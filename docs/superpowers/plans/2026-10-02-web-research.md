# Web Research Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development for independent implementation tasks and root integration. Steps use checkbox syntax.

**Goal:** Future-trend models autonomously search and read public pages before sharing verified evidence for predictions.

**Architecture:** Freeze market data in the existing batch, run bounded tool research asynchronously, persist progress in public_materials.research, then freeze the completed evidence snapshot before submitting model predictions. Existing AIAnalysisCall rows audit research calls, without a database migration.

**Tech Stack:** Python, httpx, stdlib HTML/XML parsers, SQLAlchemy, unittest, existing frontend JavaScript VM tests.

**Spec:** docs/superpowers/specs/2026-10-02-web-research-design.md

## Global Constraints

- Preserve dirty checkout on codex/future-trend-20261001; do not commit unrelated changes or create an empty checkout lacking earlier approved work.
- Do not edit CTP modules. No new packages, search credentials, automatic orders, or real paid model calls for acceptance.
- Use isolated unittest fixtures. Backend changes require stop.bat/start.bat and health verification.

## Task 1: Tool research and safe web tools

Files: backend/ai_analysis/web_research.py, backend/ai_analysis/provider.py, tests/test_web_research.py. Root extends future_trend.normalize_materials with optional accepted public host set.

Interface: run_research(snapshot, config, completion, progress=None, time_limit=90) returns a public-materials dict; completion(config, body) returns ProviderResult with optional tool_calls. progress(materials) persists public state. Tools search_public_web(query, dimension), read_public_page(candidate_id), latest_official_macro(). Metadata research includes status, actual model identifiers, rounds, search_count, pages_read, events and error messages.

- [x] Write tests for a model calling search, read, then supplementary search; sources derive only from executed tools, unknown/old publications are background.
- [x] Run `.venv\Scripts\python.exe -m unittest tests.test_web_research`; observe missing functionality failures.
- [x] Implement chat and Gemini function-call extraction, public search and bounded reader, three-decision tool loop and progress.
- [x] Test unsupported tools, provider failures, invalid arguments, URLs, redirect/DNS policy, limits and source age.

Representative acceptance assertion:
```python
self.assertEqual(materials['research']['search_count'], 2)
self.assertEqual(materials['research']['pages_read'], 1)
self.assertEqual(materials['sources'][0]['content_type'], 'BODY')
```

## Task 2: Batch integration and freeze

Files: backend/ai_analysis/snapshot.py, backend/ai_analysis/orchestrator.py, backend/ai_analysis/future_trend.py, tests/test_future_trend_integration.py.

- [x] Test initial future snapshot PENDING without public HTTP; shared research executes once before predictions, persists progress, and leaves market unchanged.
- [x] Replace synchronous collection with pending metadata; background preparation selects research model, logs calls, updates final snapshot and hash, then submits predictions.
- [x] Count research calls in sequence and usage; handle research failures and interrupted restart without auto-retry.
- [x] Run future and existing AI integration suites.

Representative acceptance assertions:
```python
self.assertEqual(frozen['market'], before['market'])
self.assertEqual(frozen['public_materials']['research']['status'], 'COMPLETED')
self.assertEqual(research.call_count, 1)
```

## Task 3: Research progress UI

Files: frontend/index.html, tests/check_future_trend.cjs.

- [x] Test model identity, live search/read counters, safe collapsed events, unavailable research and existing model-button selection.
- [x] Render research metadata from the actual batch response, preserve user-expanded process during polling and model selection where possible.
- [x] Run `node tests/check_future_trend.cjs` plus existing intraday/date/reliability VM suites.

## Task 4: Final review and runtime

- [x] Review changed research paths, source eligibility, sharing, call audit, request bounds and failures.
- [x] Run backend AI/future/trading-time suites and frontend checks; perform bounded live public search/page reads without paid model calls.
- [x] Restart backend and verify 8000 listener, auth/me 401 JSON, process start newer than backend edits. Report implemented behavior and the real-provider/browser acceptance boundary.

## Validation record

- Root full AI/future/trading-time/research suite: 123 tests passed.
- Market improvements regression: 7 tests passed via unittest discovery.
- Six frontend VM checks passed; whitespace diff check passed with CRLF-aware configuration.
- Review findings resolved: cumulative fallback budgets/events, dictionary errors, total async DNS/HTTP deadlines, official macro eligibility, legacy host filter.
- Public HTTP probe: Bing RSS gave no usable candidates; exact-query Eastmoney fallback returned 8 URLs with warning preserved. NBS body read succeeded; HTTP downgrade article rejected.
- No real paid model requests or authenticated browser acceptance.
- Backend restarted with stop.bat/start.bat; PID 45872, listener 8000 and auth/me 401 JSON verified.

