@echo off
rem 一键停止：三个 python 守护进程 + 采集用的独立 Chrome
rem 只认 python.exe，避免把正在运行这个脚本的终端一起杀掉
cd /d "%~dp0.."
echo 正在停止 ...

powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and $_.Name -like 'python*.exe' -and $_.CommandLine -match 'run\.py|feishu_bot|capture_agent' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and $_.Name -eq 'chrome.exe' -and $_.CommandLine -like '*data\profiles*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

timeout /t 2 >nul
netstat -ano | findstr /R /C:":8765 .*LISTENING" >nul
if errorlevel 1 goto clean
echo 警告 8765 还被占用
goto :eof
:clean
echo 已停止 8765 已释放
