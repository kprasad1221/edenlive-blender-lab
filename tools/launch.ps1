param(
    [string]$Blender = "",
    [ValidateSet("Offline", "Paid")][string]$Mode = "Offline",
    [string]$Scene = ""
)
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path $PSScriptRoot -Parent
if (-not $Blender) {
    $found = Get-Command blender -ErrorAction SilentlyContinue
    if ($found) { $Blender = $found.Source }
    else {
        $installed = @(Get-ChildItem "$env:ProgramFiles\Blender Foundation\Blender*\blender.exe" -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending)
        if ($installed.Count -gt 0) { $Blender = $installed[0].FullName }
    }
}
if (-not $Blender -or -not (Test-Path -LiteralPath $Blender)) {
    throw 'Pass -Blender with the full path to your Blender 4.2+ blender.exe.'
}
$versionText = (& $Blender --version | Select-Object -First 1)
if ($versionText -notmatch 'Blender (\d+)\.(\d+)' -or [version]"$($Matches[1]).$($Matches[2])" -lt [version]'4.2') {
    throw "Blender 4.2 or newer is required. Found: $versionText"
}
if ($Scene -and -not (Test-Path -LiteralPath $Scene)) { throw 'Scene file not found.' }
$names = @('EDEN_LIVE_MOCK', 'EDEN_LAB_MODE', 'EDEN_LAB_HAS_SCENE', 'REACTOR_API_KEY')
$previous = @{}
foreach ($name in $names) { $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
try {
    $env:EDEN_LAB_MODE = $Mode.ToUpperInvariant()
    $env:EDEN_LAB_HAS_SCENE = if ($Scene) { '1' } else { '0' }
    $env:EDEN_LIVE_MOCK = if ($Mode -eq 'Offline') { '1' } else { '0' }
    if ($Mode -eq 'Offline') { Remove-Item Env:REACTOR_API_KEY -ErrorAction SilentlyContinue }
    $launchArgs = @('--factory-startup')
    if ($Mode -eq 'Offline') { $launchArgs += '--offline-mode' }
    if ($Scene) { $launchArgs += (Resolve-Path -LiteralPath $Scene).Path }
    $launchArgs += @('--python', (Join-Path $PSScriptRoot 'launch.py'))
    Write-Host "$versionText | EdenLive Lab $Mode mode. Opening Blender does not open a Reactor session."
    & $Blender @launchArgs
    if ($LASTEXITCODE -ne 0) { throw "Blender exited with code $LASTEXITCODE" }
}
finally {
    foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process') }
}
