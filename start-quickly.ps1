<#
.SYNOPSIS
    Starts Quickly locally: verifies the dedicated PostgreSQL cluster is up,
    starts the FastAPI backend (which also serves the built production
    frontend from frontend/dist), and ensures Tailscale Funnel is exposing
    only the Quickly backend on a stable https://*.ts.net URL.

.DESCRIPTION
    No secrets are embedded in this script. The backend reads DATABASE_URL,
    QUICKLY_SECRET_KEY, QUICKLY_ENCRYPTION_KEY, etc. from .env in the repo
    root via python-dotenv (app/settings_manager.py calls load_dotenv() at
    import time) - this script never reads, generates, or prints any of
    those values.

    PostgreSQL itself is never started/stopped here: the dedicated
    "postgresql-quickly" Windows service (port 5433) is expected to already
    be running (it starts automatically with Windows once registered). This
    script only checks it's reachable and fails fast with a clear message
    if not.

    Public HTTPS is provided by Tailscale Funnel in background/persistent
    mode, proxying only http://127.0.0.1:<BackendPort>. PostgreSQL, RDP,
    and other localhost ports are never exposed.

.PARAMETER SkipFunnel
    Start Postgres-check + backend only; do not ensure Tailscale Funnel.

.PARAMETER TailscaleExe
    Optional path to tailscale.exe. Defaults to the standard install path.
#>
[CmdletBinding()]
param(
    [switch]$SkipFunnel,
    [string]$TailscaleExe = "",
    [string]$BackendHost = "127.0.0.1",
    [int]$BackendPort = 8000,
    [int]$PostgresPort = 5433
)

$ErrorActionPreference = "Stop"
$repoRoot = $PSScriptRoot
Set-Location $repoRoot

$logsDir = Join-Path $repoRoot "logs"
if (-not (Test-Path $logsDir)) { New-Item -ItemType Directory -Path $logsDir | Out-Null }

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "    OK: $msg" -ForegroundColor Green }
function Write-Fail($msg) { Write-Host "    FAIL: $msg" -ForegroundColor Red }
function Write-Warn($msg) { Write-Host "    WARN: $msg" -ForegroundColor Yellow }

function Resolve-TailscaleExe {
    param([string]$Preferred)
    if ($Preferred -and (Test-Path $Preferred)) { return $Preferred }
    $candidates = @(
        "C:\Program Files\Tailscale\tailscale.exe",
        "C:\Program Files (x86)\Tailscale\tailscale.exe"
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { return $c }
    }
    $cmd = Get-Command tailscale -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

# ---------------------------------------------------------------------------
# 1. PostgreSQL health check (dedicated port-5433 cluster only - never
#    touches the pre-existing default instance on 5432).
# ---------------------------------------------------------------------------
Write-Step "Checking PostgreSQL (postgresql-quickly, port $PostgresPort)"

$svc = Get-Service -Name "postgresql-quickly" -ErrorAction SilentlyContinue
if (-not $svc -or $svc.Status -ne "Running") {
    Write-Fail "postgresql-quickly service is not running. Start it with: Start-Service postgresql-quickly (may require an elevated PowerShell)."
    exit 1
}
Write-Ok "postgresql-quickly service is Running"

$tcp = Test-NetConnection -ComputerName "localhost" -Port $PostgresPort -WarningAction SilentlyContinue
if (-not $tcp.TcpTestSucceeded) {
    Write-Fail "Port $PostgresPort is not accepting connections."
    exit 1
}
Write-Ok "Port $PostgresPort is accepting connections"

# ---------------------------------------------------------------------------
# 2. .env sanity check (presence only - never reads/prints values)
# ---------------------------------------------------------------------------
Write-Step "Checking .env"
$envPath = Join-Path $repoRoot ".env"
if (-not (Test-Path $envPath)) {
    Write-Fail ".env not found at $envPath"
    exit 1
}
$envKeys = (Get-Content $envPath) | Where-Object { $_ -match '^[A-Za-z0-9_]+=' } | ForEach-Object { ($_ -split '=', 2)[0] }
foreach ($required in @("DATABASE_URL", "QUICKLY_SECRET_KEY", "QUICKLY_ENCRYPTION_KEY", "BASE_URL", "CORS_ORIGINS")) {
    if ($envKeys -contains $required) {
        Write-Ok "$required is set"
    } else {
        Write-Fail "$required is missing from .env"
        exit 1
    }
}

# ---------------------------------------------------------------------------
# 3. Production frontend build check
# ---------------------------------------------------------------------------
Write-Step "Checking production frontend build"
$distIndex = Join-Path $repoRoot "frontend\dist\index.html"
if (-not (Test-Path $distIndex)) {
    Write-Fail "frontend/dist/index.html not found. Build it first: cd frontend; npm install; npm run build"
    exit 1
}
Write-Ok "frontend/dist is present (backend serves it directly - no separate static server needed)"

# ---------------------------------------------------------------------------
# 4. Start the backend (serves API + production frontend on one port)
# ---------------------------------------------------------------------------
Write-Step "Starting Quickly backend on http://${BackendHost}:$BackendPort"
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Fail "Virtualenv python not found at $venvPython"
    exit 1
}

