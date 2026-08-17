@echo off
cd /d "%~dp0"
python scripts\verify_tdcc_distribution_fallback.py
set EXIT_CODE=%ERRORLEVEL%
echo.
if "%EXIT_CODE%"=="0" (
  echo TEST PASSED
) else (
  echo TEST FAILED - exit code %EXIT_CODE%
)
pause
exit /b %EXIT_CODE%
