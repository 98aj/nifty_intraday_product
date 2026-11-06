# Stop the Nifty options desk started from THIS folder only. Other folders,
# other clones, and unrelated Python programs are left running.
$Root = Split-Path -Parent $PSScriptRoot
$RootPattern = [regex]::Escape($Root)
$Running = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -match $RootPattern -and ($_.CommandLine -match 'streamlit run app.py' -or $_.CommandLine -match 'modules.desk_loop') })

if ($Running.Count -eq 0) {
    Write-Output "The desk is not running."
    exit 0
}

foreach ($Process in $Running) {
    Stop-Process -Id $Process.ProcessId -Force -ErrorAction SilentlyContinue
    Write-Output "Stopped process $($Process.ProcessId)"
}
Write-Output "The desk is stopped."
