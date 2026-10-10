Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$wrapper = Join-Path (Split-Path $PSScriptRoot -Parent) 'skills\senler-io-user\scripts\run-helper.ps1'
$beforeUtf8 = [Environment]::GetEnvironmentVariable('PYTHONUTF8', 'Process')
$beforeEncoding = $OutputEncoding
$global:HelperExit = 0
function global:Invoke-SenlerTestPython {
    $global:HelperArgs = @($args)
    $global:HelperInput = @($input) -join "`n"
    $global:HelperUtf8 = $env:PYTHONUTF8
    $global:LASTEXITCODE = $global:HelperExit
    '{"ok":true}'
}
function Assert-True([bool]$condition, [string]$message) {
    if (-not $condition) { throw $message }
}
try {
    $text = -join ([char[]]@(0x41F, 0x440, 0x438, 0x432, 0x435, 0x442))
    $json = @{ project_id = 'test'; instructions = $text; webhook_secret = 'test-only-secret' } | ConvertTo-Json
    $output = $json | & $wrapper configure -PythonPath 'Invoke-SenlerTestPython'
    Assert-True ($global:HelperInput.Trim() -eq $json.Trim()) 'JSON stdin was changed or lost.'
    Assert-True ($global:HelperArgs[1] -eq 'configure') 'Action was not forwarded.'
    Assert-True (-not (($global:HelperArgs -join ' ') -match 'test-only-secret')) 'A secret reached command arguments.'
    Assert-True ($global:HelperUtf8 -eq '1') 'Python UTF-8 was not enabled.'
    Assert-True ($output -eq '{"ok":true}') 'Unexpected helper output.'
    Assert-True ([Environment]::GetEnvironmentVariable('PYTHONUTF8', 'Process') -eq $beforeUtf8) 'Python environment was not restored.'
    Assert-True ($OutputEncoding -eq $beforeEncoding) 'PowerShell encoding was not restored.'
    $global:HelperExit = 2
    $failed = $false
    try { $null = & $wrapper preflight -PythonPath 'Invoke-SenlerTestPython' } catch { $failed = $true }
    Assert-True $failed 'Nonzero Python exit was ignored.'
    Write-Output 'PASS: JSON stdin, Unicode, private arguments, UTF-8 restoration and failure propagation.'
} finally {
    Remove-Item Function:\Invoke-SenlerTestPython -ErrorAction SilentlyContinue
    Remove-Variable HelperArgs, HelperInput, HelperUtf8, HelperExit -Scope Global -ErrorAction SilentlyContinue
}
