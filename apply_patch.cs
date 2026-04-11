// MyBeatSaberStats パッチ適用プログラム (C# WinForms)
//
// 配布構成:
//   MyBeatSaberStats/
//   ├── MyBeatSaberStatsPlayer.exe
//   ├── apply_patch.exe        ← このプログラム（patch.zip を埋め込み可）
//   ├── _internal/             ← メインアプリの _internal（更新対象）
//   │   ├── lib/mybeatsaberstats/
//   │   └── version.json
//   └── patch/                 ← パッチ内容（同フォルダに展開しておく、任意）
//       ├── lib/
//       │   └── mybeatsaberstats/
//       │       ├── *.py
//       │       └── collector/*.py
//       ├── PySide6/
//       │   ├── QtSvg.pyd
//       │   └── Qt6Svg.dll
//       ├── resources/
//       │   └── *
//       └── version.json
//
// ビルド: .NET Framework 4.x の csc.exe で単一 EXE にコンパイル
//   csc.exe /nologo /target:winexe /out:apply_patch.exe
//           /reference:System.Windows.Forms.dll /reference:System.Drawing.dll
//           apply_patch.cs

using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Text.RegularExpressions;
using System.Windows.Forms;

static class Program
{
    [STAThread]
    static void Main()
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new PatchDialog());
    }
}

sealed class PatchDialog : Form
{
    private static readonly string[] DefaultManagedRoots = { "lib", "PySide6", "resources" };

    // UI パーツ
    private readonly Label        _infoLabel;
    private readonly Label        _progressLabel;
    private readonly ProgressBar  _progressBar;
    private readonly Button       _applyBtn;
    private readonly Button       _cancelBtn;

    // パス
    private readonly string _patchDir;
    private readonly string _internalDir;
    private readonly string _tempPatchDir;

    public PatchDialog()
    {
        string exe       = System.Reflection.Assembly.GetExecutingAssembly().Location;
        string patcherDir = Path.GetDirectoryName(exe);
        _internalDir = Path.Combine(patcherDir, "_internal");
        _patchDir    = ResolvePatchDir(patcherDir, out _tempPatchDir);

        // ---------- フォーム設定 ----------
        this.Text            = "MyBeatSaberStats パッチ適用";
        this.FormBorderStyle = FormBorderStyle.FixedDialog;
        this.MaximizeBox     = false;
        this.MinimizeBox     = false;
        this.StartPosition   = FormStartPosition.CenterScreen;
        this.ClientSize      = new System.Drawing.Size(460, 220);
        this.Padding         = new Padding(16);

        // ---------- コントロール ----------
        _infoLabel = new Label
        {
            AutoSize = false,
            Size     = new System.Drawing.Size(428, 90),
            Location = new System.Drawing.Point(16, 16),
        };

        _progressLabel = new Label
        {
            AutoSize = false,
            Size     = new System.Drawing.Size(428, 20),
            Location = new System.Drawing.Point(16, 112),
            Visible  = false,
        };

        _progressBar = new ProgressBar
        {
            Size     = new System.Drawing.Size(428, 18),
            Location = new System.Drawing.Point(16, 138),
            Style    = ProgressBarStyle.Marquee,
            Visible  = false,
        };

        _cancelBtn = new Button
        {
            Text     = "キャンセル",
            Size     = new System.Drawing.Size(88, 30),
            Location = new System.Drawing.Point(252, 172),
        };

        _applyBtn = new Button
        {
            Text     = "適用する",
            Size     = new System.Drawing.Size(88, 30),
            Location = new System.Drawing.Point(356, 172),
        };

        _applyBtn.Click  += OnApply;
        _cancelBtn.Click += (s, e) => Application.Exit();

        this.AcceptButton = _applyBtn;
        this.CancelButton = _cancelBtn;

        this.Controls.AddRange(new Control[]
        {
            _infoLabel, _progressLabel, _progressBar, _cancelBtn, _applyBtn
        });

        // ---------- 内容セットアップ ----------
        string errMsg = ValidatePatch();
        if (errMsg != null)
        {
            _infoLabel.Text      = "エラー:\r\n" + errMsg;
            _applyBtn.Enabled    = false;
            return;
        }

        string currentVer = ReadVersion(Path.Combine(_internalDir, "version.json")) ?? "不明";
        string newVer     = ReadVersion(Path.Combine(_patchDir,    "version.json")) ?? "不明";
        _infoLabel.Text =
            "パッチを適用しますか？\r\n\r\n" +
            "現在のバージョン : v" + currentVer + "\r\n" +
            "適用後のバージョン: v" + newVer     + "\r\n\r\n" +
            "※ 適用前にメインアプリを終了してください。";
    }

