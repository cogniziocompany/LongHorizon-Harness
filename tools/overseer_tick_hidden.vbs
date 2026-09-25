' Launch the overseer sweep tick with no console window (same pattern as overseer-ingest.vbs).
' Added 2026-09-16: the interactive scheduled task opened a Windows Terminal tab titled "claude" every 5 min.
Set sh = CreateObject("WScript.Shell")
sh.Run """C:\Program Files\PowerShell\7\pwsh.exe"" -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File C:\tmp\overseer_tick.ps1", 0, False
