@echo off
setlocal
cd /d "%~dp0"
set "FAILED=0"

call :check "stock_db\factors.py"
call :check "stock_db\schema.sql"

findstr /c:"NUMERIC(22,4)" "stock_db\factors.py" >nul 2>&1
if errorlevel 1 set "FAILED=1"
findstr /c:"VALUES (1232," "stock_db\schema.sql" >nul 2>&1
if errorlevel 1 set "FAILED=1"

echo.
if "%FAILED%"=="0" goto passed
echo V12.3.1 REVENUE HOTFIX VERIFY FAILED
pause
exit /b 1

:passed
echo V12.3.1 REVENUE HOTFIX VERIFY PASSED
echo You can commit and push these files.
pause
exit /b 0

:check
if not exist "%~1" (
  echo MISSING: %~1
  set "FAILED=1"
) else (
  echo OK: %~1
)
exit /b 0
