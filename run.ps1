# C:\autocoin\run.ps1
# =============================================================
# Autocoin OS v3-H — Unified Launcher (PowerShell Compatible)
# =============================================================

param(
    [ValidateSet("DRY","LIVE","AUTO")]
    [string]$Mode = "AUTO"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $root

Write-Host "[Launcher] Autocoin OS v3-H" -ForegroundColor Cyan

# -------------------------------------------------------------
# Python PATH 자동 탐색 (설치되어 있으나 PATH 누락 시)
# -------------------------------------------------------------
$pythonOk = $false
try { python --version 2>&1 | Out-Null; if ($LASTEXITCODE -eq 0) { $pythonOk = $true } } catch {}

if (-not $pythonOk) {
    Write-Host "[python] PATH에 없음. 자동 탐색 중..." -ForegroundColor Yellow
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe",
        "C:\Python313\python.exe",
        "C:\Python312\python.exe",
        "C:\Python311\python.exe"
    )
    $found = $null
    foreach ($c in $candidates) {
        if (Test-Path $c) { $found = $c; break }
    }
    if (-not $found) {
        # glob 탐색
        $found = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue |
                 Sort-Object Name -Descending | Select-Object -First 1 -ExpandProperty FullName
    }
    if ($found) {
        $pyDir = Split-Path $found
        $env:Path = "$pyDir;$pyDir\Scripts;$env:Path"
        Write-Host "[python] 발견: $found — 세션 PATH 추가" -ForegroundColor Green
    } else {
        # Python 자동 다운로드 & 설치
        Write-Host "[python] Python이 없습니다. 자동 설치를 시작합니다..." -ForegroundColor Yellow
        # python.org에서 최신 3.13.x 버전 자동 탐색
        $pyVer = $null
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            $page = Invoke-WebRequest -Uri "https://www.python.org/downloads/" -UseBasicParsing
            $match = [regex]::Match($page.Content, 'Python (3\.13\.\d+)')
            if ($match.Success) { $pyVer = $match.Groups[1].Value }
        } catch {}
        if (-not $pyVer) { $pyVer = "3.13.12" }  # 폴백
        $installer = Join-Path $env:TEMP "python-$pyVer-amd64.exe"
        $url = "https://www.python.org/ftp/python/$pyVer/python-$pyVer-amd64.exe"
        Write-Host "[python] 다운로드 중: $url" -ForegroundColor Cyan
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -Uri $url -OutFile $installer -UseBasicParsing
        } catch {
            Write-Host "[python] 다운로드 실패. https://python.org 에서 수동 설치하세요." -ForegroundColor Red
            exit 1
        }
        Write-Host "[python] 설치 중 (silent)..." -ForegroundColor Cyan
        Start-Process -FilePath $installer -ArgumentList "/quiet","InstallAllUsers=1","PrependPath=1","Include_pip=1" -Wait -NoNewWindow
        Remove-Item $installer -ErrorAction SilentlyContinue
        # 설치 후 PATH 갱신
        $machPath = [Environment]::GetEnvironmentVariable("Path","Machine")
        $userPath = [Environment]::GetEnvironmentVariable("Path","User")
        $env:Path = "$machPath;$userPath"
        # 재확인
        try { python --version 2>&1 | Out-Null; $pythonOk = $true } catch {}
        if (-not $pythonOk) {
            Write-Host "[python] 설치 후에도 Python을 찾을 수 없습니다." -ForegroundColor Red
            exit 1
        }
        Write-Host "[python] Python $pyVer 설치 완료!" -ForegroundColor Green
    }
}

# -------------------------------------------------------------
# .env 로딩
# -------------------------------------------------------------
$dotenv = Join-Path $root ".env"
if (Test-Path $dotenv) {
    Write-Host "[.env] loading..." -ForegroundColor Cyan
    Get-Content $dotenv | ForEach-Object {
        if ($_ -match "^\s*#") { return }
        if ($_ -match "^\s*$") { return }
        $pair = $_ -split "=", 2
        if ($pair.Count -eq 2) {
            $k = $pair[0].Trim()
            $v = $pair[1].Trim()
            Set-Item -Path ("Env:{0}" -f $k) -Value $v
        }
    }
}

# -------------------------------------------------------------
# Proxy sanity guard
# - Some environments leak invalid local proxy (127.0.0.1:9)
# - This blocks Bybit API calls and stalls entry signals.
# -------------------------------------------------------------
$badProxyPattern = '^https?://127\.0\.0\.1:9/?$'
foreach ($k in @("HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","http_proxy","https_proxy","all_proxy")) {
    $item = Get-Item -Path ("Env:{0}" -f $k) -ErrorAction SilentlyContinue
    $v = if ($item) { $item.Value } else { $null }
    if ($v -and ($v -match $badProxyPattern)) {
        Write-Host ("[proxy] clearing invalid {0}={1}" -f $k, $v) -ForegroundColor Yellow
        Remove-Item -Path ("Env:{0}" -f $k) -ErrorAction SilentlyContinue
    }
}

# UTF-8 환경 정비
chcp 65001 > $null
$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONWARNINGS = "ignore::UserWarning"

# -------------------------------------------------------------
# venv 처리
# -------------------------------------------------------------
$venvPath = Join-Path $root "venv\Scripts\Activate.ps1"
$venvPython = Join-Path $root "venv\Scripts\python.exe"
$needRecreate = $false

