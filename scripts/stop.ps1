# Stop the Nifty options desk. Does not stop other Python programs.
$Running = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -match 'streamlit run app.py' -or $_.CommandLine -match 'modules.desk_loop' })

if ($Running.Count -eq 0) {
    Write-Output "The desk is not running."
    exit 0
}

foreach ($Process in $Running) {
    Stop-Process -Id $Process.ProcessId -Force -ErrorAction SilentlyContinue
    Write-Output "Stopped process $($Process.ProcessId)"
}
Write-Output "The desk is stopped."
