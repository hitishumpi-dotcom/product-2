@echo off
echo ============================================
echo  Setting up 12-hour Windows Task Scheduler
echo ============================================
echo.

:: Get the folder where this bat lives
set SCRIPT_DIR=%~dp0
set SCRIPT=%SCRIPT_DIR%l2reborn_autoclaim.py

:: Delete existing task if present
schtasks /delete /tn "L2Reborn AutoClaim" /f >nul 2>&1

:: Create task — runs every 12 hours starting now
schtasks /create ^
  /tn "L2Reborn AutoClaim" ^
  /tr "python \"%SCRIPT%\"" ^
  /sc HOURLY ^
  /mo 12 ^
  /st %TIME:~0,5% ^
  /ru "%USERNAME%" ^
  /rl HIGHEST ^
  /f

echo.
if %errorlevel% equ 0 (
    echo Task created successfully!
    echo The script will run every 12 hours starting now.
    echo You can view it in Task Scheduler under "L2Reborn AutoClaim".
) else (
    echo Failed to create task. Try running this .bat as Administrator.
)
echo.
pause
