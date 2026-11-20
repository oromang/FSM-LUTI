param([string]$Python = '3.14')
$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$taskEnvironment = Join-Path $taskRoot '.venv'
$taskPython = Join-Path $taskEnvironment 'Scripts\python.exe'
# Detect a source-only or unhydrated LFS checkout before installing dependencies.
$taskData = Join-Path $taskRoot 'data'
$taskManifest = Join-Path $taskData 'region/package_integrity.json'
if (-not (Test-Path -LiteralPath $taskManifest)) {
    throw 'Prepared inputs are missing. Use a complete Git checkout, then run git lfs install and git lfs pull.'
}
$taskSnapshot = Get-Content -LiteralPath $taskManifest -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($taskEntry in $taskSnapshot.files) {
    $taskInput = Join-Path $taskData $taskEntry.path
    if (-not (Test-Path -LiteralPath $taskInput -PathType Leaf) -or (Get-Item -LiteralPath $taskInput).Length -ne $taskEntry.bytes) {
        throw "Prepared input is missing or incomplete: $($taskEntry.path). Run git lfs install and git lfs pull; use run.ps1 --check to verify a complete checkout."
    }
}
$taskUv = Get-Command uv -ErrorAction Stop
if (-not (Test-Path -LiteralPath $taskPython)) {
    & $taskUv.Source venv --python $Python $taskEnvironment
    if ($LASTEXITCODE -ne 0) { throw 'Environment creation failed' }
}
& $taskUv.Source pip install --python $taskPython -r (Join-Path $taskRoot 'requirements.lock.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
& $taskUv.Source pip install --python $taskPython --no-deps --editable $taskRoot
if ($LASTEXITCODE -ne 0) { throw 'Package installation failed' }
