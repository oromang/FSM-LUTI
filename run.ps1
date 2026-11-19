$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    throw 'Create the local environment with setup.ps1 first.'
}
& $taskPython -X utf8 -u (Join-Path $taskRoot 'main.py') @args
exit $LASTEXITCODE
