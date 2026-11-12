@echo off
rem Direct capture mode (Douyin only): reflow page + ffmpeg, no browser window.
rem Requires: ffmpeg in PATH (streamlink is optional fallback).
rem Fallback policy is in the app settings (default ASK: on failure the room
rem stops and the rooms page asks the user to confirm switching to a browser).
rem Do not set LIVENOTES_DIRECT_FALLBACK here: an env var would override the
rem settings page and make it look like the setting is ignored.
set LIVENOTES_CAPTURE_MODE=direct
call "%~dp0start.bat"
