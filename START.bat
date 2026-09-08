@echo off
setlocal
set "PYTHONW=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%PYTHONW%" (
    echo The Python environment is missing: "%PYTHONW%"
    echo Keep the .venv folder beside this launcher.
    pause
    exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -c "import win32com.client" >nul 2>&1
if errorlevel 1 (
    echo Installing the playback dependency...
    "%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0requirements-podcast.txt"
    if errorlevel 1 (
        echo Playback controls need pywin32. You can still generate audio.
        pause
    )
)
start "" "%PYTHONW%" "%~dp0podcast_gui.py"
exit /b 0
