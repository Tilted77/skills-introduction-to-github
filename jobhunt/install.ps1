# Job Hunt installer for Windows.
#
# Open PowerShell (press the Windows key, type PowerShell, press Enter), paste this one line, press Enter:
#
#   irm https://raw.githubusercontent.com/Tilted77/skills-introduction-to-github/claude/happy-einstein-n3qja8/jobhunt/install.ps1 | iex
#
# It installs Python if needed, downloads the tool into a "jobhunt" folder in your user folder,
# and runs install.py, which does the rest. It is safe to run again.

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$repo = "Tilted77/skills-introduction-to-github"
$branch = "claude/happy-einstein-n3qja8"
$target = Join-Path $env:USERPROFILE "jobhunt"

function Find-Python {
    # Windows ships a placeholder "python" that only opens the Store, so check the version it reports.
    $candidates = @("python", "py")
    foreach ($version in "313", "312", "311") {
        $candidates += Join-Path $env:LOCALAPPDATA "Programs\Python\Python$version\python.exe"
        $candidates += "C:\Program Files\Python$version\python.exe"
    }
    foreach ($candidate in $candidates) {
        try {
            $reported = & $candidate --version 2>&1 | Out-String
            if ($reported -match "Python 3\.(1[1-9]|[2-9]\d)") { return $candidate }
        } catch { }
    }
    return $null
}

Write-Host ""
Write-Host "Step 1 of 3: Python"
$python = Find-Python
if (-not $python) {
    Write-Host "  Not installed yet. Installing it now; this can take a few minutes."
    try {
        winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
    } catch {
        Write-Host "  The automatic install did not work on this computer."
    }
    $python = Find-Python
}
if (-not $python) {
    Write-Host "  Please install Python from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'),"
    Write-Host "  then close this window, open PowerShell again and paste the same line."
    return
}
Write-Host "  Found."

Write-Host "Step 2 of 3: downloading Job Hunt"
$zip = Join-Path $env:TEMP "jobhunt-download.zip"
$unpacked = Join-Path $env:TEMP "jobhunt-download"
Invoke-WebRequest -UseBasicParsing "https://github.com/$repo/archive/refs/heads/$branch.zip" -OutFile $zip
if (Test-Path $unpacked) { Remove-Item $unpacked -Recurse -Force }
Expand-Archive $zip -DestinationPath $unpacked -Force
$installer = Get-ChildItem $unpacked -Recurse -Filter "install.py" | Select-Object -First 1
if (-not $installer) {
    Write-Host "  The download did not contain the tool. Copy this message into the chat for help."
    return
}
New-Item -ItemType Directory -Force $target | Out-Null
# Copies the program files over any older copy. Your own files in the folder are left alone,
# including the employer list if you have changed it.
$employers = Join-Path $target "companies.txt"
if (Test-Path $employers) { Remove-Item (Join-Path $installer.Directory.FullName "companies.txt") }
Copy-Item (Join-Path $installer.Directory.FullName "*") $target -Recurse -Force
Remove-Item $zip, $unpacked -Recurse -Force
Write-Host "  Saved in $target"

Write-Host "Step 3 of 3: setting up"
& $python (Join-Path $target "install.py")
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "Setup did not finish. Copy the messages above into the chat for help."
}
