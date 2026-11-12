# 开发态启动包装：Ctrl+C / 正常退出时把 `npx tauri dev` 整棵进程树收掉。
#
# 壳本身有 Job Object 兜底，即使这里没跑到，引擎也会跟着壳退出；这层主要
# 清掉 tauri dev 的 node/cargo 开发服务器，避免 1430 端口残留。

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$p = Start-Process -FilePath "npx.cmd" -ArgumentList @("tauri", "dev") -NoNewWindow -PassThru
try {
  Wait-Process -Id $p.Id
} finally {
  if (-not $p.HasExited) {
    taskkill /PID $p.Id /T /F 2>$null | Out-Null
  }
}
