$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Server = Join-Path $Root "server.py"
$Requirements = Join-Path $Root "requirements.txt"
$BackupDir = Join-Path $Root ".v10_backup"

if (-not (Test-Path $Server) -or -not (Test-Path $Requirements)) {
    Write-Host "ERROR: 請把整個 V10 安裝包內容複製到 taiwan-stock-mcp 根目錄後再執行。" -ForegroundColor Red
    exit 1
}

New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null

function Backup-File([string]$Path) {
    if (Test-Path $Path) {
        $Target = Join-Path $BackupDir ([IO.Path]::GetFileName($Path))
        if (-not (Test-Path $Target)) {
            Copy-Item $Path $Target
        }
    }
}

# 清除先前由 GitHub 網頁錯誤攤平到根目錄的 V10 草稿檔案。
$Obsolete = @(
    "REQUIREMENTS_ADD.txt",
    "UPLOAD_INSTRUCTIONS.txt",
    "__init__.py",
    "config.py",
    "connection.py",
    "indicators.py",
    "jobs.py",
    "repository.py",
    "schema.sql",
    "init_stock_database.py",
    "backfill_daily_bars.py",
    "update_daily_bars.py",
    "test_stock_db_config.py"
)

$Removed = @()
foreach ($Name in $Obsolete) {
    $Path = Join-Path $Root $Name
    if (Test-Path $Path -PathType Leaf) {
        Backup-File $Path
        Remove-Item $Path -Force
        $Removed += $Name
    }
}

if ($Removed.Count -gt 0) {
    Write-Host ("已清除舊版錯誤上傳檔案：" + ($Removed -join ", "))
}

# requirements.txt 加入 asyncpg。
$RequirementsText = Get-Content $Requirements -Raw -Encoding UTF8
if ($RequirementsText -notmatch "(?m)^\s*asyncpg") {
    Backup-File $Requirements
    $RequirementsText = $RequirementsText.TrimEnd() + "`r`nasyncpg>=0.29,<1`r`n"
    Set-Content -Path $Requirements -Value $RequirementsText -Encoding UTF8
    Write-Host "已在 requirements.txt 加入 asyncpg。"
} else {
    Write-Host "requirements.txt 已包含 asyncpg。"
}

# 在 server.py 的 main block 前註冊 V10 MCP 工具。
$Marker = "# === V10 PostgreSQL Tool Registration ==="
$ServerText = Get-Content $Server -Raw -Encoding UTF8

if ($ServerText -notmatch [regex]::Escape($Marker)) {
    $Needle = 'if __name__ == "__main__":'
    $Index = $ServerText.LastIndexOf($Needle)

    if ($Index -lt 0) {
        throw "找不到 server.py 的主程式區塊，未修改 server.py。"
    }

    Backup-File $Server

    $Block = @"

$Marker
try:
    from server_v10_tools import register_v10_tools
    register_v10_tools(mcp)
except Exception as exc:
    # PostgreSQL is optional and must never prevent the existing MCP from starting.
    print(f"V10 PostgreSQL tools were not registered: {type(exc).__name__}: {exc}")


"@

    $ServerText = $ServerText.Insert($Index, $Block)
    Set-Content -Path $Server -Value $ServerText -Encoding UTF8
    Write-Host "已在 server.py 註冊 V10 MCP 工具。"
} else {
    Write-Host "server.py 已包含 V10 工具註冊。"
}

# 建立範例環境變數檔，不含真實密碼。
$EnvExample = Join-Path $Root ".env.v10.example"
if (-not (Test-Path $EnvExample)) {
@'
# Never commit the real DATABASE_URL
DATABASE_URL=postgresql://USER:PASSWORD@INTERNAL_HOST/DATABASE
STOCK_DB_ENABLED=false
STOCK_DB_READ_PREFERRED=false
STOCK_DB_DAILY_UPDATE=false
STOCK_DB_FALLBACK_ENABLED=true
STOCK_DB_HISTORY_YEARS=3
STOCK_DB_POOL_MIN=1
STOCK_DB_POOL_MAX=3
STOCK_DB_STATEMENT_TIMEOUT_SECONDS=30
'@ | Set-Content -Path $EnvExample -Encoding UTF8
}

Write-Host ""
Write-Host "V10 installation completed." -ForegroundColor Green
Write-Host "原始檔案備份位置：$BackupDir"
Write-Host "下一步：回到 GitHub Desktop 檢查變更、Commit，再 Push origin。"
