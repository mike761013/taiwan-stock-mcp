@echo off
chcp 65001 >nul
set FAIL=0
for %%F in (server_v10_tools.py v12_config.json README_V12_INSTALL.txt stock_db\v12.py stock_db\radar.py tests\test_v12_radar.py) do (
  if not exist "%%F" (
    echo MISSING: %%F
    set FAIL=1
  ) else (
    echo OK: %%F
  )
)
if "%FAIL%"=="0" (
  echo.
  echo All V12 patch files are present.
) else (
  echo.
  echo One or more files are missing. Re-extract the ZIP.
)
pause
