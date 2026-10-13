# Atomic installer checks using real PowerShell and a fake Hermes command.
# No connection to Senler, model call, or user's Hermes configuration is used.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$installer = Join-Path (Split-Path $PSScriptRoot -Parent) 'install.ps1'
$sandbox = Join-Path ([IO.Path]::GetTempPath()) ('senler plugin test ' + [Guid]::NewGuid().ToString('N'))
$originalHome = [Environment]::GetEnvironmentVariable('HERMES_HOME', 'Process')
$global:SenlerInstallerTestCalls = New-Object 'System.Collections.Generic.List[object]'
$global:SenlerInstallerTestFailure = ''

function global:Invoke-SenlerTestHermes {
    $global:SenlerInstallerTestCalls.Add(@{
        Arguments = @($args)
        HermesHome = $env:HERMES_HOME
    })
    if ($args[1] -eq $global:SenlerInstallerTestFailure) {
        $global:LASTEXITCODE = 7
    } else {
        $global:LASTEXITCODE = 0
    }
}

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

function New-TestHome {
    param([string]$Name)
    $root = Join-Path $sandbox $Name
    $null = New-Item -ItemType Directory -Path $root -Force
    [IO.File]::WriteAllText((Join-Path $root 'config.yaml'), "unrelated_setting: keep-me`n")
    return $root
}

function Assert-InstallFails {
    param([string]$TargetHome, [string]$ExpectedMessage)
    $caught = $null
    try {
        & $installer -HermesHome $TargetHome -HermesCommand Invoke-SenlerTestHermes
    } catch {
        $caught = $_.Exception.Message
    }
    Assert-True ($null -ne $caught) 'Expected installation to fail.'
    Assert-True ($caught.Contains($ExpectedMessage)) "Unexpected error: $caught"
}

