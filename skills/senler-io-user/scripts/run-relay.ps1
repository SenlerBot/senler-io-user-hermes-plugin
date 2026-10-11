[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('identity', 'configure', 'status', 'instructions', 'update-instructions', 'pause', 'resume', 'remove')][string]$Action,
    [string]$HermesCommand = 'hermes',
    [switch]$EnableAutomation,
    [Parameter(ValueFromPipeline = $true)][string]$InputJson
)
begin { $jsonParts = New-Object 'System.Collections.Generic.List[string]' }
process { if ($null -ne $InputJson) { $jsonParts.Add($InputJson) } }
end {
    Set-StrictMode -Version Latest
    $ErrorActionPreference = 'Stop'
    $null = Get-Command -Name $HermesCommand -ErrorAction Stop
    $arguments = @('senler', 'relay', $Action)
    if ($EnableAutomation) { $arguments += '--enable-automation' }
    $payload = $jsonParts -join "`n"
    if ($Action -notin @('identity', 'status') -and -not $payload) {
        throw 'Pass the relay JSON through stdin; never place credentials in command arguments.'
    }
    $previousEncoding = $OutputEncoding
    $previousConsoleEncoding = [Console]::OutputEncoding
    $previousPythonUtf8 = [Environment]::GetEnvironmentVariable('PYTHONUTF8', 'Process')
    try {
        $env:PYTHONUTF8 = '1'
        $OutputEncoding = New-Object System.Text.UTF8Encoding $false
        [Console]::OutputEncoding = $OutputEncoding
        if ($payload) { $payload | & $HermesCommand @arguments }
        else { & $HermesCommand @arguments }
        if ($LASTEXITCODE -ne 0) { throw "Senler relay helper failed (exit $LASTEXITCODE)." }
    } finally {
        $OutputEncoding = $previousEncoding
        [Console]::OutputEncoding = $previousConsoleEncoding
        [Environment]::SetEnvironmentVariable('PYTHONUTF8', $previousPythonUtf8, 'Process')
    }
}
