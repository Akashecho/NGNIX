<#
    Stops the backend and static frontend started by run.ps1.
    Ollama is left running because it is a shared background service.
#>
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*uvicorn*brain.app*' -or $_.CommandLine -like '*http.server*8080*' } |
    ForEach-Object {
        Write-Host "stopping PID $($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
Write-Host 'Done. Ollama was left running; stop it from the tray icon if you want it closed.'
