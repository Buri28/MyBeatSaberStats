#!/usr/bin/env pwsh
<#
.SYNOPSIS
    MyBeatSaberStats / MyBeatSaberRanking の配布用ビルドスクリプト。

.DESCRIPTION
    PyInstaller で --onedir ビルドを行い、配布フォルダ (release/) を生成する。
    ビルド後に version.json と resources/ を配布フォルダへコピーする。
    -Version を指定すると version.json を更新してからビルドする。

.PARAMETER Target
    ビルド対象を選択する。
      "stats"   → MyBeatSaberStats.exe のみ (デフォルト)
      "ranking" → MyBeatSaberRanking.exe のみ
      "all"     → 両方ビルド

.PARAMETER Version
    リリースバージョン (例: 1.0.1)。指定すると version.json を更新する。
    省略した場合は version.json の現在値をそのまま使う。

.PARAMETER Clean
    ビルド前に build/ dist/ __pycache__ を削除する。

.EXAMPLE
    .\.build.ps1                          # Stats 画面だけビルド (バージョン変更なし)
    .\.build.ps1 -Version 1.0.1           # version.json を 1.0.1 に更新してビルド
    .\.build.ps1 -Version 1.0.1 -Target all -Clean  # クリーンビルド
#>

param(
    [ValidateSet("stats", "ranking", "all")]
    [string]$Target = "stats",

    [string]$Version = "",

    [switch]$Clean
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python    = Join-Path $ScriptDir ".venv\Scripts\python.exe"
$ReleaseDir = Join-Path $ScriptDir "release"

# ─────────────────────────────────────────────
# 事前チェック
# ─────────────────────────────────────────────
if (-not (Test-Path $Python)) {
    Write-Error ".venv が見つかりません。先に venv を作成して requirements.txt をインストールしてください。"
    exit 1
}

Write-Host ""
Write-Host "===  MyBeatSaberStats Build  ===" -ForegroundColor Cyan
Write-Host "Target : $Target"
Write-Host "Python : $Python"
Write-Host "Release: $ReleaseDir"
Write-Host ""

Set-Location $ScriptDir

# ─────────────────────────────────────────────
# バージョン更新
# ─────────────────────────────────────────────
$VersionJsonPath = Join-Path $ScriptDir "version.json"
$currentVersion = (Get-Content $VersionJsonPath -Raw | ConvertFrom-Json).version

if ($Version -ne "") {
    # 先頭の "v" を除去して正規化
    $Version = $Version.TrimStart("v")
    Write-Host "[0/4] version.json を $currentVersion → $Version に更新中..." -ForegroundColor Yellow
    @{ version = $Version } | ConvertTo-Json | Set-Content $VersionJsonPath -Encoding UTF8
    Write-Host "  完了: version.json = $Version" -ForegroundColor Green
    $currentVersion = $Version
} else {
    Write-Host "[0/4] バージョン: $currentVersion (変更なし。-Version x.y.z で更新できます)" -ForegroundColor DarkGray
}
Write-Host ""

# ─────────────────────────────────────────────
# クリーン
# ─────────────────────────────────────────────
if ($Clean) {
    Write-Host "[1/4] クリーン中..." -ForegroundColor Yellow
    foreach ($dir in @("build", "dist", "release")) {
        if (Test-Path $dir) {
            Remove-Item $dir -Recurse -Force
            Write-Host "  削除: $dir"
        }
    }
    # __pycache__ を再帰削除
    Get-ChildItem -Recurse -Filter "__pycache__" -Directory | Remove-Item -Recurse -Force
} else {
    Write-Host "[1/4] クリーンスキップ (-Clean を付けるとクリーンビルド)"
}

# ─────────────────────────────────────────────
# ビルド関数
# ─────────────────────────────────────────────
function Build-Spec {
    param([string]$SpecFile, [string]$ExeName)

    Write-Host ""
    Write-Host "[BUILD] $SpecFile ..." -ForegroundColor Cyan

    & $Python -m PyInstaller $SpecFile --noconfirm

    if ($LASTEXITCODE -ne 0) {
        Write-Error "PyInstaller が失敗しました (exit code $LASTEXITCODE)"
        exit $LASTEXITCODE
    }

    # dist/<ExeName>/ → release/<ExeName>/
    $srcDir  = Join-Path $ScriptDir "dist\$ExeName"
    $destDir = Join-Path $ReleaseDir $ExeName

    if (Test-Path $destDir) {
        Remove-Item $destDir -Recurse -Force
    }

    New-Item $destDir -ItemType Directory -Force | Out-Null
    Copy-Item "$srcDir\*" $destDir -Recurse -Force

    Write-Host "  → $destDir" -ForegroundColor Green
}

# ─────────────────────────────────────────────
# PyInstaller 実行
# ─────────────────────────────────────────────
Write-Host ""
Write-Host "[2/4] PyInstaller ビルド中..." -ForegroundColor Yellow

if ($Target -eq "stats" -or $Target -eq "all") {
    Build-Spec "MyBeatSaberStats.spec" "MyBeatSaberStats"
}
if ($Target -eq "ranking" -or $Target -eq "all") {
    Build-Spec "MyBeatSaberRanking.spec" "MyBeatSaberRanking"
}

# ─────────────────────────────────────────────
# 共通ファイルのコピー (version.json)
# ─────────────────────────────────────────────
Write-Host ""
Write-Host "[3/4] 共通ファイルをコピー中..." -ForegroundColor Yellow

function Copy-CommonFiles {
    param([string]$ExeName)
    $internalDir = Join-Path $ReleaseDir "$ExeName\_internal"

    # version.json は spec の datas で既に _internal/ にコピーされているが、念のため上書き
    if (Test-Path $internalDir) {
        Copy-Item "$ScriptDir\version.json" "$internalDir\version.json" -Force
        Write-Host "  version.json → $internalDir"
    }
}

if ($Target -eq "stats" -or $Target -eq "all") {
    Copy-CommonFiles "MyBeatSaberStats"
}
if ($Target -eq "ranking" -or $Target -eq "all") {
    Copy-CommonFiles "MyBeatSaberRanking"
}

# ─────────────────────────────────────────────
# 完了メッセージ
# ─────────────────────────────────────────────
Write-Host ""
Write-Host "[4/4] ビルド完了！" -ForegroundColor Green
Write-Host ""

if ($Target -eq "stats" -or $Target -eq "all") {
    $path = Join-Path $ReleaseDir "MyBeatSaberStats"
    $size = (Get-ChildItem $path -Recurse -File | Measure-Object -Property Length -Sum).Sum
    Write-Host ("  MyBeatSaberStats  : {0:N0} MB" -f ($size / 1MB)) -ForegroundColor Cyan
    Write-Host "    フォルダ: $path"
}
if ($Target -eq "ranking" -or $Target -eq "all") {
    $path = Join-Path $ReleaseDir "MyBeatSaberRanking"
    $size = (Get-ChildItem $path -Recurse -File | Measure-Object -Property Length -Sum).Sum
    Write-Host ("  MyBeatSaberRanking: {0:N0} MB" -f ($size / 1MB)) -ForegroundColor Cyan
    Write-Host "    フォルダ: $path"
}

Write-Host ""
Write-Host "配布する際は release\ フォルダ内の各フォルダを ZIP 等で圧縮してください。" -ForegroundColor Yellow
Write-Host ""
Write-Host "GitHub Release 手順:" -ForegroundColor Cyan
Write-Host "  1. git add version.json && git commit -m \"Release v$currentVersion\""
Write-Host "  2. git tag v$currentVersion && git push origin main v$currentVersion"
Write-Host "  3. GitHub で v$currentVersion の Release を作成してリリースノートを記入"
Write-Host ""
