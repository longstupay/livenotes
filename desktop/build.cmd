@echo off
setlocal
call "%~dp0env.cmd"
if errorlevel 1 exit /b 1
if "%CARGO_BUILD_JOBS%"=="" set "CARGO_BUILD_JOBS=2"
cd /d "%~dp0src-tauri"
cargo %*
