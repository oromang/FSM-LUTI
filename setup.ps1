param([string]$Python = '3.14')
$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$taskEnvironment = Join-Path $taskRoot '.venv'
$taskPython = Join-Path $taskEnvironment 'Scripts\python.exe'
$taskUv = Get-Command uv -ErrorAction Stop
if (-not (Test-Path -LiteralPath $taskPython)) {
    & $taskUv.Source venv --python $Python $taskEnvironment
    if ($LASTEXITCODE -ne 0) { throw 'Environment creation failed' }
}
& $taskUv.Source pip install --python $taskPython -r (Join-Path $taskRoot 'requirements.lock.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
& $taskUv.Source pip install --python $taskPython --no-deps --editable $taskRoot
if ($LASTEXITCODE -ne 0) { throw 'Package installation failed' }
