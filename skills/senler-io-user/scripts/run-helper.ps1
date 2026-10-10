[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)][string]$Action,
    [string]$PythonPath,
    [string]$PublicBaseUrl,
    [switch]$EnableAutomation,
    [Parameter(ValueFromPipeline = $true)][string]$InputJson
)
begin { $jsonParts = New-Object 'System.Collections.Generic.List[string]' }
process { if ($null -ne $InputJson) { $jsonParts.Add($InputJson) } }
end {
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not $PythonPath) {
    $hermes = Get-Command hermes -ErrorAction Stop
    $candidate = Join-Path (Split-Path $hermes.Source -Parent) 'python.exe'
    if (Test-Path -LiteralPath $candidate -PathType Leaf) { $PythonPath = $candidate }
    else { throw 'Pass -PythonPath with python.exe from the Hermes virtual environment.' }
}
$arguments = @((Join-Path $PSScriptRoot 'senler_webhook.py'), $Action)
if ($PublicBaseUrl) { $arguments += @('--public-base-url', $PublicBaseUrl) }
if ($EnableAutomation) { $arguments += '--enable-automation' }
$previousEncoding = $OutputEncoding
$previousConsoleEncoding = [Console]::OutputEncoding
$previousPythonUtf8 = [Environment]::GetEnvironmentVariable('PYTHONUTF8', 'Process')
try {
    $env:PYTHONUTF8 = '1'
    $OutputEncoding = New-Object System.Text.UTF8Encoding $false
    [Console]::OutputEncoding = $OutputEncoding
    $payload = $jsonParts -join "`n"
    if ($payload) { $payload | & $PythonPath @arguments }
    else { & $PythonPath @arguments }
    if ($LASTEXITCODE -ne 0) { throw "Senler helper failed (exit $LASTEXITCODE)." }
} finally {
    $OutputEncoding = $previousEncoding
    [Console]::OutputEncoding = $previousConsoleEncoding
    [Environment]::SetEnvironmentVariable('PYTHONUTF8', $previousPythonUtf8, 'Process')
}
}
