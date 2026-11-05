@echo off
setlocal
where py >nul 2>nul
if not errorlevel 1 goto use_py
where python >nul 2>nul
if not errorlevel 1 goto use_python
echo Nexarl Forge requires Windows Python 3.10+ and Git. 1>&2
exit /b 1
:use_py
py -3 "%~dp0scripts\vibe_memory_cli.py" install --source-root "%~dp0." %*
exit /b %errorlevel%
:use_python
python "%~dp0scripts\vibe_memory_cli.py" install --source-root "%~dp0." %*
exit /b %errorlevel%