    protected override void OnClosed(EventArgs e)
    {
        base.OnClosed(e);
        CleanupTemporaryPatchDir();
    }

    // ------------------------------------------------------------------
    //  バリデーション
    // ------------------------------------------------------------------

    private string ValidatePatch()
    {
        string[] managedRoots = LoadManagedInternalRoots();
        if (!Directory.Exists(_patchDir))
            return "パッチ内容が見つかりません。\r\n" +
                   "apply_patch.exe と同じフォルダに patch/ フォルダを置くか、\r\n" +
                   "埋め込みパッチ付きの apply_patch.exe を使用してください。\r\n" +
                   "(" + _patchDir + ")";
        if (!File.Exists(Path.Combine(_patchDir, "version.json")))
            return "patch/version.json が見つかりません。";
        foreach (string root in managedRoots)
        {
            if (!Directory.Exists(Path.Combine(_patchDir, root)))
                return "patch/" + root + " フォルダが見つかりません。";
        }
        if (!Directory.Exists(_internalDir))
            return "_internal フォルダが見つかりません。\r\n" +
                   "メインアプリと同じフォルダで実行してください。\r\n" +
                   "(" + _internalDir + ")";
        if (!File.Exists(Path.Combine(_internalDir, "version.json")))
            return "_internal/version.json が見つかりません。";
        return null;
    }

    // ------------------------------------------------------------------
    //  バージョン読み込み（System.Text.Json 不要・正規表現で抽出）
    // ------------------------------------------------------------------

    private static string ReadVersion(string path)
    {
        try
        {
            string text = File.ReadAllText(path, System.Text.Encoding.UTF8);
            var m = Regex.Match(text, @"""version""\s*:\s*""([^""]+)""");
            return m.Success ? m.Groups[1].Value.TrimStart('v') : null;
        }
        catch
        {
            return null;
        }
    }

    // ------------------------------------------------------------------
    //  パッチ適用（BackgroundWorker で非同期）
    // ------------------------------------------------------------------

    private void OnApply(object sender, EventArgs e)
    {
        _applyBtn.Enabled  = false;
        _cancelBtn.Enabled = false;
        _progressLabel.Visible = true;
        _progressBar.Visible   = true;

        var worker = new BackgroundWorker();
        worker.DoWork             += DoApply;
        worker.RunWorkerCompleted += OnApplyCompleted;
        worker.RunWorkerAsync();
    }

    private void DoApply(object sender, DoWorkEventArgs e)
    {
        try
        {
            string[] managedRoots = LoadManagedInternalRoots();
            string verSrc  = Path.Combine(_patchDir,    "version.json");
            string verDest = Path.Combine(_internalDir, "version.json");

            foreach (string root in managedRoots)
            {
                string src = Path.Combine(_patchDir, root);
                string dest = Path.Combine(_internalDir, root);

                SetProgress("既存の " + root + " フォルダを削除中...");
                if (Directory.Exists(dest))
                    Directory.Delete(dest, recursive: true);

                SetProgress("新しい " + root + " フォルダをコピー中...");
                CopyDirectory(src, dest);
            }

            SetProgress("version.json を更新中...");
            File.Copy(verSrc, verDest, overwrite: true);
        }
        catch (Exception ex)
        {
            e.Result = ex.Message;
        }
    }

