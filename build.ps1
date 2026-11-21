#!/usr/bin/env pwsh
<#
.SYNOPSIS
    MyBeatSaberStats の配布用ビルドスクリプト。

.DESCRIPTION
    PyInstaller で --onedir ビルドを行い、配布フォルダ (release/) を生成する。
    ビルド後に version.json と resources/ を配布フォルダへコピーする。
    -Version を指定すると version.json を更新してからビルドする。

.PARAMETER Version
    リリースバージョン (例: 1.0.1)。指定すると version.json を更新する。
    省略した場合は version.json の現在値をそのまま使う。

.PARAMETER Clean
    ビルド前に build/ dist/ __pycache__ を削除する。

.PARAMETER RankedSeedSourceDir
    同梱するランク譜面一覧（ranked_seed）のコピー元。省略すると cache\ を使う。

.EXAMPLE
    .\.build.ps1                          # ビルド (バージョン変更なし)
    .\.build.ps1 -Version 1.0.1           # version.json を 1.0.1 に更新してビルド
    .\.build.ps1 -Version 1.0.1 -Clean    # クリーンビルド
#>

param(
    [string]$Version = "",

    [switch]$Clean,

    # 同梱するランク譜面一覧のコピー元（既定は cache\。歯止めを試すときに一時フォルダを渡す）
    [string]$RankedSeedSourceDir = ""
)

$ErrorActionPreference = "Stop"
if ($PSVersionTable.PSVersion.Major -ge 7) {
    # PowerShell 7 の新しい引数エスケープ規則は csc.exe への手動クォート済み引数 (/win32icon:"...") を
    # 壊してしまうため、旧来の互換モードに戻す。
    $PSNativeCommandArgumentPassing = "Legacy"
}
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python    = Join-Path $ScriptDir ".venv\Scripts\python.exe"
$ReleaseDir = Join-Path $ScriptDir "release"
$UpdateTargetsPath = Join-Path $ScriptDir "resources\update_targets.json"

# 同梱するランク譜面一覧（ranked_seed）。全件取得を最後まで終えていない一覧を配らないよう、ビルドの前に確かめる。
$RankedSeedFiles = @(
    "scoresaber_ranked_maps.json",
    "beatleader_ranked_maps.json",
    "ranked_full_traversal.json",
    "scoresaber_reweights.json"
)
if ($RankedSeedSourceDir -eq "") {
    $RankedSeedSourceDir = Join-Path $ScriptDir "cache"
}
# 相対パスは呼び出した場所を基準に、ここで絶対パスにする（後で Set-Location $ScriptDir するため）
if (-not [System.IO.Path]::IsPathRooted($RankedSeedSourceDir)) {
    $RankedSeedSourceDir = Join-Path (Get-Location).Path $RankedSeedSourceDir
}
$RankedSeedSourceDir = [System.IO.Path]::GetFullPath($RankedSeedSourceDir)
# ビルド中（PyInstaller の数分）に cache が Snapshot で書き換わっても、一覧と記録がずれないよう、
# 確かめる前に一時フォルダへ写し、確かめたものをそのまま配布物に入れる。-Clean で消える build\ には置かない。
$RankedSeedStagingDir = Join-Path $env:TEMP "MyBeatSaberStats_ranked_seed_staging"

