param(
    [Parameter(Mandatory = $true)][string]$ArchivePath,
    [Parameter(Mandatory = $true)][string]$Destination
)
$ErrorActionPreference = 'Stop'
$ExpectedSha256 = 'cd9de7c7cce1fbec95d521ab22b101abda6b86cd37907e8d5c131beca4855c94'
$ArchivePath = (Resolve-Path -LiteralPath $ArchivePath).Path
$actual = (Get-FileHash -LiteralPath $ArchivePath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actual -ne $ExpectedSha256) { throw "R4 archive SHA-256 mismatch: $actual" }
$zstdCommand = Get-Command zstd.exe -ErrorAction SilentlyContinue
if (-not $zstdCommand) { $zstdCommand = Get-Command zstd -ErrorAction SilentlyContinue }
if (-not $zstdCommand) { throw 'Install zstd.exe and add it to PATH before restoring this archive.' }
$tarCommand = Get-Command tar.exe -ErrorAction SilentlyContinue
if (-not $tarCommand) { throw 'tar.exe is required on PATH.' }
New-Item -ItemType Directory -Force -Path $Destination | Out-Null
$Destination = (Resolve-Path -LiteralPath $Destination).Path
$tarFile = Join-Path $Destination 'R4_formal_checkpoint_full_20261030T161453.tar'
& $zstdCommand.Source -d -f -o $tarFile $ArchivePath
if ($LASTEXITCODE -ne 0) { throw "zstd decompression failed with exit code $LASTEXITCODE" }
& $tarCommand.Source -xf $tarFile -C $Destination
if ($LASTEXITCODE -ne 0) { throw "tar extraction failed with exit code $LASTEXITCODE" }
Write-Host 'R4 checkpoint restored. No experiment was started.'
