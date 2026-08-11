@echo off
setlocal
cd /d "%~dp0"

if not exist "stock_db\performance.py" goto missing
findstr /C:"_V12_ONLY_STRATEGY_ALIASES" "stock_db\performance.py" >nul
if errorlevel 1 goto failed
findstr /C:"v12_reversal_reclaim" "stock_db\performance.py" >nul
if errorlevel 1 goto failed

echo PASS: V12 reversal performance alias fix is installed.
echo After Render deploy, query strategy=reversal_reclaim.
pause
exit /b 0

:missing
echo FAIL: stock_db\performance.py was not found.
echo Copy this package CONTENTS into the repository root first.
pause
exit /b 1

:failed
echo FAIL: the alias fix was not found in stock_db\performance.py.
pause
exit /b 1
