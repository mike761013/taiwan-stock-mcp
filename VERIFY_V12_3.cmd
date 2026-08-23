@echo off
setlocal
cd /d "%~dp0"
python scripts\verify_v12_3.py
if errorlevel 1 goto failed
echo.
echo V12.3 VERIFY PASSED
pause
exit /b 0
:failed
echo.
echo V12.3 VERIFY FAILED
pause
exit /b 1
