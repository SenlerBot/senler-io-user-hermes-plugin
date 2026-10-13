[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$HermesHome,
    [string]$HermesCommand = 'hermes'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Invoke-HermesChecked {
    param([string[]]$CommandArguments)
    & $HermesCommand @CommandArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Hermes command failed (exit $LASTEXITCODE): $($CommandArguments -join ' ')"
    }
}

$packageRoot = $PSScriptRoot

if (-not $HermesHome) {
    if ($env:HERMES_HOME) {
        $HermesHome = $env:HERMES_HOME
    } elseif ($env:LOCALAPPDATA) {
        $HermesHome = Join-Path $env:LOCALAPPDATA 'hermes'
    } else {
        throw 'Pass -HermesHome with the directory containing your Hermes config.yaml.'
    }
}
$HermesHome = [IO.Path]::GetFullPath($HermesHome)
if (-not (Test-Path -LiteralPath (Join-Path $HermesHome 'config.yaml') -PathType Leaf)) {
    throw "Hermes config.yaml was not found in $HermesHome. Pass the correct directory with -HermesHome."
}
$null = Get-Command -Name $HermesCommand -ErrorAction Stop
$pluginsRoot = Join-Path $HermesHome 'plugins'
$destination = Join-Path $pluginsRoot 'senler-io-user'
if (Test-Path -LiteralPath $destination) {
    throw "Plugin directory already exists: $destination. Existing files were preserved. See README.md for updating."
}

$packageFiles = @('plugin.yaml', '__init__.py', 'senler_connection.py', 'senler_chat.py', 'senler_relay.py', 'senler_relay_store.py', 'senler_relay_setup.py', 'senler_relay_actions.py', 'senler_notifications.py', 'mcp-server.json', 'LICENSE', 'README.md', 'assets', 'skills', 'docs')
foreach ($relative in $packageFiles) {
    if (-not (Test-Path -LiteralPath (Join-Path $packageRoot $relative))) {
        throw "Incomplete package: $relative is missing. Extract the entire ZIP first."
    }
}

if (-not $PSCmdlet.ShouldProcess($destination, 'Validate, install and enable the Senler.io User plugin')) {
    return
}

$previousHermesHome = [Environment]::GetEnvironmentVariable('HERMES_HOME', 'Process')
$staging = Join-Path $HermesHome ('.senler-install-' + [Guid]::NewGuid().ToString('N'))
try {
    $env:HERMES_HOME = $HermesHome
    Invoke-HermesChecked -CommandArguments @('plugins', 'validate', $packageRoot)
    $null = New-Item -ItemType Directory -Path $staging
    foreach ($relative in $packageFiles) {
        Copy-Item -LiteralPath (Join-Path $packageRoot $relative) -Destination $staging -Recurse
    }
    $null = New-Item -ItemType Directory -Path $pluginsRoot -Force
    # Directory.Move refuses an existing destination, including a racing install.
    [IO.Directory]::Move($staging, $destination)
    try {
        Invoke-HermesChecked -CommandArguments @('plugins', 'enable', 'senler-io-user')
    } catch {
        throw "Files are installed at $destination, but enabling failed. Retry 'hermes plugins enable senler-io-user' in the same Hermes home. $($_.Exception.Message)"
    }
    Write-Host "Installed: $destination"
    Write-Host 'Next: open a new Hermes chat in this profile and ask: Connect Senler. Complete the native authorization card.'
    Write-Host 'Terminal alternative: hermes senler connect. The MCP address is bundled in the plugin.'
    Write-Host 'The installer did not configure an MCP connection or any webhook.'
} finally {
    [Environment]::SetEnvironmentVariable('HERMES_HOME', $previousHermesHome, 'Process')
    if (Test-Path -LiteralPath $staging) {
        Remove-Item -LiteralPath $staging -Recurse -Force
    }
}
