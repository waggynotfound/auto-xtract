# ===========================================================================
#  build_release.ps1 - build AutoXtract.exe and AutoXtract-<version>-Setup.exe
#
#  From the repository root:
#      powershell -ExecutionPolicy Bypass -File .\build_release.ps1
#
#  Produces, in dist\ (ready to attach to a GitHub release):
#      AutoXtract.exe                    portable single-file executable
#      AutoXtract-<version>-Setup.exe    installer (Inno Setup)
#      AutoXtract-<version>-win64.zip    portable zip
#      SHA256SUMS.txt
#
#  Needs: Python 3.9+ with PyInstaller, Inno Setup 6.3+ (ISCC.exe).
# ===========================================================================
[CmdletBinding()]
param(
    [string] $Version,      # defaults to the contents of VERSION
    [switch] $SkipExe,      # reuse an existing dist\AutoXtract.exe
    [switch] $KeepBuild,    # keep build\ (PyInstaller work dir) on success
    [switch] $NoZip         # skip the portable zip
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$root = $PSScriptRoot
Push-Location $root
try {
    function Step($msg) { Write-Host ""; Write-Host "==> $msg" -ForegroundColor Cyan }
    function Fail($msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

    function Find-Iscc {
        $cmd = Get-Command iscc.exe -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
        $candidates = @(
            "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
            "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
            "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
        )
        foreach ($c in $candidates) { if ($c -and (Test-Path $c)) { return $c } }
        return $null
    }

    # ---------------------------------------------------------------- version
    Step "Resolving version"
    if (-not $Version) { $Version = (Get-Content (Join-Path $root 'VERSION') -Raw).Trim() }
    $Version = $Version.Trim().TrimStart('v', 'V')
    if ($Version -notmatch '^\d+\.\d+\.\d+([.-][0-9A-Za-z.]+)?$') {
        Fail "Version '$Version' is not x.y.z (VERSION file or -Version)."
    }
    $fileVersion = (Get-Content (Join-Path $root 'VERSION') -Raw).Trim()
    if ($fileVersion -ne $Version) {
        Write-Host "    note: building $Version, VERSION file says $fileVersion"
    }
    $nums = @($Version -split '[.-]' | Where-Object { $_ -match '^\d+$' } | ForEach-Object { [int]$_ })
    while ($nums.Count -lt 4) { $nums += 0 }
    $v4 = "($($nums[0]), $($nums[1]), $($nums[2]), $($nums[3]))"
    $dist = Join-Path $root 'dist'
    $work = Join-Path $root 'build'
    New-Item -ItemType Directory -Force -Path $dist | Out-Null
    Write-Host "    version $Version"

    # ------------------------------------------------------------- toolchain
    Step "Checking the toolchain"
    $python = $null
    foreach ($cand in @('python', 'py')) {
        $c = Get-Command $cand -ErrorAction SilentlyContinue
        if ($c) { $python = $c.Source; break }
    }
    if (-not $python) { Fail "Python not found on PATH. Install Python 3.9+ from python.org." }
    & $python -c "import sys; print('    Python', sys.version.split()[0])"
    if ($LASTEXITCODE -ne 0) { Fail "Python at $python does not run." }

    if (-not $SkipExe) {
        & $python -m PyInstaller --version *> $null
        if ($LASTEXITCODE -ne 0) {
            Write-Host "    PyInstaller missing, installing it"
            & $python -m pip install --upgrade pyinstaller
            if ($LASTEXITCODE -ne 0) { Fail "Could not install PyInstaller: $python -m pip install pyinstaller" }
        }
        & $python -m PyInstaller --version | ForEach-Object { Write-Host "    PyInstaller $_" }
    }

    $iscc = Find-Iscc
    if (-not $iscc) {
        Fail "ISCC.exe not found. Install Inno Setup 6 from https://jrsoftware.org/isdl.php (winget install JRSoftware.InnoSetup), then run this again."
    }
    Write-Host "    $iscc"

    # ------------------------------------------------------------- clear old
    Step "Clearing previous output"
    $patterns = @('AutoXtract-*-Setup.exe', 'AutoXtract*.zip', 'SHA256SUMS.txt')
    if (-not $SkipExe) { $patterns += 'AutoXtract.exe' }
    foreach ($pattern in $patterns) {
        Get-ChildItem -Path $dist -Filter $pattern -File -ErrorAction SilentlyContinue |
            ForEach-Object { Remove-Item $_.FullName -Force; Write-Host "    removed dist\$($_.Name)" }
    }

    # ------------------------------------------------------------------ .exe
    $exe = Join-Path $dist 'AutoXtract.exe'
    if ($SkipExe) {
        if (-not (Test-Path $exe)) { Fail "-SkipExe given but dist\AutoXtract.exe does not exist." }
        Write-Host "    reusing existing dist\AutoXtract.exe"
    }
    else {
        Step "Building AutoXtract.exe (PyInstaller)"
        $tpl = Join-Path $root 'packaging\version_info.template.txt'
        $vi = Join-Path $work 'version_info.txt'
        New-Item -ItemType Directory -Force -Path $work | Out-Null
        (Get-Content $tpl -Raw).Replace('{{VERSION4}}', $v4).Replace('{{VERSION}}', $Version) |
            Set-Content -Path $vi -Encoding ASCII

        if (Test-Path (Join-Path $work 'pyinstaller')) { Remove-Item (Join-Path $work 'pyinstaller') -Recurse -Force }
        & $python -m PyInstaller --clean --noconfirm --onefile --noconsole `
            --name AutoXtract `
            --icon (Join-Path $root 'app_icon.ico') `
            --add-data "$root\app_icon.ico;." `
            --add-data "$root\assets\icon-48.png;assets" `
            --add-data "$root\VERSION;." `
            --version-file $vi `
            --distpath $dist `
            --workpath (Join-Path $work 'pyinstaller') `
            --specpath $work `
            (Join-Path $root 'auto_extract_gui.py')
        if ($LASTEXITCODE -ne 0) { Fail "PyInstaller failed." }
        if (-not (Test-Path $exe)) { Fail "PyInstaller reported success but dist\AutoXtract.exe is missing." }
    }
    $exeMb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
    Write-Host "    dist\AutoXtract.exe  $exeMb MB"

    # ------------------------------------------------------------- installer
    Step "Compiling the installer (Inno Setup)"
    $iss = Join-Path $root 'installer\setup.iss'
    & $iscc "/DAppVersion=$Version" "/O$dist" $iss
    if ($LASTEXITCODE -ne 0) { Fail "ISCC failed with exit code $LASTEXITCODE." }
    $setup = Join-Path $dist "AutoXtract-$Version-Setup.exe"
    if (-not (Test-Path $setup)) { Fail "Expected $setup, ISCC did not produce it." }
    $setupMb = [math]::Round((Get-Item $setup).Length / 1MB, 1)
    Write-Host "    dist\AutoXtract-$Version-Setup.exe  $setupMb MB"

    # ------------------------------------------------------------------- zip
    if (-not $NoZip) {
        Step "Packing the portable zip"
        $zip = Join-Path $dist "AutoXtract-$Version-win64.zip"
        Remove-Item $zip -Force -ErrorAction SilentlyContinue
        Compress-Archive -Path $exe, (Join-Path $root 'README.md'), (Join-Path $root 'CHANGELOG.md') `
            -DestinationPath $zip -CompressionLevel Optimal
        Write-Host "    dist\AutoXtract-$Version-win64.zip"
    }

    # --------------------------------------------------------------- hashes
    Step "Writing SHA256SUMS.txt"
    $sums = Join-Path $dist 'SHA256SUMS.txt'
    $lines = Get-ChildItem -Path $dist -File |
        Where-Object { $_.Extension -in @('.exe', '.zip') } |
        Sort-Object Name |
        ForEach-Object { "{0}  {1}" -f (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower(), $_.Name }
    $lines | Set-Content -Path $sums -Encoding ASCII
    $lines | ForEach-Object { Write-Host "    $_" }

    if (-not $KeepBuild) { Remove-Item (Join-Path $work 'pyinstaller') -Recurse -Force -ErrorAction SilentlyContinue }

    Step "Done"
    Write-Host "    dist\ is ready: upload these three files to a GitHub release tagged v$Version."
    exit 0
}
finally {
    Pop-Location
}
