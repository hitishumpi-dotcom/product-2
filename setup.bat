@echo off
echo ============================================
echo  L2Reborn Auto-Claimer - First-Time Setup
echo ============================================
echo.

:: Install Python dependencies
echo Installing Python packages...
pip install playwright requests
python -m playwright install chromium
echo.

echo Setup complete!
echo.
echo Run the script manually first to confirm it works:
echo   python l2reborn_autoclaim.py
echo.
echo Then set up Windows Task Scheduler using schedule_task.bat
pause
