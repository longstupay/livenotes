# 监听中心 spike 资源采样：每 N 秒记一次 chrome / python 的 CPU 与内存
#   .\tools\spike_measure.ps1 -Minutes 10 -Interval 5
param(
  [int]$Minutes = 10,
  [int]$Interval = 5,
  [string]$Out = "data\spike.csv"
)

$outPath = Join-Path (Get-Location) $Out
"ts,chrome_procs,chrome_mem_mb,chrome_cpu_s,python_procs,python_mem_mb" | Set-Content $outPath -Encoding utf8

$end = (Get-Date).AddMinutes($Minutes)
$t0 = Get-Date
$peakMem = 0
$n = 0

while ((Get-Date) -lt $end) {
  $c = @(Get-Process chrome -ErrorAction SilentlyContinue)
  $p = @(Get-Process python -ErrorAction SilentlyContinue)
  $cMem = [math]::Round((($c | Measure-Object WorkingSet64 -Sum).Sum) / 1MB, 1)
  $cCpu = [math]::Round((($c | Measure-Object CPU -Sum).Sum), 1)
  $pMem = [math]::Round((($p | Measure-Object WorkingSet64 -Sum).Sum) / 1MB, 1)
  if ($cMem -gt $peakMem) { $peakMem = $cMem }
  $row = "{0},{1},{2},{3},{4},{5}" -f (Get-Date -Format "HH:mm:ss"), $c.Count, $cMem, $cCpu, $p.Count, $pMem
  Add-Content -Path $outPath -Value $row -Encoding utf8
  Write-Host ("[{0}] chrome {1} 进程 {2} MB / CPU {3}s | python {4} 进程 {5} MB" -f (Get-Date -Format "HH:mm:ss"), $c.Count, $cMem, $cCpu, $p.Count, $pMem)
  $n++
  Start-Sleep -Seconds $Interval
}

$elapsed = [math]::Round(((Get-Date) - $t0).TotalSeconds, 0)
Write-Host ""
Write-Host "==== 汇总 ===="
Write-Host ("采样 $n 次，跨度 $elapsed 秒")
Write-Host ("chrome 内存峰值 : $peakMem MB")
$last = Get-Content $outPath -Tail 1
Write-Host ("最后一行       : $last")
Write-Host ("CSV            : $outPath")
