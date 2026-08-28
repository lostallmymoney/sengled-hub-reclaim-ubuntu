# Rebuilds Bank2 images from an existing backup without connecting to a hub.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$BackupDir,
    [Parameter(Mandatory=$true)][string]$BuildDir
)

$ErrorActionPreference = 'Stop'
$packageRoot = Split-Path -Parent $PSScriptRoot
$controller = Join-Path $packageRoot 'Reclaim-SengledHub.ps1'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $controller,
    [ref]$tokens,
    [ref]$parseErrors
)
if ($parseErrors.Count) { throw ($parseErrors | Out-String) }

# Load only the controller's function definitions; never execute its main flow.
$functions = $ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst]
}, $false)
foreach ($function in $functions) {
    . ([scriptblock]::Create($function.Extent.Text))
}

$ScriptRoot = $packageRoot
$Payload = Join-Path $packageRoot 'payload'
$Lib = Join-Path $packageRoot 'lib'
$Cache = Join-Path $packageRoot 'cache'
New-Item -ItemType Directory -Force -Path $Cache | Out-Null
Add-Type -Path (Join-Path $Lib 'ReclaimSupport.cs')

$resolvedBackup = (Resolve-Path -LiteralPath $BackupDir).Path
New-Item -ItemType Directory -Force -Path $BuildDir | Out-Null
$result = Build-ReclaimedImages $resolvedBackup $BuildDir

Write-Host ''
Write-Host 'LOCAL BUILD TEST: PASS' -ForegroundColor Green
Write-Host "Kernel: $($result.Kernel)"
Write-Host "Rootfs: $($result.Rootfs)"
