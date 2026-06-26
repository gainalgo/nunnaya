$pythonDir = "$env:LOCALAPPDATA\Programs\Python\Python313"
$scriptsDir = "$pythonDir\Scripts"
$currentPath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
if ($currentPath -notlike "*Python313*") {
    [Environment]::SetEnvironmentVariable('Path', "$pythonDir;$scriptsDir;$currentPath", 'Machine')
    Write-Host "PATH updated (Machine level)" -ForegroundColor Green
} else {
    Write-Host "Already in PATH" -ForegroundColor DarkGray
}
# Also set for current session
$env:Path = "$pythonDir;$scriptsDir;$env:Path"
python --version
