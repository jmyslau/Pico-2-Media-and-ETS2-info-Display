# Installs scs-telemetry.dll (RenCloud scs-sdk-plugin) into Euro Truck Simulator 2
# - and American Truck Simulator if installed - by finding the games in all Steam
# library folders. Run it by double-clicking "Install plugin.bat".

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$dll = Join-Path $here 'scs-telemetry.dll'

if (-not (Test-Path $dll)) {
    Write-Host ''
    Write-Host 'scs-telemetry.dll is missing from this folder.' -ForegroundColor Red
    Write-Host 'Download scs-sdk-plugin V.1.12.1 from https://github.com/RenCloud/scs-sdk-plugin/releases'
    Write-Host 'and copy scs-telemetry.dll (from its Win64 folder) next to this script.'
    exit 1
}

# Find all Steam library folders
$libraries = @()
try {
    $steam = (Get-ItemProperty 'HKCU:\Software\Valve\Steam' -ErrorAction Stop).SteamPath -replace '/', '\'
    $libraries += $steam
    $vdf = Join-Path $steam 'steamapps\libraryfolders.vdf'
    if (Test-Path $vdf) {
        $text = Get-Content $vdf -Raw
        foreach ($m in [regex]::Matches($text, '"path"\s+"([^"]+)"')) {
            $libraries += ($m.Groups[1].Value -replace '\\\\', '\')
        }
    }
} catch {
    Write-Host 'Steam was not found in the registry.' -ForegroundColor Yellow
}

$games = @('Euro Truck Simulator 2', 'American Truck Simulator')
$installed = 0
foreach ($lib in ($libraries | Select-Object -Unique)) {
    foreach ($game in $games) {
        $bin = Join-Path $lib "steamapps\common\$game\bin\win_x64"
        if (Test-Path $bin) {
            $plugins = Join-Path $bin 'plugins'
            New-Item -ItemType Directory -Force -Path $plugins | Out-Null
            Copy-Item $dll $plugins -Force
            Unblock-File (Join-Path $plugins 'scs-telemetry.dll') -ErrorAction SilentlyContinue
            Write-Host "Installed for $game :" -ForegroundColor Green
            Write-Host "  $plugins"
            $installed++
        }
    }
}

Write-Host ''
if ($installed -eq 0) {
    Write-Host 'Could not find the game automatically.' -ForegroundColor Yellow
    Write-Host 'Copy scs-telemetry.dll by hand to:'
    Write-Host '  ...\steamapps\common\Euro Truck Simulator 2\bin\win_x64\plugins\'
    Write-Host '(In Steam: right-click the game > Manage > Browse local files.)'
} else {
    Write-Host 'Done. When the game starts, click OK on the "advanced SDK features" message.'
}
