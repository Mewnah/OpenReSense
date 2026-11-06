# Builds the OpenReSense release for Windows x64, which runs without installing Python or Node.js:
#   dist\OpenReSense\                          the app folder (OpenReSense.exe starts it)
#   dist\OpenReSense-<version>-portable.zip    that folder, zipped
#   dist\OpenReSense-<version>-setup.exe       per-user installer (with -Installer; needs Inno Setup 6)
#   dist\SHA256SUMS.txt
# Needs the dev setup that run.bat creates (.venv with Python 3.12, node_modules) and internet the first time
# (Python's embeddable package from python.org and the wheels from PyPI; both are cached).
# Usage: powershell -ExecutionPolicy Bypass -File scripts\build_release.ps1 [-Version 1.2.3] [-Installer]
param([string]$Version, [switch]$Installer)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'  # Invoke-WebRequest is very slow with its progress bar on
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py) -or -not (Test-Path 'node_modules\cesium\Build\Cesium\Cesium.js')) {
    throw 'Run run.bat once first: it creates .venv and node_modules, which this build packs.'
}
if (-not $Version) { $Version = (Get-Content 'package.json' -Raw | ConvertFrom-Json).version }
$Version = $Version.TrimStart('v')
if ($Version -notmatch '^\d+(\.\d+){0,3}([-+][0-9A-Za-z.-]+)?$') { throw "Version '$Version' is not like 1.2.3" }
$pyver = (& $py -c "import platform; print(platform.python_version())").Trim()
$dist = Join-Path $root 'dist'
$out = Join-Path $dist 'OpenReSense'
$work = Join-Path $dist '_build'
New-Item -ItemType Directory -Force $work | Out-Null
foreach ($old in @($out, (Join-Path $dist 'SHA256SUMS.txt'))) {
    if (Test-Path $old) { Remove-Item -Recurse -Force $old }
}
Get-ChildItem $dist -File -Filter 'OpenReSense-*' | Remove-Item -Force

Write-Host "OpenReSense $Version with Python $pyver (embeddable package)..."
$embed = Join-Path $work "python-$pyver-embed-amd64.zip"
if (-not (Test-Path $embed)) {
    Invoke-WebRequest "https://www.python.org/ftp/python/$pyver/python-$pyver-embed-amd64.zip" -OutFile "$embed.tmp" -UseBasicParsing
    Move-Item -Force "$embed.tmp" $embed
}
$pydir = Join-Path $out 'python'
Expand-Archive $embed $pydir
$sig = Get-AuthenticodeSignature (Join-Path $pydir 'python.exe')
if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
    throw "python.exe in $embed is not signed by the Python Software Foundation ($($sig.Status)); delete it and retry."
}
# sys.path = the standard library, the packages and the app folder (one up). No "import site": packages installed
# elsewhere on the PC (user site-packages) can never mix in.
$pth = Get-ChildItem $pydir -Filter 'python3*._pth' | Select-Object -First 1
Set-Content $pth.FullName -Encoding Ascii -Value @("$($pth.BaseName).zip", '.', 'Lib\site-packages', '..')

Write-Host 'Packages (the versions of the tested .venv, without the test tools)...'
$runtime = Join-Path $work 'requirements-runtime.txt'
Get-Content requirements.txt | Where-Object { $_ -notmatch '^\s*pytest' } | Set-Content $runtime -Encoding Ascii
$pins = Join-Path $work 'constraints.txt'
& $py -m pip freeze --disable-pip-version-check | Set-Content $pins -Encoding Ascii
$site = Join-Path $pydir 'Lib\site-packages'
# --no-compile: compiled files would carry this PC's paths (user name); Python compiles them on the user's PC instead
& $py -m pip install --disable-pip-version-check --no-warn-script-location --only-binary=:all: --no-compile -q `
    --target $site -r $runtime -c $pins
if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }
Remove-Item -Recurse -Force (Join-Path $site 'bin') -ErrorAction SilentlyContinue  # console scripts bound to .venv

Write-Host 'App, web libraries and launcher...'
foreach ($d in 'server', 'web', 'catalog') { Copy-Item -Recurse $d (Join-Path $out $d) }
Get-ChildItem $out -Recurse -Directory -Filter '__pycache__' | Where-Object { $_.FullName -notlike "$pydir*" } |
    Remove-Item -Recurse -Force
New-Item -ItemType Directory -Force (Join-Path $out 'scripts') | Out-Null
Copy-Item 'scripts\validate_catalog.py' (Join-Path $out 'scripts')
Copy-Item 'README.md', 'LICENSE', 'THIRD-PARTY-NOTICES.txt' $out
# only what server/app.py mounts and the page loads, at the same relative paths
$vendor = @(
    'node_modules\ol\ol.css', 'node_modules\ol\dist\ol.js', 'node_modules\ol\LICENSE.md',
    'node_modules\proj4\dist\proj4.js', 'node_modules\proj4\LICENSE.md',
    'node_modules\cesium\LICENSE.md', 'node_modules\cesium\Build\Cesium'
)
foreach ($v in $vendor) {
    $dst = Join-Path $out $v
    New-Item -ItemType Directory -Force (Split-Path $dst -Parent) | Out-Null
    Copy-Item -Recurse $v $dst
}
# OpenReSense.exe: compiled with the C# compiler that ships with Windows; the version shows in its file properties
$numeric = ((($Version -split '[-+]')[0].Split('.')) + @('0', '0', '0', '0'))[0..3] -join '.'
$info = Join-Path $work 'AssemblyInfo.cs'
Set-Content $info -Encoding Ascii -Value @(
    '[assembly: System.Reflection.AssemblyTitle("OpenReSense")]',
    '[assembly: System.Reflection.AssemblyProduct("OpenReSense")]',
    '[assembly: System.Reflection.AssemblyCopyright("Copyright (c) 2026 Mewnah, MIT License")]',
    '[assembly: System.Reflection.AssemblyCompany("Mewnah")]',
    "[assembly: System.Reflection.AssemblyFileVersion(`"$numeric`")]",
    "[assembly: System.Reflection.AssemblyInformationalVersion(`"$Version`")]"
)
$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
& $csc /nologo /optimize+ /target:exe /platform:x64 '/win32icon:installer\OpenReSense.ico' `
    "/out:$(Join-Path $out 'OpenReSense.exe')" 'installer\launcher.cs' $info
