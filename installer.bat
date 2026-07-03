@echo off
setlocal

set "SCRIPT_DIR=%~dp0"

where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    py -3 "%SCRIPT_DIR%installer.py" %*
    exit /b %ERRORLEVEL%
)

where python >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    python "%SCRIPT_DIR%installer.py" %*
    exit /b %ERRORLEVEL%
)

where conda >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    for /f "delims=" %%I in ('where conda') do (
        set "CONDA_EXE=%%I"
        goto found_conda
    )
)

:found_conda
if defined CONDA_EXE (
    for %%I in ("%CONDA_EXE%") do set "CONDA_BIN_DIR=%%~dpI"
    if exist "%CONDA_BIN_DIR%python.exe" (
        "%CONDA_BIN_DIR%python.exe" "%SCRIPT_DIR%installer.py" %*
        exit /b %ERRORLEVEL%
    )
)

echo ERROR: Python was not found. Install Miniconda/Anaconda, then rerun this installer.
exit /b 1
