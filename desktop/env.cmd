@echo off
rem Shared environment probe. Called by build.cmd / build-release.cmd / run-dev.cmd.
rem Do not use setlocal here: its scope would end when this file returns.

set "PF=%ProgramFiles%"
set "PF86=%ProgramFiles(x86)%"
rem vcvars64.bat / VsDevCmd call vswhere by name; keep the Installer dir on PATH.
set "PATH=%PF86%\Microsoft Visual Studio\Installer;%PATH%"

rem ---------- MSVC ----------
where cl.exe >nul 2>nul
if not errorlevel 1 goto :rust

set "VSWHERE=%PF86%\Microsoft Visual Studio\Installer\vswhere.exe"
if not exist "%VSWHERE%" set "VSWHERE=%PF%\Microsoft Visual Studio\Installer\vswhere.exe"

set "VSPATH="
if exist "%VSWHERE%" (
  "%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath > "%TEMP%\livenotes-vswhere.txt" 2>nul
  set /p VSPATH=<"%TEMP%\livenotes-vswhere.txt"
  del "%TEMP%\livenotes-vswhere.txt" >nul 2>nul
)
if defined VSPATH (
  call "%VSPATH%\VC\Auxiliary\Build\vcvars64.bat" >nul
  goto :check_msvc
)

rem Fallback for machines without vswhere: check common editions.
set "VCVARS="
for %%e in (Community Professional Enterprise BuildTools) do (
  if not defined VCVARS if exist "%PF%\Microsoft Visual Studio\2022\%%e\VC\Auxiliary\Build\vcvars64.bat" set "VCVARS=%PF%\Microsoft Visual Studio\2022\%%e\VC\Auxiliary\Build\vcvars64.bat"
)
if not defined VCVARS if exist "%PF86%\Microsoft Visual Studio\2019\BuildTools\VC\Auxiliary\Build\vcvars64.bat" set "VCVARS=%PF86%\Microsoft Visual Studio\2019\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
if defined VCVARS call "%VCVARS%" >nul

:check_msvc
where cl.exe >nul 2>nul
if errorlevel 1 (
  echo [env] Visual Studio C++ toolchain not found.
  echo [env] Install VS 2022 Build Tools or Community with "Desktop development with C++".
  exit /b 1
)

:rust
rem ---------- Rust ----------
rem Precedence: existing CARGO_HOME/RUSTUP_HOME -> repo-parent toolchains -> rustup default.
if not defined CARGO_HOME (
  if exist "%~dp0..\..\toolchains\cargo" (
    set "CARGO_HOME=%~dp0..\..\toolchains\cargo"
  ) else (
    set "CARGO_HOME=%USERPROFILE%\.cargo"
  )
)
if not defined RUSTUP_HOME (
  if exist "%~dp0..\..\toolchains\rustup" (
    set "RUSTUP_HOME=%~dp0..\..\toolchains\rustup"
  ) else (
    set "RUSTUP_HOME=%USERPROFILE%\.rustup"
  )
)
set "PATH=%CARGO_HOME%\bin;%PATH%"

where cargo >nul 2>nul
if errorlevel 1 (
  echo [env] cargo not found.
  echo [env] Install Rust stable-msvc: https://rustup.rs
  echo [env] Current CARGO_HOME=%CARGO_HOME%
  exit /b 1
)
exit /b 0
