[CmdletBinding()]
param(
    [string]$Destination = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')),
    [string]$DownloadDirectory = (Join-Path $PSScriptRoot 'downloaded_delta_parts'),
    [string]$ZstdCommand = 'zstd.exe'
)

$ErrorActionPreference = 'Stop'
$repo = 'RoystonZheng/Royston_EI'
$tag = 'windows-migration-checkpoint-20261030'
$assetPattern = '^experiment-delta-20261030\.tar\.zst\.part-(\d{6})$'

if (-not (Get-Command 'curl.exe' -ErrorAction SilentlyContinue)) {
    throw 'curl.exe is required. Install a current Windows curl, then rerun this script.'
}
$zstd = Get-Command $ZstdCommand -ErrorAction SilentlyContinue
if (-not $zstd) {
    throw 'zstd.exe is required and must be on PATH. Install the Zstandard command-line tool, then rerun.'
}
if (-not (Get-Command 'tar.exe' -ErrorAction SilentlyContinue)) {
    throw 'tar.exe was not found. Use a current Windows version that includes tar.exe.'
}

$release = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/tags/$tag" -Headers @{'User-Agent' = 'Royston-EI-Windows-Migration'}
if ($release.draft) {
    throw 'The GitHub migration release is still a draft. Retry after it is published.'
}

$assets = @()
$page = 1
do {
    $pageAssets = @(Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/$($release.id)/assets?per_page=100&page=$page" -Headers @{'User-Agent' = 'Royston-EI-Windows-Migration'})
    $assets += $pageAssets
    $page++
} while ($pageAssets.Count -eq 100)

$parts = @($assets | Where-Object { $_.name -match $assetPattern } | Sort-Object { [int]([regex]::Match($_.name, $assetPattern).Groups[1].Value) })
if ($parts.Count -eq 0) {
    throw 'No full experiment archive parts were found in the published release.'
}
for ($i = 0; $i -lt $parts.Count; $i++) {
    $expectedName = 'experiment-delta-20261030.tar.zst.part-{0:D6}' -f ($i + 1)
    if ($parts[$i].name -ne $expectedName -or $parts[$i].state -ne 'uploaded' -or -not $parts[$i].digest) {
        throw "Release asset sequence or upload state is incomplete at $expectedName."
    }
}

$destinationPath = [IO.Path]::GetFullPath($Destination)
$downloadPath = [IO.Path]::GetFullPath($DownloadDirectory)
New-Item -ItemType Directory -Force -Path $destinationPath, $downloadPath | Out-Null

$archiveBytes = [long](($parts | Measure-Object -Property size -Sum).Sum)
$sourceBytes = [long]69414105294
$drive = [IO.DriveInfo]::new([IO.Path]::GetPathRoot($destinationPath))
$requiredFreeBytes = $sourceBytes + $archiveBytes + 2147483648
if ($drive.AvailableFreeSpace -lt $requiredFreeBytes) {
    throw ('Not enough free space on {0}. Need about {1:N1} GB for the expanded project and downloaded archive.' -f $drive.Name, ($requiredFreeBytes / 1GB))
}

Write-Host "Downloading and verifying $($parts.Count) archive parts..."
foreach ($asset in $parts) {
    $localPart = Join-Path $downloadPath $asset.name
    $expectedHash = ($asset.digest -replace '^sha256:', '').ToLowerInvariant()
    $validLocal = $false
    if (Test-Path -LiteralPath $localPart) {
        $item = Get-Item -LiteralPath $localPart
        if ($item.Length -eq [long]$asset.size) {
            $localHash = (Get-FileHash -LiteralPath $localPart -Algorithm SHA256).Hash.ToLowerInvariant()
            $validLocal = ($localHash -eq $expectedHash)
        }
    }
    if (-not $validLocal) {
        Remove-Item -LiteralPath $localPart -Force -ErrorAction SilentlyContinue
        Write-Host "Downloading $($asset.name)"
        & curl.exe --fail --location --retry 4 --retry-delay 2 --output $localPart $asset.browser_download_url
        if ($LASTEXITCODE -ne 0) {
            throw "Download failed for $($asset.name), curl exit code $LASTEXITCODE. Rerun to continue; verified parts are kept."
        }
        $item = Get-Item -LiteralPath $localPart
        if ($item.Length -ne [long]$asset.size) {
            throw "Downloaded size mismatch for $($asset.name)."
        }
        $localHash = (Get-FileHash -LiteralPath $localPart -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($localHash -ne $expectedHash) {
            throw "SHA-256 mismatch for $($asset.name)."
        }
    }
}

$archivePath = Join-Path $downloadPath 'experiment-delta-20261030.tar.zst'
Remove-Item -LiteralPath $archivePath -Force -ErrorAction SilentlyContinue
$output = [IO.File]::Open($archivePath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
try {
    foreach ($asset in $parts) {
        $input = [IO.File]::OpenRead((Join-Path $downloadPath $asset.name))
        try { $input.CopyTo($output) } finally { $input.Dispose() }
    }
    $output.Flush()
} finally {
    $output.Dispose()
}

Write-Host 'Checking the joined Zstandard archive...'
& $zstd.Source -t $archivePath
if ($LASTEXITCODE -ne 0) {
    throw "The joined archive failed zstd validation (exit code $LASTEXITCODE)."
}

$destinationPath = $destinationPath.TrimEnd('\')
$cmdPath = Join-Path $env:TEMP 'restore_royston_ei_windows.cmd'
$zstdExe = $zstd.Source.Replace('"', '""')
$archiveForCmd = $archivePath.Replace('"', '""')
$destinationForCmd = $destinationPath.Replace('"', '""')
$cmdText = "@echo off`r`n`"$zstdExe`" -d -c `"$archiveForCmd`" | tar.exe -xf - -C `"$destinationForCmd`"`r`nexit /b %ERRORLEVEL%`r`n"
Set-Content -LiteralPath $cmdPath -Value $cmdText -Encoding ASCII
Write-Host "Extracting into $destinationPath. This includes over 500,000 files and may take a while..."
& $cmdPath
if ($LASTEXITCODE -ne 0) {
    throw "Extraction failed with exit code $LASTEXITCODE. Keep the downloaded parts and inspect the Windows path-length setting."
}

Write-Host 'Restore completed. Start an offline run with START_WINDOWS.bat in the destination folder.'
