param([switch]$Archive)
$ErrorActionPreference = 'Stop'
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    $taskPython = (Get-Command python -ErrorAction Stop).Source
}
$taskBuildArguments = @()
if ($Archive) { $taskBuildArguments += '--archive' }
& $taskPython (Join-Path $PSScriptRoot 'tools\build.py') @taskBuildArguments
exit $LASTEXITCODE
