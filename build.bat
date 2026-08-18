@echo off
REM Build FilePicker into a standalone Windows .exe with Nuitka.
REM Requires: Python 3.10+, `pip install -r requirements.txt`, `pip install nuitka`.
python build.py
if errorlevel 1 (
    echo Build failed.
    exit /b 1
)
echo Done. Binary is in dist\.