    private void OnApplyCompleted(object sender, RunWorkerCompletedEventArgs e)
    {
        _progressBar.Visible = false;

        string errorMsg = e.Result as string;
        if (errorMsg != null)
        {
            _progressLabel.Text = "エラー: " + errorMsg;
            MessageBox.Show(
                "エラーが発生しました:\r\n" + errorMsg,
                "エラー",
                MessageBoxButtons.OK,
                MessageBoxIcon.Error);
            _cancelBtn.Enabled = true;
        }
        else
        {
            _progressLabel.Text = "完了！アプリを再起動してください。";
            MessageBox.Show(
                "パッチが正常に適用されました。\r\nアプリを再起動してください。",
                "完了",
                MessageBoxButtons.OK,
                MessageBoxIcon.Information);
            Application.Exit();
        }
    }

    // ------------------------------------------------------------------
    //  ユーティリティ
    // ------------------------------------------------------------------

    private void SetProgress(string msg)
    {
        if (InvokeRequired)
            Invoke((Action)(() => _progressLabel.Text = msg));
        else
            _progressLabel.Text = msg;
    }

    private static string ResolvePatchDir(string patcherDir, out string tempPatchDir)
    {
        string patchDir = Path.Combine(patcherDir, "patch");
        tempPatchDir = null;
        if (Directory.Exists(patchDir))
            return patchDir;

        string resourceName = FindEmbeddedPatchResourceName();
        if (resourceName == null)
            return patchDir;

        tempPatchDir = Path.Combine(Path.GetTempPath(), "MyBeatSaberStatsPatch_" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(tempPatchDir);
        string zipPath = Path.Combine(tempPatchDir, "patch_payload.zip");

        using (Stream stream = Assembly.GetExecutingAssembly().GetManifestResourceStream(resourceName))
        {
            if (stream == null)
                return patchDir;
            using (FileStream fs = File.Create(zipPath))
                stream.CopyTo(fs);
        }

        string extractDir = Path.Combine(tempPatchDir, "patch");
        ZipFile.ExtractToDirectory(zipPath, extractDir);
        return extractDir;
    }

    private static string FindEmbeddedPatchResourceName()
    {
        foreach (string name in Assembly.GetExecutingAssembly().GetManifestResourceNames())
        {
            if (name.EndsWith("patch_payload.zip", StringComparison.OrdinalIgnoreCase))
                return name;
        }
        return null;
    }

    private void CleanupTemporaryPatchDir()
    {
        try
        {
            if (!string.IsNullOrEmpty(_tempPatchDir) && Directory.Exists(_tempPatchDir))
                Directory.Delete(_tempPatchDir, recursive: true);
        }
        catch
        {
        }
    }

    private string[] LoadManagedInternalRoots()
    {
        string configPath = Path.Combine(_patchDir, "resources", "update_targets.json");
        try
        {
            if (!File.Exists(configPath))
                return DefaultManagedRoots;

            string text = File.ReadAllText(configPath, System.Text.Encoding.UTF8);
            Match arrayMatch = Regex.Match(
                text,
                @"""internal_sync_dirs""\s*:\s*\[(.*?)\]",
                RegexOptions.Singleline);
            if (!arrayMatch.Success)
                return DefaultManagedRoots;

            var roots = new List<string>();
            MatchCollection matches = Regex.Matches(arrayMatch.Groups[1].Value, @"""([^""]+)""");
            foreach (Match match in matches)
            {
                string value = match.Groups[1].Value.Trim();
                if (value.Length > 0)
                    roots.Add(value);
            }
            return roots.Count > 0 ? roots.ToArray() : DefaultManagedRoots;
        }
        catch
        {
            return DefaultManagedRoots;
        }
    }

    private static void CopyDirectory(string src, string dest)
    {
        Directory.CreateDirectory(dest);
        foreach (string file in Directory.GetFiles(src))
            File.Copy(file, Path.Combine(dest, Path.GetFileName(file)), overwrite: true);
        foreach (string dir in Directory.GetDirectories(src))
            CopyDirectory(dir, Path.Combine(dest, Path.GetFileName(dir)));
    }
}
