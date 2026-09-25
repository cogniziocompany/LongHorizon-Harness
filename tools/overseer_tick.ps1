# Overseer sweep tick - run by the Windows Scheduled Task "LH-Overseer-Sweep" every 5 minutes.
# Paxton 2026-09-14: "yes windows schedule task". Single-instance guard + log; never overlaps.
$ErrorActionPreference = 'Continue'
$log  = 'C:\tmp\overseer_tick.log'
$lock = 'C:\tmp\overseer_tick.lock'
$cwd  = 'C:\Users\PaxtonTait\source\LongHorizon-Harness'
$prompt = 'Run one overseer sweep tick now: read C:\tmp\queue\LOOP-PROMPT.md in full and execute it.'
$stamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')

# single-instance guard: a lock younger than 55 minutes means a tick is still running
if (Test-Path $lock) {
  $age = (Get-Date) - (Get-Item $lock).LastWriteTime
  if ($age.TotalMinutes -lt 55) { Add-Content $log "$stamp SKIP - previous tick still running (lock age $([int]$age.TotalMinutes) min)"; exit 0 }
  Add-Content $log "$stamp STALE LOCK removed (age $([int]$age.TotalMinutes) min)"; Remove-Item $lock -Force
}
New-Item -ItemType File -Path $lock -Force | Out-Null
try {
  $claude = (Get-Command claude -ErrorAction Stop).Source
  Add-Content $log "$stamp START pid=$PID claude=$claude"
  Set-Location $cwd
  $tickDir = 'C:\tmp\overseer_ticks'; New-Item -ItemType Directory -Path $tickDir -Force | Out-Null
  $tickFile = Join-Path $tickDir ((Get-Date).ToString('yyyyMMdd-HHmmss') + '.txt')
  # --dangerously-skip-permissions: the headless tick must be able to edit the ledger, resolve gates and
  # publish, exactly as the interactive overseer session does under bypass mode (Paxton 2026-09-14).
  $out = & $claude -p $prompt --output-format text --dangerously-skip-permissions 2>&1
  $out | Out-File -FilePath $tickFile -Encoding utf8
  $rc = $LASTEXITCODE
  $tail = ($out | Select-Object -Last 8) -join ' | '
  Add-Content $log "$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss')) END rc=$rc | $($tail.Substring(0, [Math]::Min(600, $tail.Length)))"
} catch {
  Add-Content $log "$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss')) ERROR $($_.Exception.Message)"
} finally {
  Remove-Item $lock -Force -ErrorAction SilentlyContinue
}