function Assert-RankedSeedSource {
    foreach ($file in $RankedSeedFiles) {
        if (-not (Test-Path (Join-Path $RankedSeedSourceDir $file))) {
            Write-Error "同梱するランク譜面の資材がありません: $RankedSeedSourceDir\$file（Snapshot でランク譜面一覧を最後まで取得してからビルドしてください）"
            exit 1
        }
    }
    if (Test-Path $RankedSeedStagingDir) {
        Remove-Item $RankedSeedStagingDir -Recurse -Force
    }
    New-Item $RankedSeedStagingDir -ItemType Directory -Force | Out-Null
    foreach ($file in $RankedSeedFiles) {
        Copy-Item (Join-Path $RankedSeedSourceDir $file) $RankedSeedStagingDir -Force
    }
    # 一覧が JSON として読めて fetched_at と中身があるか（読めない同梱分は、アプリ側で取り込まれず全件取得になる）
    $checkCode = 'import json,sys,pathlib; d=pathlib.Path(sys.argv[1]); ss=json.loads((d/''scoresaber_ranked_maps.json'').read_text(encoding=''utf-8'')); bl=json.loads((d/''beatleader_ranked_maps.json'').read_text(encoding=''utf-8'')); json.loads((d/''scoresaber_reweights.json'').read_text(encoding=''utf-8'')); ok=isinstance(ss.get(''fetched_at''),str) and isinstance(bl.get(''fetched_at''),str) and bool(ss.get(''leaderboards'')) and bool(bl.get(''pages'')); print(''SS'',len(ss.get(''leaderboards'') or {}),ss.get(''fetched_at''),''/ BL pages'',len(bl.get(''pages'') or []),bl.get(''fetched_at'')); sys.exit(0 if ok else 3)'
    & $Python -I -c $checkCode $RankedSeedStagingDir
    if ($LASTEXITCODE -ne 0) {
        Write-Error "同梱するランク譜面一覧を読めないか、fetched_at・中身がありません（exit code $LASTEXITCODE）"
        exit 1
    }
    $traversal = Get-Content (Join-Path $RankedSeedStagingDir "ranked_full_traversal.json") -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($service in @("scoresaber", "beatleader")) {
        if (-not $traversal."${service}_complete") {
            Write-Error "ranked_full_traversal.json に ${service}_complete がありません（全件取得を最後まで終えていない一覧は配りません）"
            exit 1
        }
        if ($traversal."${service}_last_ok" -eq $false) {
            Write-Error "ranked_full_traversal.json の ${service}_last_ok が false です（直前の全件取得が失敗しています。取り直してからビルドしてください）"
            exit 1
        }
        if ($traversal."${service}_stars_drift" -eq $true) {
            Write-Error "ranked_full_traversal.json の ${service}_stars_drift が true です（★のずれを直してからビルドしてください）"
            exit 1
        }
    }
    Write-Host "Ranked seed: $RankedSeedSourceDir（SS $($traversal.scoresaber_complete) / BL $($traversal.beatleader_complete)）"
}

function Get-CSharpCompiler {
    foreach ($candidate in @(
        "${env:WINDIR}\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
        "${env:WINDIR}\Microsoft.NET\Framework\v4.0.30319\csc.exe"
    )) {
        if (Test-Path $candidate) { return $candidate }
    }
    Write-Error "csc.exe が見つかりません。.NET Framework 4.x がインストールされているか確認してください。"
    exit 1
}

function Get-ManagedInternalRoots {
    if (-not (Test-Path $UpdateTargetsPath)) {
        Write-Error "update_targets.json が見つかりません: $UpdateTargetsPath"
        exit 1
    }

    $config = Get-Content $UpdateTargetsPath -Raw | ConvertFrom-Json
    $roots = @($config.internal_sync_dirs | Where-Object { $_ -and $_.ToString().Trim() -ne "" })
    if ($roots.Count -eq 0) {
        Write-Error "update_targets.json に internal_sync_dirs がありません。"
        exit 1
    }
    return $roots
}

