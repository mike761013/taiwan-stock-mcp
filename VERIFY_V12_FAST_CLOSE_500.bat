@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    set "PYTHON_CMD=py -3"
) else (
    set "PYTHON_CMD=python"
)

echo Running V12 fast-close tests...
%PYTHON_CMD% scripts\verify_fast_close_500.py

if errorlevel 1 (
    echo.
    echo TESTS FAILED - 請先不要 Commit 或 Push。
    pause
    exit /b 1
)

echo.
echo TESTS PASSED - 可以回到 GitHub Desktop Commit 並 Push。
pause
exit /b 0