if (Test-Path $venvPath) {
    # venv가 깨졌는지 확인 (python.exe 실행 가능 여부)
    if (Test-Path $venvPython) {
        try {
            & $venvPython --version *> $null
            if ($LASTEXITCODE -ne 0) { $needRecreate = $true }
        } catch { $needRecreate = $true }
    } else {
        $needRecreate = $true
    }

    if ($needRecreate) {
        Write-Host "[venv] 깨진 venv 감지 — 재생성합니다..." -ForegroundColor Yellow
        Remove-Item -Recurse -Force (Join-Path $root "venv")
        python -m venv venv
    }
    . $venvPath
} else {
    Write-Host "[venv] not found. Creating..." -ForegroundColor Yellow
    python -m venv venv
    . $venvPath
}

# -------------------------------------------------------------
# requirements 설치
# -------------------------------------------------------------
$req = Join-Path $root "requirements.txt"
if (Test-Path $req) {
    Write-Host "[pip] verifying requirements..." -ForegroundColor Cyan
    $installNeeded = $false
    foreach ($line in Get-Content $req) {
        if ($line -match "^\s*#") { continue }
        if ($line.Trim() -eq "") { continue }
        # Extract package name (handle version specifiers and markers)
        $pkg = ($line -split "[<>=!~;]")[0].Trim()
        try {
            python -m pip show $pkg 2>&1 | Out-Null
            if ($LASTEXITCODE -ne 0) { $installNeeded = $true; break }
        } catch {
            $installNeeded = $true; break
        }
    }
    if ($installNeeded) {
        Write-Host "[pip] installing requirements..." -ForegroundColor Yellow
        python -m pip install -r $req
    } else {
        Write-Host "[pip] all installed." -ForegroundColor DarkGray
    }
}

# -------------------------------------------------------------
# Mode (DRY / LIVE)
# -------------------------------------------------------------

# 1) CLI -Mode 우선
if ($PSBoundParameters.ContainsKey("Mode")) {
    if ($Mode -eq "LIVE") {
        $env:AUTOBOT_LIVE = "1"
    } elseif ($Mode -eq "DRY") {
        $env:AUTOBOT_LIVE = "0"
    }
    # AUTO면 .env 값 사용하도록 아래로 fall-through
}
else {
    # 2) .env 값 반영
    $norm = (($env:AUTOBOT_LIVE | Out-String).Trim().ToLower())
    if ($norm -in @("1","true","live")) {
        $env:AUTOBOT_LIVE = "1"
    } else {
        $env:AUTOBOT_LIVE = "0"
    }
}

# PowerShell은 삼항 연산자가 없으므로 if 문으로 처리
if ($env:AUTOBOT_LIVE -eq "1") {
    $currMode = "LIVE"
} else {
    $currMode = "DRY"
}

Write-Host "[MODE] $currMode (AUTOBOT_LIVE=$env:AUTOBOT_LIVE)" -ForegroundColor Green

# -------------------------------------------------------------
# 기존 uvicorn 종료 — 이전 인스턴스 강제 정리 후 시작
# -------------------------------------------------------------
$port = 8010
$already = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($already) {
    $ownerPid = $already.OwningProcess | Select-Object -First 1
    Write-Host "[kill] Port $port in use by PID $ownerPid — killing..." -ForegroundColor Yellow
}
Get-Process python -ErrorAction SilentlyContinue | ForEach-Object {
    try { Stop-Process -Id $_.Id -Force } catch {}
}
# 포트 해제 대기 (최대 10초)
for ($i = 0; $i -lt 10; $i++) {
    $still = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if (-not $still) { break }
    Write-Host "[wait] Port $port still held, waiting... ($($i+1)/10)" -ForegroundColor DarkGray
    Start-Sleep -Seconds 1
}

# -------------------------------------------------------------
# uvicorn 실행 (with auto-restart loop)
# -------------------------------------------------------------
$bindHost = "0.0.0.0"
$port = 8010

# uvicorn entrypoint
$entry = "app.main:app"

# Restart loop - exit code 42 means "restart requested"
$restartCode = 42

while ($true) {
    # ── Port collision guard: 이미 다른 인스턴스가 돌고 있으면 루프 중단 ──
    $portInUse = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($portInUse) {
        $ownerPid = $portInUse.OwningProcess | Select-Object -First 1
        Write-Host "[guard] Port $port already in use (PID $ownerPid). Another instance is running — stopping this loop." -ForegroundColor Red
        break
    }

    # Clean stale runtime state before boot
    $omaPath = Join-Path $PSScriptRoot "runtime\oma_state.json"
    if (Test-Path $omaPath) { [System.IO.File]::WriteAllText($omaPath, "{}") }

    Write-Host ("[exec] Starting server on {0}:{1} ..." -f $bindHost, $port) -ForegroundColor Cyan
    
    python -m uvicorn $entry --host $bindHost --port $port --log-level info `
        --timeout-keep-alive 30 `
        --backlog 2048
    $exitCode = $LASTEXITCODE
    
    if ($exitCode -eq $restartCode) {
        Write-Host "[restart] Server restart requested. Restarting in 2 seconds..." -ForegroundColor Yellow
        Start-Sleep -Seconds 2
        continue
    } elseif ($exitCode -eq 0) {
        Write-Host "[exit] Server stopped gracefully (code 0)" -ForegroundColor Magenta
        break
    } else {
        Write-Host "[crash] Server crashed with code $exitCode. Auto-restart in 5 seconds..." -ForegroundColor Red
        Start-Sleep -Seconds 5
        continue
    }
}
