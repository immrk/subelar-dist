# Subelar CLI One-Line Installer & Updater (Windows PowerShell)
# Usage: irm https://raw.githubusercontent.com/immrk/subelar-dist/main/install.ps1 | iex
$ErrorActionPreference = "Stop"

$DistRepo = if ($env:SUBELAR_DIST_REPO) { $env:SUBELAR_DIST_REPO } else { "immrk/subelar-dist" }
$InstallDir = if ($env:SUBELAR_INSTALL_DIR) { $env:SUBELAR_INSTALL_DIR } else { Join-Path $HOME ".subelar\bin" }

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

$AssetName = "subelar-windows-amd64.exe"
$DownloadUrl = "https://github.com/$DistRepo/releases/latest/download/$AssetName"
$TargetBin = Join-Path $InstallDir "subelar.exe"

Write-Host "▶ Downloading Subelar CLI ($AssetName) from $DistRepo..." -ForegroundColor Cyan
Invoke-WebRequest -Uri $DownloadUrl -OutFile $TargetBin -UseBasicParsing

$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($UserPath -notlike "*$InstallDir*") {
    [Environment]::SetEnvironmentVariable("Path", "$InstallDir;$UserPath", "User")
    $env:Path = "$InstallDir;$env:Path"
}

Write-Host "✅ Subelar CLI installed to $TargetBin" -ForegroundColor Green
Write-Host "🚀 Run 'subelar init' or 'subelar doctor' to get started." -ForegroundColor Yellow