if ($LASTEXITCODE -ne 0) { throw 'compiling the launcher failed' }
# Windows paths stop at 259 characters: the app folder's own path can be at most 258 - its deepest file's path inside it
$deepest = (Get-ChildItem $out -Recurse -File | ForEach-Object { $_.FullName.Length - $out.Length - 1 } | Measure-Object -Maximum).Maximum
$maxdir = 258 - $deepest
$maxparent = $maxdir - 'OpenReSense\'.Length
Set-Content (Join-Path $out 'README-portable.txt') -Encoding Ascii -Value @(
    "OpenReSense $Version for Windows 10/11 (64-bit). Nothing to install.",
    '',
    'Start:  double-click OpenReSense.exe. The app opens in your browser at http://127.0.0.1:8765/',
    '        and runs while its black window is open; close that window to stop it.',
    'Other port: OpenReSense.exe --port 9000. Starting it again while it runs just opens the running app.',
    '',
    'Your data (downloads, conversions, the dataset list, settings) is kept in the "cache" folder here.',
    'To update: replace everything except "cache" (and catalog\user, if you added sources) with the new version.',
    "Unzip it into a folder you can write to whose path has at most $maxparent characters, e.g. C:\ or Documents",
    '(not Program Files): from deeper folders Windows cannot create or load some of its files.',
    '',
    'Downloaded from the internet: if Windows says "Windows protected your PC", click "More info", then',
    '"Run anyway" (OpenReSense is not code-signed yet). The release page lists SHA256SUMS.txt to check the zip.',
    '',
    'README.md explains the app (its "Run from source" and "Making a release" parts are for the source code on the',
    'project page). Where it says .venv/Scripts/python.exe, use python\python.exe from this folder.',
    '',
    'OpenReSense is MIT-licensed (LICENSE). The Python, packages, native libraries and web libraries it contains',
    'keep their own licenses: see THIRD-PARTY-NOTICES.txt.'
)

Write-Host 'Checking that every package imports with the bundled Python...'
Push-Location $pydir  # a folder without a "server" package: the import must find the bundled one; -B: no compiled files
& (Join-Path $pydir 'python.exe') -B -c "import rasterio, numpy, pyproj, fastapi, uvicorn, httpx, PIL, pdr, astropy, pyhdf.SD, h5py, server.app, sys; assert server.app.__file__.startswith(sys.argv[1]), server.app.__file__; print('ok: GDAL', rasterio.__gdal_version__, 'PROJ', pyproj.proj_version_str)" $out
$ok = $LASTEXITCODE -eq 0
Pop-Location
if (-not $ok) { throw 'the bundled Python cannot import the app' }
# the check imported the app: drop what that wrote (compiled files with this build path, an empty cache)
Get-ChildItem $out -Recurse -Directory -Filter '__pycache__' | Where-Object { $_.FullName -notlike "$pydir*" } |
    Remove-Item -Recurse -Force
Remove-Item -Recurse -Force (Join-Path $out 'cache') -ErrorAction SilentlyContinue

Write-Host 'Zipping...'
$zip = Join-Path $dist "OpenReSense-$Version-portable.zip"
& $py -c "import shutil, sys; shutil.make_archive(sys.argv[1][:-4], 'zip', sys.argv[2], 'OpenReSense')" $zip $dist
if ($LASTEXITCODE -ne 0) { throw 'zip failed' }
$files = @($zip)

if ($Installer) {
    Write-Host 'Installer...'
    $iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
              "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $iscc) { $iscc = (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source }
    if (-not $iscc) { throw 'Inno Setup 6 (ISCC.exe) not found: install it from https://jrsoftware.org/isdl.php or build without -Installer.' }
    & $iscc /Q "/DAppVersion=$Version" "/DNumericVersion=$numeric" "/DSourceDir=$out" "/DOutputDir=$dist" `
        "/DMaxDirLength=$maxdir" 'installer\OpenReSense.iss'
    if ($LASTEXITCODE -ne 0) { throw 'building the installer failed' }
    $files += Join-Path $dist "OpenReSense-$Version-setup.exe"
}

$sums = foreach ($f in $files) { '{0}  {1}' -f (Get-FileHash $f -Algorithm SHA256).Hash.ToLower(), (Split-Path $f -Leaf) }
# LF line ends: "sha256sum -c" on Linux/macOS/Git Bash takes a CR as part of the file name
[IO.File]::WriteAllText((Join-Path $dist 'SHA256SUMS.txt'), (($sums -join "`n") + "`n"))
$mb = [math]::Round(((Get-ChildItem $out -Recurse -File | Measure-Object Length -Sum).Sum) / 1MB)
Write-Host "Done: $out ($mb MB; install folder at most $maxdir characters, zip parent folder at most $maxparent)"
foreach ($f in $files) { Write-Host ('  {0} ({1} MB)' -f (Split-Path $f -Leaf), [math]::Round((Get-Item $f).Length / 1MB)) }
