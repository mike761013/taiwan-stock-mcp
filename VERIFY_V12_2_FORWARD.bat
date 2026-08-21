@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo ERROR: Python was not found.
  echo You may still deploy through GitHub Desktop and validate on Render.
  pause
  exit /b 1
)

python -m unittest -v tests\test_v12_forward_persistence_unittest.py
if errorlevel 1 (
  echo.
  echo V12.2 TEST FAILED
  pause
  exit /b 1
)

echo.
echo V12.2 TEST PASSED
pause
exit /b 0
