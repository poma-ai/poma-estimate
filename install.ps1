# POMA estimate installer for Windows (PowerShell):
#   irm https://raw.githubusercontent.com/poma-ai/poma-estimate/main/install.ps1 | iex
$ErrorActionPreference = "Stop"
$base = if ($env:POMA_ESTIMATE_BASE) { $env:POMA_ESTIMATE_BASE } else { "https://github.com/poma-ai/poma-estimate/releases/latest/download" }
$name = "poma-estimate-windows.exe"
$dir = Join-Path $env:LOCALAPPDATA "poma-estimate"
New-Item -ItemType Directory -Force $dir | Out-Null
$tmp = Join-Path $dir ("dl-" + [guid]::NewGuid().ToString("N") + ".part")
try {
  Write-Host "Downloading $name ..."
  Invoke-WebRequest "$base/$name" -OutFile $tmp -UseBasicParsing
  $sums = (Invoke-WebRequest "$base/SHA256SUMS" -UseBasicParsing).Content
  $lines = @($sums -split "`r?`n" | Where-Object { ($_ -split "\s+")[1] -replace '^\*','' -eq $name })
  if ($lines.Count -ne 1) { throw "No valid checksum for $name. Aborting." }
  $want = ($lines[0] -split "\s+")[0].ToLower()
  if ($want -notmatch '^[0-9a-f]{64}$') { throw "No valid checksum for $name. Aborting." }
  $got = (Get-FileHash $tmp -Algorithm SHA256).Hash.ToLower()
  if ($want -ne $got) { throw "Download is corrupt (checksum mismatch). Nothing was installed." }
  $exe = Join-Path $dir "poma-estimate.exe"
  Move-Item -Force $tmp $exe
} finally {
  if (Test-Path $tmp) { Remove-Item -Force $tmp }
}
Unblock-File $exe
$folder = (Read-Host "Paste the folder path to scan (e.g. C:\Users\you\Documents), then press Enter").Trim('"')
& $exe $folder
