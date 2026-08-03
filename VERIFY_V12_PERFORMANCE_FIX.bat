@echo off
setlocal
cd /d "%~dp0"

if not exist stock_db\performance.py goto failed
if not exist stock_db\maintenance.py goto failed
if not exist server_v10_tools.py goto failed
if not exist tests\test_performance_update_priority.py goto failed

findstr /C:"DEFAULT_PERFORMANCE_UPDATE_LIMIT = 5000" stock_db\performance.py >nul
if errorlevel 1 goto failed
findstr /C:"p.calculated_at ASC NULLS FIRST" stock_db\performance.py >nul
if errorlevel 1 goto failed
findstr /C:"limit=DEFAULT_PERFORMANCE_UPDATE_LIMIT" stock_db\maintenance.py >nul
if errorlevel 1 goto failed
findstr /C:"limit: int = DEFAULT_PERFORMANCE_UPDATE_LIMIT" server_v10_tools.py >nul
if errorlevel 1 goto failed

echo.
echo TEST PASSED
echo All V12 performance fix files and settings are present.
pause
exit /b 0

:failed
echo.
echo TEST FAILED
echo Please take a screenshot of this window and send it to ChatGPT.
pause
exit /b 1
