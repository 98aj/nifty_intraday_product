# Start the Nifty options desk at http://localhost:8501
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Error "Missing .venv. From the project folder run: python -m venv .venv"
    exit 1
}

$Running = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -match 'streamlit run app.py' }
if ($Running) {
    Write-Output "The desk is already running. Open http://localhost:8501"
    exit 0
}

Write-Output "Starting the desk at http://localhost:8501"
Write-Output "Leave this window open. Run stop.bat to shut it down."
& $Python -m streamlit run app.py --server.headless true --server.port 8501 --browser.gatherUsageStats false
