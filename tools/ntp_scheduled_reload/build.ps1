param()

$ErrorActionPreference = "Stop"
$source = $PSScriptRoot
$distRoot = Join-Path $source "dist"
# Chromeに既に登録済みのフォルダー名を維持し、今後も同じ場所へ上書きします。
$packageName = "ntp_scheduled_reload_v1.0.1"
$packageDir = Join-Path $distRoot $packageName
$zipPath = Join-Path $distRoot "$packageName.zip"
$resolvedDistRoot = [IO.Path]::GetFullPath($distRoot).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
$resolvedPackageDir = [IO.Path]::GetFullPath($packageDir)

if (-not $resolvedPackageDir.StartsWith($resolvedDistRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Package output must stay inside the dist directory."
}

if (Test-Path -LiteralPath $packageDir) {
    Remove-Item -LiteralPath $packageDir -Recurse -Force
}
if (Test-Path -LiteralPath $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}

New-Item -ItemType Directory -Path $packageDir -Force | Out-Null

$runtimeFiles = @(
    "manifest.json",
    "background.js",
    "popup.html",
    "popup.css",
    "popup.js",
    "README.md"
)

foreach ($file in $runtimeFiles) {
    Copy-Item -LiteralPath (Join-Path $source $file) -Destination $packageDir
}

foreach ($directory in @("common", "content", "test")) {
    Copy-Item -LiteralPath (Join-Path $source $directory) -Destination $packageDir -Recurse
}

Compress-Archive -Path (Join-Path $packageDir "*") -DestinationPath $zipPath -CompressionLevel Optimal

$manifest = Get-Content -LiteralPath (Join-Path $packageDir "manifest.json") -Raw | ConvertFrom-Json

Write-Host "Unpacked: $packageDir"
Write-Host "ZIP:      $zipPath"
Write-Host "Version:  $($manifest.version)"
