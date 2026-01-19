param(
    [Parameter(Mandatory = $true)]
    [string]$SteamId,

    [Parameter(Mandatory = $false)]
    [string]$SnapshotDir
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# 配布版（exe）があればそれを優先し、無ければ開発用の Python+collect.py を使う
$snapshotExe = Join-Path $scriptDir "MyBeatSaberRankSnapshot.exe"

Write-Host "Creating snapshot for SteamID: $SteamId" -ForegroundColor Cyan
if ($SnapshotDir) {
    Write-Host "Snapshot directory: $SnapshotDir" -ForegroundColor Cyan
}

if (Test-Path $snapshotExe) {
    # PyInstaller でビルドした CUI exe を使用
    if ($SnapshotDir) {
        & $snapshotExe $SteamId --snapshot-dir $SnapshotDir
    } else {
        & $snapshotExe $SteamId
    }
} else {
    # 開発環境: venv を有効化して collect.py を直接実行
    $venvActivate = Join-Path $scriptDir ".venv\Scripts\Activate.ps1"
    if (Test-Path $venvActivate) {
        . $venvActivate
    }

    $python = "python"
    $collectPath = Join-Path $scriptDir "collect.py"

    if (-not (Test-Path $collectPath)) {
        Write-Error "collect.py not found: $collectPath"
        exit 1
    }

    if ($SnapshotDir) {
        & $python $collectPath $SteamId --snapshot-dir $SnapshotDir
    } else {
        & $python $collectPath $SteamId
    }
}

if ($LASTEXITCODE -ne 0) {
    Write-Error "Snapshot creation failed (exit code: $LASTEXITCODE)"
    exit $LASTEXITCODE
}

Write-Host "Snapshot created successfully" -ForegroundColor Green
