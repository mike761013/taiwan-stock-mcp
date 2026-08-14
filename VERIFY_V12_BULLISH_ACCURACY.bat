@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if not errorlevel 1 (
    py -3 scripts\verify_v12_bullish_accuracy.py
    goto result
)

where python >nul 2>nul
if errorlevel 1 (
    echo ERROR: Python was not found.
    echo The source files can still be uploaded to GitHub.
    echo Ask ChatGPT to run validate_v12_release after deployment.
    goto failed
)

python scripts\verify_v12_bullish_accuracy.py

:result
if errorlevel 1 goto failed
echo.
echo TEST PASSED.
pause
exit /b 0

:failed
echo.
echo TEST FAILED. Take a screenshot and send it to ChatGPT.
pause
exit /b 1
