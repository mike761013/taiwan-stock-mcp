@echo off
setlocal
cd /d "%~dp0"
set "FAILED=0"

call :check "server_v10_tools.py"
call :check "v12_config.json"
call :check "stock_db\factors.py"
call :check "stock_db\v12.py"
call :check "stock_db\radar.py"
call :check "stock_db\performance.py"
call :check "stock_db\schema.sql"
call :check "stock_db\maintenance.py"
call :check "stock_db\pipeline.py"
call :check "stock_db\repository.py"
call :check "scripts\verify_v12_3_1.py"

findstr /c:"V12.3.1_SEVEN_FACTOR_FIX" "stock_db\v12.py" >nul 2>&1
if errorlevel 1 set "FAILED=1"
findstr /c:"VALUES (1231," "stock_db\schema.sql" >nul 2>&1
if errorlevel 1 set "FAILED=1"

echo.
if "%FAILED%"=="0" goto passed
echo V12.3.1 VERIFY FAILED
echo Copy the files inside the update folder to the repository root again.
pause
exit /b 1

:passed
echo V12.3.1 VERIFY PASSED
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
