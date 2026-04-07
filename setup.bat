@echo off
echo ============================================
echo  L2Reborn Auto-Vote - First-Time Setup
echo ============================================
echo.

:: Check if Python is already installed
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo Python not found. Downloading Python installer...
    powershell -Command "Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.9/python-3.12.9-amd64.exe' -OutFile '%TEMP%\python_installer.exe'"
    echo Installing Python silently - this may take a minute, please wait...
    %TEMP%\python_installer.exe /quiet InstallAllUsers=1 PrependPath=1 Include_pip=1
    del "%TEMP%\python_installer.exe"
    echo Python installed!
    echo.
    set "PATH=%PATH%;C:\Program Files\Python312;C:\Program Files\Python312\Scripts;%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts"
) else (
    echo Python found. Skipping install.
)
echo.

:: Install Python dependencies
echo Installing Python packages...
pip install playwright requests
echo.

:: Install Chromium browser for Playwright
echo Installing Chromium browser...
python -m playwright install chromium
echo.

echo ============================================
echo  Setup complete!
echo ============================================
echo.
echo Next steps:
echo   1. Edit config.py and fill in your credentials
echo      (or use the GUI to set up accounts automatically)
echo.
echo   2. Launch the GUI:
echo         python app.py
echo.
echo   3. Click "+ Add" to add your first account
echo      The app will auto-discover your server and character.
echo.
echo   4. Click "Run Now" to vote, or "Schedule 12h" to automate.
echo.
pause
