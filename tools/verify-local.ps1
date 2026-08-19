param(
    [Parameter(Mandatory = $true)]
    [string]$SourceArchive,
    [Parameter(Mandatory = $true)]
    [string]$GoExecutable,
    [string]$StateRoot = (Join-Path $env:TEMP 'sub2api-overdraft-fork-verify'),
    [string]$PythonLauncher = 'py',
    [ValidateSet('official', 'overdraft')]
    [string]$Channel = 'overdraft',
    [string]$Version = '0.1.178'
)

$ErrorActionPreference = 'Stop'
$pluginRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$archive = (Resolve-Path -LiteralPath $SourceArchive).Path
$go = (Resolve-Path -LiteralPath $GoExecutable).Path
$state = [IO.Path]::GetFullPath($StateRoot)
New-Item -ItemType Directory -Path $state -Force | Out-Null

$pnpmWrapper = Join-Path $state 'pnpm10.cmd'
@'
@echo off
npx --yes pnpm@10.28.2 %*
'@ | Set-Content -LiteralPath $pnpmWrapper -Encoding ascii

$env:SUB2API_STATE_ROOT = $state
$env:SUB2API_GO = $go
$env:SUB2API_PNPM = $pnpmWrapper
$env:SUB2API_FULL_FRONTEND_TESTS = '1'
$env:GOPROXY = 'https://goproxy.cn,direct'
$env:GOSUMDB = 'sum.golang.google.cn'

& $PythonLauncher -3.11 (Join-Path $pluginRoot 'manager.py') verify $Version --channel $Channel --source-archive $archive
if ($LASTEXITCODE -ne 0) {
    throw "Local replay verification failed with exit code $LASTEXITCODE"
}