try {
    $env:HERMES_HOME = 'senler-test-original-home'
    $targetHome = New-TestHome 'home with spaces'

    & $installer -HermesHome $targetHome -HermesCommand Invoke-SenlerTestHermes -WhatIf
    Assert-True (-not (Test-Path (Join-Path $targetHome 'plugins'))) 'WhatIf wrote plugin files.'
    Assert-True ($global:SenlerInstallerTestCalls.Count -eq 0) 'WhatIf invoked Hermes.'
    Assert-True ($env:HERMES_HOME -eq 'senler-test-original-home') 'WhatIf changed HERMES_HOME.'
    Write-Host 'PASS: WhatIf performs no mutation or Hermes call.'

    & $installer -HermesHome $targetHome -HermesCommand Invoke-SenlerTestHermes
    $installed = Join-Path $targetHome 'plugins\senler-io-user'
    foreach ($relative in @('plugin.yaml', '__init__.py', 'senler_connection.py', 'senler_chat.py', 'senler_relay_setup.py', 'senler_notifications.py', 'mcp-server.json')) {
        Assert-True (Test-Path (Join-Path $installed $relative) -PathType Leaf) "Native connection file missing: $relative"
    }
    $installedIcon = Join-Path $installed 'assets\icon.svg'
    Assert-True (Test-Path -LiteralPath $installedIcon -PathType Leaf) 'README logo missing from installed package.'
    $sourceIcon = Join-Path (Split-Path $PSScriptRoot -Parent) 'assets\icon.svg'
    Assert-True ((Get-FileHash -LiteralPath $installedIcon).Hash -eq (Get-FileHash -LiteralPath $sourceIcon).Hash) 'Installed logo differs from the source.'
    foreach ($skill in @('senler-io-user', 'senler-io-incoming')) {
        Assert-True (Test-Path (Join-Path $installed "skills\$skill\SKILL.md")) "Skill missing: $skill"
    }
    foreach ($module in @('senler_relay.py', 'senler_relay_store.py', 'senler_relay_setup.py')) {
        Assert-True (Test-Path (Join-Path $installed $module)) "Relay module missing: $module"
    }
    Assert-True ($global:SenlerInstallerTestCalls.Count -eq 2) 'Unexpected number of Hermes calls.'
    Assert-True ($global:SenlerInstallerTestCalls[0].Arguments[1] -eq 'validate') 'Must validate before enabling.'
    Assert-True ($global:SenlerInstallerTestCalls[1].Arguments[1] -eq 'enable') 'Plugin was not enabled.'
    foreach ($call in $global:SenlerInstallerTestCalls) {
        Assert-True ($call.HermesHome -eq $targetHome) 'Hermes used the wrong home.'
    }
    Assert-True ($env:HERMES_HOME -eq 'senler-test-original-home') 'HERMES_HOME was not restored.'
    Assert-True ([IO.File]::ReadAllText((Join-Path $targetHome 'config.yaml')) -eq "unrelated_setting: keep-me`n") 'Installer edited the config itself.'
    Assert-True (@(Get-ChildItem $targetHome -Filter '.senler-install-*' -Force).Count -eq 0) 'Staging directory was left behind.'
    Write-Host 'PASS: installation handles spaces, copies the logo and both skills, scopes commands and preserves config.'

    $marker = Join-Path $installed 'user-note.txt'
    [IO.File]::WriteAllText($marker, 'keep')
    $global:SenlerInstallerTestCalls.Clear()
    Assert-InstallFails $targetHome 'already exists'
    Assert-True ([IO.File]::ReadAllText($marker) -eq 'keep') 'Existing user file was changed.'
    Assert-True ($global:SenlerInstallerTestCalls.Count -eq 0) 'Existing installation should not invoke Hermes.'
    Write-Host 'PASS: existing plugin and user changes are preserved.'

    $invalidHome = New-TestHome 'validation fails'
    $global:SenlerInstallerTestFailure = 'validate'
    Assert-InstallFails $invalidHome 'exit 7'
    Assert-True (-not (Test-Path (Join-Path $invalidHome 'plugins\senler-io-user'))) 'Failed validation installed files.'
    Assert-True ($global:SenlerInstallerTestCalls.Count -eq 1) 'Validation failure did not stop installation.'
    Assert-True ($env:HERMES_HOME -eq 'senler-test-original-home') 'Validation failure leaked HERMES_HOME.'
    Write-Host 'PASS: failed validation stops before installation.'

    $enableHome = New-TestHome 'enable fails'
    $global:SenlerInstallerTestCalls.Clear()
    $global:SenlerInstallerTestFailure = 'enable'
    Assert-InstallFails $enableHome 'enabling failed'
    Assert-True (Test-Path (Join-Path $enableHome 'plugins\senler-io-user\plugin.yaml')) 'Enable failure removed the recoverable package.'
    Assert-True ($global:SenlerInstallerTestCalls.Count -eq 2) 'Unexpected retry after enable failure.'
    Assert-True ($env:HERMES_HOME -eq 'senler-test-original-home') 'Enable failure leaked HERMES_HOME.'
    Write-Host 'PASS: failed enable reports a recoverable installed package.'

    $global:SenlerInstallerTestCalls.Clear()
    Assert-InstallFails (Join-Path $sandbox 'not a Hermes home') 'config.yaml was not found'
    Assert-True ($global:SenlerInstallerTestCalls.Count -eq 0) 'Missing home invoked Hermes.'
    Write-Host 'PASS: wrong Hermes home is rejected before changes.'
} finally {
    [Environment]::SetEnvironmentVariable('HERMES_HOME', $originalHome, 'Process')
    Remove-Item Function:\Invoke-SenlerTestHermes -ErrorAction SilentlyContinue
    Remove-Variable SenlerInstallerTestCalls, SenlerInstallerTestFailure -Scope Global -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $sandbox) {
        Remove-Item -LiteralPath $sandbox -Recurse -Force
    }
}
