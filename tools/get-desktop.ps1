# Downloads the desktop app that CI built for a branch (default: the current git branch) from the
# rolling GitHub release desktop-<branch> and installs it into %USERPROFILE%\SyncVR, with a Start menu shortcut.
# Windows SmartScreen may warn the first time: More info > Run anyway.
# Usage: powershell -ExecutionPolicy Bypass -File tools\get-desktop.ps1 [branch] [-Launch]
param([string]$Branch = "", [switch]$Launch)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is much faster without the progress bar

$repo = "Trigger-EX/synchronized-vr-video-playback"
$dest = Join-Path $env:USERPROFILE "SyncVR"

if (-not $Branch) { try { $Branch = (git rev-parse --abbrev-ref HEAD 2>$null) } catch { $Branch = "" } }
if (-not $Branch -or $Branch -eq "HEAD") {
    Write-Error "could not tell which branch to fetch; pass it: get-desktop.ps1 <branch>"
}
$tag = "desktop-" + ($Branch -replace "/", "-")
$asset = "SyncVR-windows-x86_64.zip"
$url = "https://github.com/$repo/releases/download/$tag/$asset"

$tmp = Join-Path ([IO.Path]::GetTempPath()) ("syncvr-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
    Write-Host "Downloading $url"
    try { Invoke-WebRequest -Uri $url -OutFile (Join-Path $tmp $asset) }
    catch { Write-Error "no desktop build published for '$Branch' yet (CI publishes after the server and desktop jobs pass)" }

    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    # replace the old app, keep anything else (e.g. a 'portable' marker)
    Remove-Item -Recurse -Force (Join-Path $dest "SyncVR.exe"), (Join-Path $dest "_internal") -ErrorAction SilentlyContinue
    Expand-Archive -Path (Join-Path $tmp $asset) -DestinationPath $tmp -Force
    Copy-Item -Recurse -Force (Join-Path $tmp "SyncVR\*") $dest
} finally {
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}

$exe = Join-Path $dest "SyncVR.exe"
$lnk = Join-Path ([Environment]::GetFolderPath("Programs")) "SyncVR.lnk"
$sc = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
$sc.TargetPath = $exe
$sc.WorkingDirectory = $dest
$sc.Save()
Write-Host "Installed $exe (Start menu shortcut: $lnk)"
if ($Launch) { Start-Process $exe }
