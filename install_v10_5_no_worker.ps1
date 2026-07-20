$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Server = Join-Path $Root "server.py"
$Requirements = Join-Path $Root "requirements.txt"
$Gitignore = Join-Path $Root ".gitignore"

if (-not (Test-Path $Server) -or -not (Test-Path $Requirements)) {
    throw "請把 V10.5 No-Worker 套件全部複製到 taiwan-stock-mcp 根目錄。"
}

$RequirementsText = Get-Content $Requirements -Raw -Encoding UTF8
if ($RequirementsText -notmatch "(?m)^\s*asyncpg") {
    $RequirementsText = $RequirementsText.TrimEnd() + "`r`nasyncpg>=0.29,<1`r`n"
    Set-Content $Requirements $RequirementsText -Encoding UTF8
}

$ServerText = Get-Content $Server -Raw -Encoding UTF8
$Marker = "# === V10 PostgreSQL Tool Registration ==="
if ($ServerText -notmatch [regex]::Escape($Marker)) {
    $Needle = 'if __name__ == "__main__":'
    $Index = $ServerText.LastIndexOf($Needle)
    if ($Index -lt 0) {
        throw "找不到 server.py main block。"
    }
    $Block = @'
# === V10 PostgreSQL Tool Registration ===
try:
    from server_v10_tools import register_v10_tools
    register_v10_tools(mcp)
except Exception as exc:
    print(f"V10 PostgreSQL tools were not registered: {type(exc).__name__}: {exc}")


'@
    $ServerText = $ServerText.Insert($Index, $Block)
    Set-Content $Server $ServerText -Encoding UTF8
}

$Entries = @(
    ".v10_backup/",
    "__pycache__/",
    "*.pyc",
    ".env",
    ".env.*",
    "!.env.v10.example"
)
$Current = if (Test-Path $Gitignore) {
    Get-Content $Gitignore -Raw -Encoding UTF8
} else {
    ""
}
foreach ($Entry in $Entries) {
    if ($Current -notmatch "(?m)^" + [regex]::Escape($Entry) + "$") {
        $Current = $Current.TrimEnd() + "`r`n" + $Entry + "`r`n"
    }
}
Set-Content $Gitignore $Current.TrimStart() -Encoding UTF8

Write-Host ""
Write-Host "V10.5 No-Worker installation completed." -ForegroundColor Green
Write-Host "monitor_worker.py 未被修改。"
Write-Host "每日維護請使用 MCP 工具 run_v10_daily_maintenance，或執行 scripts/daily_maintenance.py。"
