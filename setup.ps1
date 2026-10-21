param([string]$PythonExe = 'python')
$ErrorActionPreference = 'Stop'
$taskProject = $PSScriptRoot
$taskPython = Join-Path $taskProject '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    & $PythonExe -m venv (Join-Path $taskProject '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create venv. Supply a Windows CPython 3.11+ executable via -PythonExe.' }
}
& $taskPython -m pip --isolated install --index-url https://pypi.org/simple --disable-pip-version-check -r (Join-Path $taskProject 'requirements.lock')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& $taskPython -m pip --isolated install --no-build-isolation --no-deps --disable-pip-version-check -e $taskProject
if ($LASTEXITCODE -ne 0) { throw 'Project installation failed.' }
Write-Output "Ready: $taskPython -m discourse_mcp --help"
