@echo off
setlocal
set "PATH=C:\Program Files (x86)\Microsoft Visual Studio\Installer;%PATH%"
call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat" >nul
if errorlevel 1 exit /b 1
set "RUSTUP_HOME=%~dp0..\..\toolchains\rustup"
set "CARGO_HOME=%~dp0..\..\toolchains\cargo"
set "PATH=%CARGO_HOME%\bin;%PATH%"
if "%CARGO_BUILD_JOBS%"=="" set "CARGO_BUILD_JOBS=2"
cd /d "%~dp0"
call npx tauri build
