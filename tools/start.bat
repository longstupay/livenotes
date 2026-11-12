@echo off
rem 一键启动：服务 -> 采集代理 -> 飞书机器人，各自一个窗口
rem 关掉哪个窗口 = 停掉哪个进程
cd /d "%~dp0.."

netstat -ano | findstr /R /C:":8765 .*LISTENING" >nul
if not errorlevel 1 goto running

echo 1/3 服务，这个窗口别关 ...
start "LiveNotes 服务" cmd /k python run.py --lan
timeout /t 4 >nul

echo 2/3 采集代理，贴链接后由它开浏览器 ...
start "LiveNotes 采集代理" cmd /k python tools\capture_agent.py --base http://127.0.0.1:8765
timeout /t 2 >nul

echo 3/3 飞书机器人，收链接、推高光 ...
start "LiveNotes 飞书机器人" cmd /k python tools\feishu_bot.py
timeout /t 4 >nul

start "" http://127.0.0.1:8765/rooms
echo.
echo 三个窗口都起来了，浏览器已打开收听页。
goto :eof

:running
echo 服务已经在跑了 8765 被占用，没有重复启动。
echo 要重启的话先运行 tools\stop.bat
start "" http://127.0.0.1:8765/rooms
timeout /t 3 >nul
