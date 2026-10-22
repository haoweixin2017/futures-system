@echo off
rem One-shot test runner: Python unittest suite + Node frontend check scripts.
rem Usage: run_tests.bat  (or: run_tests.bat -v)
setlocal
cd /d %~dp0

echo == Python unittest suite ==
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" %*
if errorlevel 1 (
  echo PYTHON TESTS FAILED
  exit /b 1
)

where node >nul 2>nul
if errorlevel 1 (
  echo [WARN] node not found in PATH, skip .cjs checks
  exit /b 0
)

for %%f in (tests\check_*.cjs) do (
  echo == node %%f ==
  node "%%f"
  if errorlevel 1 (
    echo NODE CHECK FAILED: %%f
    exit /b 1
  )
)

echo ALL TESTS PASSED
endlocal