function Get-ProcessesUnderPath {
    param([string]$RootPath)

    $normalizedRoot = [System.IO.Path]::GetFullPath($RootPath)
    if (-not $normalizedRoot.EndsWith("\")) {
        $normalizedRoot += "\"
    }

    $results = @()
    foreach ($process in Get-Process) {
        try {
            $processPath = $process.MainModule.FileName
        } catch {
            continue
        }
        if (-not $processPath) {
            continue
        }
        $normalizedProcessPath = [System.IO.Path]::GetFullPath($processPath)
        if ($normalizedProcessPath.StartsWith($normalizedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            $results += [PSCustomObject]@{
                Name = $process.ProcessName
                Id = $process.Id
                Path = $normalizedProcessPath
            }
        }
    }
    return $results
}

function Assert-ReleaseTargetNotRunning {
    param([string]$TargetDir)

    if (-not (Test-Path $TargetDir)) {
        return
    }

    $locked = @(Get-ProcessesUnderPath -RootPath $TargetDir)
    if ($locked.Count -eq 0) {
        return
    }

    $details = ($locked | ForEach-Object { "  $($_.Name) (PID=$($_.Id))  $($_.Path)" }) -join [Environment]::NewLine
    Write-Error (
        "release フォルダを使用中のプロセスがあるためビルドできません。`n" +
        "対象: $TargetDir`n" +
        "次のプロセスを終了してから再実行してください。`n" +
        $details
    )
    exit 1
}

# ─────────────────────────────────────────────
# 事前チェック
# ─────────────────────────────────────────────
if (-not (Test-Path $Python)) {
    Write-Error ".venv が見つかりません。先に venv を作成して requirements.txt をインストールしてください。"
    exit 1
}

Assert-RankedSeedSource

Write-Host ""
Write-Host "===  MyBeatSaberStats Build  ===" -ForegroundColor Cyan
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
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText(
        $VersionJsonPath,
        (@{ version = $Version } | ConvertTo-Json),
        $utf8NoBom
    )
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

    Assert-ReleaseTargetNotRunning $destDir

    if (Test-Path $destDir) {
        Remove-Item $destDir -Recurse -Force
    }

    New-Item $destDir -ItemType Directory -Force | Out-Null
    Copy-Item "$srcDir\*" $destDir -Recurse -Force

    Write-Host "  → $destDir" -ForegroundColor Green
}

function Build-CSharpWinExe {
    param(
        [string]$SourceFile,
        [string]$ExeName,
        [string[]]$References
    )

    Write-Host ""
    Write-Host "[BUILD] $SourceFile を csc.exe でビルド中..." -ForegroundColor Cyan

    $csc = Get-CSharpCompiler
    $outExe = Join-Path $ScriptDir "dist\$ExeName.exe"
    $iconIco = Join-Path $ScriptDir "resources\app_icon.ico"

    $cscArgs = @(
        "/nologo",
        "/target:winexe",
        "/out:`"$outExe`""
    )
    foreach ($reference in $References) {
        $cscArgs += "/reference:$reference"
    }
    if (Test-Path $iconIco) {
        $cscArgs = @("/win32icon:`"$iconIco`"") + $cscArgs
    }
    $cscArgs += "`"$SourceFile`""

    & $csc @cscArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Error "csc.exe が失敗しました (exit code $LASTEXITCODE)"
        exit $LASTEXITCODE
    }

    Write-Host "  → $outExe" -ForegroundColor Green
}

# ─────────────────────────────────────────────
# PyInstaller 実行
# ─────────────────────────────────────────────
Write-Host ""
Write-Host "[2/4] PyInstaller ビルド中..." -ForegroundColor Yellow

Build-Spec "MyBeatSaberStats.spec" "MyBeatSaberStats"
Build-CSharpWinExe `
    (Join-Path $ScriptDir "Update.cs") `
    "Update" `
    @(
        "System.Windows.Forms.dll",
        "System.Drawing.dll",
        "System.IO.Compression.dll",
        "System.IO.Compression.FileSystem.dll",
        "System.Web.Extensions.dll"
    )

# ─────────────────────────────────────────────
# 共通ファイルのコピー (version.json)
# ─────────────────────────────────────────────
Write-Host ""
Write-Host "[3/4] 共通ファイルをコピー中..." -ForegroundColor Yellow

function Copy-CommonFiles {
    param([string]$ExeName)
    $internalDir = Join-Path $ReleaseDir "$ExeName\_internal"
    $appDir = Join-Path $ReleaseDir $ExeName
    $updaterExe = Join-Path $ScriptDir "dist\Update.exe"

    # version.json は spec の datas で既に _internal/ にコピーされているが、念のため上書き
    if (Test-Path $internalDir) {
        Copy-Item "$ScriptDir\version.json" "$internalDir\version.json" -Force
        Write-Host "  version.json → $internalDir"
    }

    if (Test-Path $updaterExe) {
        Copy-Item $updaterExe (Join-Path $appDir "Update.exe") -Force
        Write-Host "  Update.exe → $appDir"
    }

    # サードパーティの著作権表示とライセンス文。PyInstaller が同梱したファイルから作る。
    # exe の隣（利用者が見る場所）と _internal\resources\ の2か所に置く。
    # 更新は zip 全体をミラーするので exe の隣のものも届く。
    # v0.5.0 の更新は _internal の lib / PySide6 / resources だけを同期するため、resources にも置く。
    # v0.3.1〜v0.4.1 の更新は GitHub のリポジトリから src と resources だけを取るため、
    # リポジトリの resources\ にも書き、リリースのときに一緒にコミットする。
    # v0.1.0〜v0.3.0 の更新は src の .py だけを取るので、最初の更新では届かない。
    # その更新で新しい updater.py に入れ替わり、次の更新（zip 全体のミラー）で届く。
    $noticeArgs = @(
        (Join-Path $ScriptDir "make_third_party_notices.py"),
        (Join-Path $ScriptDir "build\$ExeName"),
        (Join-Path $appDir "THIRD-PARTY-NOTICES.txt"),
        (Join-Path $internalDir "resources\THIRD-PARTY-NOTICES.txt"),
        (Join-Path $ScriptDir "resources\THIRD-PARTY-NOTICES.txt")
    )
    & $Python -I @noticeArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Error "THIRD-PARTY-NOTICES.txt を作れませんでした (exit code $LASTEXITCODE)"
        exit $LASTEXITCODE
    }

    # MyBeatSaberStats 自身のライセンス。通知と同じ理由で同じ3か所に置く。
    $licenseFile = Join-Path $ScriptDir "LICENSE"
    foreach ($dest in @(
        (Join-Path $appDir "LICENSE"),
        (Join-Path $internalDir "resources\LICENSE"),
        (Join-Path $ScriptDir "resources\LICENSE")
    )) {
        Copy-Item $licenseFile $dest -Force
        Write-Host "  LICENSE → $dest"
    }

    # ランク譜面一覧（SS・BL）は _internal\resources\ranked_seed\ に入れる（同梱分）。
    # _internal\resources はアップデートでもフル版でも入れ替わるので、アップデートした人にも届く。
    # 起動後の Snapshot で利用者の cache\ に取り込み（ranked_seed.py）、その後は差分取得だけにする。
    # 中身（_complete・★ずれの印・ファイルの有無）は、ビルドの前に Assert-RankedSeedSource で確かめてある。
    $seedDir = Join-Path $internalDir "resources\ranked_seed"
    New-Item $seedDir -ItemType Directory -Force | Out-Null
    foreach ($file in $RankedSeedFiles) {
        Copy-Item (Join-Path $RankedSeedStagingDir $file) $seedDir -Force
        Write-Host "  $file → $seedDir"
    }

    # ユーザーに紐づかないキャッシュファイルを release/<ExeName>/cache/ にコピーする
    # （プレイヤースコアや設定ファイルは除外。ランク譜面一覧は上の ranked_seed に入れるので、ここには入れない）
    $CacheSharedFiles = @(
        "players_index.json"
    )
    $srcCacheDir  = Join-Path $ScriptDir "cache"
    $destCacheDir = Join-Path $ReleaseDir "$ExeName\cache"

    if (Test-Path $srcCacheDir) {
        New-Item $destCacheDir -ItemType Directory -Force | Out-Null
        foreach ($file in $CacheSharedFiles) {
            $src = Join-Path $srcCacheDir $file
            if (Test-Path $src) {
                Copy-Item $src $destCacheDir -Force
                Write-Host "  cache\$file → $destCacheDir"
            }
        }
    }
}

Copy-CommonFiles "MyBeatSaberStats"

# ─────────────────────────────────────────────
# 完了メッセージ
# ─────────────────────────────────────────────
Write-Host ""
Write-Host "[4/4] ビルド完了！" -ForegroundColor Green
Write-Host ""

$path = Join-Path $ReleaseDir "MyBeatSaberStats"
$size = (Get-ChildItem $path -Recurse -File | Measure-Object -Property Length -Sum).Sum
Write-Host ("  MyBeatSaberStats  : {0:N0} MB" -f ($size / 1MB)) -ForegroundColor Cyan
Write-Host "    フォルダ: $path"

Write-Host ""
Write-Host "配布する際は release\ フォルダ内の各フォルダを ZIP 等で圧縮してください。" -ForegroundColor Yellow
Write-Host ""
Write-Host "GitHub Release 手順:" -ForegroundColor Cyan
Write-Host "  1. git add version.json resources/THIRD-PARTY-NOTICES.txt resources/LICENSE && git commit -m \"Release v$currentVersion\""
Write-Host "  2. git tag v$currentVersion && git push origin main v$currentVersion"
Write-Host "  3. GitHub で v$currentVersion の Release を作成してリリースノートを記入"
Write-Host ""