# If something is already listening on the backend port, reuse it.
$existing = Get-NetTCPConnection -LocalPort $BackendPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
$backendProc = $null
if ($existing) {
    Write-Ok "Backend already listening on port $BackendPort (PID $($existing.OwningProcess))"
} else {
    $backendProc = Start-Process -FilePath $venvPython `
        -ArgumentList "-m", "uvicorn", "app.main:app", "--host", $BackendHost, "--port", $BackendPort `
        -RedirectStandardOutput (Join-Path $logsDir "uvicorn_stdout.log") `
        -RedirectStandardError (Join-Path $logsDir "uvicorn_stderr.log") `
        -PassThru -WindowStyle Hidden

    Start-Sleep -Seconds 4
    if ($backendProc.HasExited) {
        Write-Fail "Backend process exited immediately - check logs\uvicorn_stderr.log"
        exit 1
    }
    Write-Ok "Backend running, PID $($backendProc.Id)"
}

# ---------------------------------------------------------------------------
# 5. Tailscale Funnel (stable public HTTPS *.ts.net → localhost backend only)
# ---------------------------------------------------------------------------
$publicUrl = $null
if (-not $SkipFunnel) {
    Write-Step "Ensuring Tailscale Funnel for port $BackendPort"
    $ts = Resolve-TailscaleExe -Preferred $TailscaleExe
    if (-not $ts) {
        Write-Fail "tailscale.exe not found. Install Tailscale, sign in, enable Funnel on the tailnet, then re-run."
        exit 1
    }
    Write-Ok "Tailscale CLI: $ts"

    $statusOut = & $ts status 2>&1 | Out-String
    if ($statusOut -match 'Logged out|NeedsLogin') {
        Write-Fail "Tailscale is not signed in. Run: & `"$ts`" login"
        exit 1
    }
    Write-Ok "Tailscale is signed in"

    $funnelOut = & $ts funnel status 2>&1 | Out-String
    $already = ($funnelOut -match [regex]::Escape("127.0.0.1:$BackendPort")) -or ($funnelOut -match [regex]::Escape("localhost:$BackendPort")) -or ($funnelOut -match "proxy http://127\.0\.0\.1:$BackendPort")
    if (-not $already) {
        Write-Step "Starting Funnel in background (port $BackendPort only)"
        $enableOut = & $ts funnel --bg --yes $BackendPort 2>&1 | Out-String
        if ($enableOut -match 'Funnel is not enabled on your tailnet') {
            Write-Fail "Funnel is not enabled on your tailnet. Open the enable URL printed by: & `"$ts`" funnel --bg --yes $BackendPort"
            Write-Host $enableOut
            exit 1
        }
        Write-Ok "Funnel enable requested"
        $funnelOut = & $ts funnel status 2>&1 | Out-String
    } else {
        Write-Ok "Funnel already proxies port $BackendPort"
    }

    $m = [regex]::Match($funnelOut, 'https://[a-zA-Z0-9.-]+\.ts\.net')
    if ($m.Success) {
        $publicUrl = $m.Value
        Write-Ok "Public URL: $publicUrl"
    } else {
        # Fall back to MagicDNS name from status --json
        try {
            $j = & $ts status --json | ConvertFrom-Json
            $dns = ($j.Self.DNSName).TrimEnd('.')
            if ($dns) {
                $publicUrl = "https://$dns"
                Write-Ok "Public URL (from MagicDNS): $publicUrl"
            }
        } catch {
            Write-Warn "Could not resolve public *.ts.net hostname from funnel status"
        }
    }
} else {
    Write-Step "Skipping Tailscale Funnel (-SkipFunnel)"
}

# ---------------------------------------------------------------------------
# Summary (non-secret health only)
# ---------------------------------------------------------------------------
Write-Step "Summary"
if ($backendProc) { "Backend PID:    $($backendProc.Id)" }
"Local URL:      http://${BackendHost}:$BackendPort"
if ($publicUrl) { "Public URL:     $publicUrl" }
"Logs:           $logsDir"
"Funnel note:    background serve config persists with the Tailscale service; re-run this script after reboot if Funnel shows empty."
if ($backendProc) {
    "To stop backend: Stop-Process -Id $($backendProc.Id)"
}
