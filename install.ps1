param([switch]$Dev, [string]$Python = '')
$ErrorActionPreference = 'Stop'
try {
    $taskEnvironment = Join-Path $PSScriptRoot '.venv'
    $taskPython = Join-Path $taskEnvironment 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $taskPython)) {
        if (Test-Path -LiteralPath $taskEnvironment) { throw 'Existing .venv is not a Windows virtual environment.' }
        $taskPythonArguments = @()
        if ($Python) {
            $taskInterpreter = $Python
        } elseif (Get-Command py -ErrorAction SilentlyContinue) {
            $taskInterpreter = 'py'
            $taskPythonArguments = @('-3.12')
        } else {
            $taskInterpreter = (Get-Command python -ErrorAction Stop).Source
        }
        & $taskInterpreter @taskPythonArguments -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else "Python 3.12 or newer is required")'
        if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.12+ or pass -Python with its executable path.' }
        & $taskInterpreter @taskPythonArguments -m venv $taskEnvironment
        if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
    }
    & $taskPython -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else "Python 3.12 or newer is required")'
    if ($LASTEXITCODE -ne 0) { throw 'Recreate .venv with Python 3.12 or newer.' }
    $taskRequirements = if ($Dev) { 'requirements-dev.txt' } else { 'requirements.txt' }
    & $taskPython -m pip install --disable-pip-version-check -r (Join-Path $PSScriptRoot $taskRequirements)
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
    Write-Output 'Ready. Use the launcher to start SCI Scope.'
} catch {
    Write-Error $_
    exit 1
}
