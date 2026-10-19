# Start the Nifty options desk at http://localhost:8501
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Error "Missing .venv. From the project folder run: python -m venv .venv"
    exit 1
}

$Runner = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -match 'modules.desk_loop' })
$StartedRunner = $false
if ($Runner.Count -eq 0) {
    Start-Process -FilePath $Python -ArgumentList "-m","modules.desk_loop" -WorkingDirectory $Root -WindowStyle Hidden
    $StartedRunner = $true
    Write-Output "Trade loop started. The browser can be closed."
}

$Running = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -match 'streamlit run app.py' }
if ($Running) {
    Write-Output "The desk is already running. Open http://localhost:8501 when you want to look."
    exit 0
}

Write-Output "Starting the desk at http://localhost:8501"
Write-Output "Leave this window open on the instance. Close the browser whenever you want. Run stop.bat to shut it down."
try {
    & $Python -m streamlit run app.py --server.headless true --server.port 8501 --browser.gatherUsageStats false
}
finally {
    if ($StartedRunner) {
        Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
            Where-Object { $_.CommandLine -match 'modules.desk_loop' } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    }
}
