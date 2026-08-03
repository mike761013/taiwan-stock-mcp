@echo off
setlocal
cd /d "%~dp0"

set "FAILED=0"
call :check "server_v10_tools.py"
call :check "stock_db\performance.py"
call :check "tests\test_weekly_performance_report.py"
call :check "README_V12_WEEKLY_REPORT.txt"

echo.
if "%FAILED%"=="0" (
  echo ALL V12 WEEKLY REPORT FILES ARE PRESENT.
) else (
  echo V12 WEEKLY REPORT FILE CHECK FAILED.
)
echo.
pause
exit /b %FAILED%

:check
if exist "%~1" (
  echo OK: %~1
) else (
  echo MISSING: %~1
  set "FAILED=1"
)
exit /b 0
