# 生成 D3 用的语音媒体：Windows SAPI 合成中文语音 -> speech.wav，
# 再用 ffmpeg 合成一个带真实音轨的 MP4（页面里用 <video>，贴近直播页）。
#   powershell -ExecutionPolicy Bypass -File desktop/test/make_media.ps1
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$wav = Join-Path $here "speech.wav"
$mp4 = Join-Path $here "speech.mp4"

Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synth.SelectVoice("Microsoft Huihui Desktop")
$synth.Rate = 0
$synth.Volume = 100
$synth.SetOutputToWaveFile($wav)
$synth.Speak("大家好，欢迎来到今天的直播。我们正在测试实时字幕和多人语音记录。今天讨论三个话题：产品路线、采集方案和发布计划。")
$synth.Dispose()
Write-Host "wav: $wav"

& ffmpeg -y -hide_banner -loglevel error `
  -f lavfi -i "color=c=0x203040:s=640x360:r=15:d=30" `
  -i $wav -c:v libx264 -preset ultrafast -pix_fmt yuv420p `
  -c:a aac -b:a 96k -shortest $mp4
if ($LASTEXITCODE -ne 0) { throw "ffmpeg failed" }
Write-Host "mp4: $mp4"
