using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Net;
using System.Reflection;
using System.Text;
using System.Text.RegularExpressions;
using System.Globalization;
using System.Web.Script.Serialization;
using System.Windows.Forms;

static class Program
{
    [STAThread]
    static void Main(string[] args)
    {
        ServicePointManager.SecurityProtocol = (SecurityProtocolType)3072 | (SecurityProtocolType)768 | SecurityProtocolType.Tls;

        CommandLineOptions options;
        try
        {
            options = CommandLineOptions.Parse(args);
        }
        catch (Exception ex)
        {
            MessageBox.Show(ex.Message, "Update.exe", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return;
        }

        if (options.ApplyStaged)
        {
            try
            {
                UpdateInstaller.ApplyStagedPackage(
                    options.ZipSource,
                    options.InstallDir,
                    options.ExePath,
                    options.WaitPid,
                    !options.NoRestart,
                    options.CleanupDir,
                    options.TargetVersion,
                    options.PreserveUpdater);
            }
            catch (Exception ex)
            {
                MessageBox.Show(
                    "更新の適用に失敗しました:\r\n" + ex.Message,
                    "アップデートエラー",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Error);
            }
            return;
        }

        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new UpdateDialog(options));
    }
}

sealed class CommandLineOptions
{
    public string InstallDir;
    public string ExePath;
    public string Tag;
    public bool ApplyStaged;
    public string ZipSource;
    public int WaitPid;
    public bool NoRestart;
    public string CleanupDir;
    public string TargetVersion;
    public bool PreserveUpdater;

    public static CommandLineOptions Parse(string[] args)
    {
        string exeLocation = Assembly.GetExecutingAssembly().Location;
        string baseDir = Path.GetDirectoryName(exeLocation);
        var options = new CommandLineOptions();
        options.InstallDir = baseDir;
        options.ExePath = UpdateInstaller.InferTargetExePath(baseDir);

        for (int i = 0; i < args.Length; i++)
        {
            string arg = args[i];
            switch (arg)
            {
                case "--tag":
                    options.Tag = ReadValue(args, ref i, arg);
                    break;
                case "--install-dir":
                    options.InstallDir = Path.GetFullPath(ReadValue(args, ref i, arg));
                    if (string.IsNullOrEmpty(options.ExePath))
                        options.ExePath = UpdateInstaller.InferTargetExePath(options.InstallDir);
                    break;
                case "--exe-path":
                    options.ExePath = Path.GetFullPath(ReadValue(args, ref i, arg));
                    break;
                case "--apply-staged":
                    options.ApplyStaged = true;
                    break;
                case "--zip":
                    options.ZipSource = ReadValue(args, ref i, arg);
                    break;
                case "--wait-pid":
                    options.WaitPid = int.Parse(ReadValue(args, ref i, arg));
                    break;
                case "--no-restart":
                    options.NoRestart = true;
                    break;
                case "--cleanup-dir":
                    options.CleanupDir = Path.GetFullPath(ReadValue(args, ref i, arg));
                    break;
                case "--target-version":
                    options.TargetVersion = ReadValue(args, ref i, arg);
                    break;
                case "--preserve-updater":
                    options.PreserveUpdater = true;
                    break;
                default:
                    throw new InvalidOperationException("未知の引数です: " + arg);
            }
        }

        if (options.ApplyStaged)
        {
            if (string.IsNullOrEmpty(options.ZipSource))
                throw new InvalidOperationException("--apply-staged には --zip が必要です。");
            options.ZipSource = Path.GetFullPath(options.ZipSource);
        }

        options.ExePath = UpdateInstaller.ValidateInstallDirectory(options.InstallDir, options.ExePath);

        return options;
    }

    private static string ReadValue(string[] args, ref int index, string option)
    {
        if (index + 1 >= args.Length)
            throw new InvalidOperationException(option + " の値がありません。");
        index += 1;
        return args[index];
    }
}

sealed class ReleaseInfo
{
    public string TagName;
    public string Title;
    public string Body;
    public string PublishedAt;
    public string ZipUrl;
    public string Version;

    public override string ToString()
    {
        string text = TagName;
        if (!string.IsNullOrEmpty(Title) && Title != TagName)
            text += "  " + Title;
        if (!string.IsNullOrEmpty(PublishedAt))
            text += "\r\n" + GetPublishedAtDisplay();
        return text;
    }

    public string GetPublishedAtDisplay()
    {
        string value = (PublishedAt ?? string.Empty).Trim();
        if (value.Length == 0)
            return string.Empty;

        DateTimeOffset parsed;
        if (!DateTimeOffset.TryParse(
            value,
            CultureInfo.InvariantCulture,
            DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal,
            out parsed))
        {
            return value.Replace("T", " ").Replace("Z", " UTC");
        }

        DateTimeOffset local = parsed.ToLocalTime();
        TimeSpan offset = local.Offset;
        string sign = offset < TimeSpan.Zero ? "-" : "+";
        offset = offset.Duration();
        return string.Format(
            CultureInfo.InvariantCulture,
            "{0:yyyy-MM-dd HH:mm:ss} UTC{1}{2}:{3:00}",
            local.DateTime,
            sign,
            (int)offset.TotalHours,
            offset.Minutes);
    }
}

sealed class UpdateDialog : Form
{
    private readonly CommandLineOptions _options;
    private readonly Label _currentLabel;
    private readonly Label _statusLabel;
    private readonly TextBox _filterEdit;
    private readonly ListBox _listBox;
    private readonly TextBox _notesBox;
    private readonly ProgressBar _progressBar;
    private readonly Button _developerButton;
    private readonly Label _developerWarningLabel;
    private readonly CheckBox _preserveUpdaterCheckBox;
    private readonly Button _refreshButton;
    private readonly Button _applyButton;
    private readonly Button _cancelButton;
    private readonly BackgroundWorker _loadWorker;
    private readonly BackgroundWorker _applyWorker;
    private List<ReleaseInfo> _releases = new List<ReleaseInfo>();

    public UpdateDialog(CommandLineOptions options)
    {
        _options = options;

        Text = "Update.exe";
        FormBorderStyle = FormBorderStyle.FixedDialog;
        StartPosition = FormStartPosition.CenterScreen;
        MaximizeBox = false;
        MinimizeBox = false;
        ClientSize = new System.Drawing.Size(700, 640);
        Padding = new Padding(12);

        _currentLabel = new Label
        {
            AutoSize = false,
            Location = new System.Drawing.Point(12, 12),
            Size = new System.Drawing.Size(676, 48),
            Text = BuildHeaderText()
        };

        _filterEdit = new TextBox
        {
            Location = new System.Drawing.Point(12, 66),
            Size = new System.Drawing.Size(676, 27),
            Text = string.Empty
        };
        _filterEdit.TextChanged += delegate { ApplyFilter(); };

        _statusLabel = new Label
        {
            AutoSize = false,
            Location = new System.Drawing.Point(12, 98),
            Size = new System.Drawing.Size(676, 20),
            Text = "候補を取得中..."
        };

        _listBox = new ListBox
        {
            Location = new System.Drawing.Point(12, 124),
            Size = new System.Drawing.Size(676, 220),
            HorizontalScrollbar = true
        };
        _listBox.SelectedIndexChanged += delegate { SyncSelectionToDetail(); };
        _listBox.DoubleClick += delegate { BeginApply(); };

        _notesBox = new TextBox
        {
            Location = new System.Drawing.Point(12, 352),
            Size = new System.Drawing.Size(676, 92),
            Multiline = true,
            ReadOnly = true,
            ScrollBars = ScrollBars.Vertical
        };

        _developerButton = new Button
        {
            Text = "開発者モード...",
            Location = new System.Drawing.Point(12, 452),
            Size = new System.Drawing.Size(128, 28)
        };
        _developerButton.Click += delegate { ToggleDeveloperOptions(); };

        _developerWarningLabel = new Label
        {
            AutoSize = false,
            Location = new System.Drawing.Point(12, 486),
            Size = new System.Drawing.Size(676, 44),
            Text = "非常用オプションです。通常は使用しません。\r\nチェックすると Updater 自体は更新せず、現在の Updater を維持します。",
            Visible = false
        };

        _preserveUpdaterCheckBox = new CheckBox
        {
            Text = "非常用: Updater を更新しない",
            Location = new System.Drawing.Point(12, 534),
            Size = new System.Drawing.Size(260, 24),
            Visible = false
        };

        _progressBar = new ProgressBar
        {
            Location = new System.Drawing.Point(12, 572),
            Size = new System.Drawing.Size(676, 18),
            Style = ProgressBarStyle.Marquee,
            Visible = false
        };

        _refreshButton = new Button
        {
            Text = "再読込",
            Location = new System.Drawing.Point(332, 598),
            Size = new System.Drawing.Size(88, 28)
        };
        _refreshButton.Click += delegate { LoadReleaseList(); };

        _cancelButton = new Button
        {
            Text = "閉じる",
            Location = new System.Drawing.Point(500, 598),
            Size = new System.Drawing.Size(88, 28)
        };
        _cancelButton.Click += delegate { Close(); };

        _applyButton = new Button
        {
            Text = "アップデート",
            Location = new System.Drawing.Point(600, 598),
            Size = new System.Drawing.Size(88, 28)
        };
        _applyButton.Click += delegate { BeginApply(); };

        Controls.AddRange(new Control[]
        {
            _currentLabel,
            _filterEdit,
            _statusLabel,
            _listBox,
            _notesBox,
            _developerButton,
            _developerWarningLabel,
            _preserveUpdaterCheckBox,
            _progressBar,
            _refreshButton,
            _cancelButton,
            _applyButton,
        });

        _loadWorker = new BackgroundWorker();
        _loadWorker.DoWork += LoadWorker_DoWork;
        _loadWorker.RunWorkerCompleted += LoadWorker_RunWorkerCompleted;

        _applyWorker = new BackgroundWorker();
        _applyWorker.DoWork += ApplyWorker_DoWork;
        _applyWorker.RunWorkerCompleted += ApplyWorker_RunWorkerCompleted;

        Shown += delegate { LoadReleaseList(); };
    }

    private string BuildHeaderText()
    {
        string currentVersion = UpdateInstaller.ReadCurrentVersion(_options.InstallDir) ?? "不明";
        string exeName = string.IsNullOrEmpty(_options.ExePath) ? "(自動判定できません)" : Path.GetFileName(_options.ExePath);
        return "対象フォルダ: " + _options.InstallDir + "\r\n"
            + "対象アプリ: " + exeName + "    現在のバージョン: v" + currentVersion;
    }

    private void SetBusy(bool busy, string message)
    {
        _statusLabel.Text = message;
        _progressBar.Visible = busy;
        _developerButton.Enabled = !busy;
        _preserveUpdaterCheckBox.Enabled = !busy;
        _refreshButton.Enabled = !busy;
        _applyButton.Enabled = !busy;
        _cancelButton.Enabled = !busy;
        _filterEdit.Enabled = !busy;
        _listBox.Enabled = !busy;
    }

    private void LoadReleaseList()
    {
        if (_loadWorker.IsBusy)
            return;
        SetBusy(true, "候補を取得中...");
        _loadWorker.RunWorkerAsync();
    }

    private void LoadWorker_DoWork(object sender, DoWorkEventArgs e)
    {
        e.Result = GitHubReleaseClient.ListReleases(_options, 30);
    }

    private void LoadWorker_RunWorkerCompleted(object sender, RunWorkerCompletedEventArgs e)
    {
        if (e.Error != null)
        {
            SetBusy(false, "候補の取得に失敗しました");
            MessageBox.Show(
                "tag 候補の取得に失敗しました:\r\n" + e.Error.Message,
                "Update.exe",
                MessageBoxButtons.OK,
                MessageBoxIcon.Warning);
            return;
        }

        _releases = (List<ReleaseInfo>)e.Result;
        ApplyFilter();
        SelectInitialRelease();
        SetBusy(false, _releases.Count + " 件の release を取得しました");
    }

    private void SelectInitialRelease()
    {
        string requestedTag = (_options.Tag ?? string.Empty).Trim();
        if (requestedTag.Length == 0 || _listBox.Items.Count == 0)
            return;

        string normalized = requestedTag.StartsWith("v", StringComparison.OrdinalIgnoreCase)
            ? requestedTag
            : "v" + requestedTag;
        for (int i = 0; i < _listBox.Items.Count; i++)
        {
            ReleaseInfo release = _listBox.Items[i] as ReleaseInfo;
            if (release == null)
                continue;
            if (string.Equals(release.TagName, normalized, StringComparison.OrdinalIgnoreCase))
            {
                _listBox.SelectedIndex = i;
                return;
            }
        }
    }

    private void ApplyFilter()
    {
        string filter = (_filterEdit.Text ?? string.Empty).Trim().ToLowerInvariant();
        _listBox.BeginUpdate();
        _listBox.Items.Clear();
        foreach (ReleaseInfo release in _releases)
        {
            string haystack = (release.TagName + "\n" + release.Title + "\n" + release.PublishedAt).ToLowerInvariant();
            if (filter.Length > 0 && haystack.IndexOf(filter, StringComparison.Ordinal) < 0)
                continue;
            _listBox.Items.Add(release);
        }
        _listBox.EndUpdate();

        if (_listBox.Items.Count > 0 && _listBox.SelectedIndex < 0)
            _listBox.SelectedIndex = 0;

        if (_listBox.Items.Count == 0)
        {
            _statusLabel.Text = _releases.Count == 0
                ? "候補がありません"
                : "一致する tag がありません。直接入力できます";
            _notesBox.Text = string.Empty;
        }
        else if (!_loadWorker.IsBusy && !_applyWorker.IsBusy)
        {
            _statusLabel.Text = _listBox.Items.Count + " 件を表示中";
        }
    }

    private void SyncSelectionToDetail()
    {
        ReleaseInfo release = _listBox.SelectedItem as ReleaseInfo;
        if (release == null)
        {
            _notesBox.Text = string.Empty;
            return;
        }

        var builder = new StringBuilder();
        builder.AppendLine(release.TagName);
        if (!string.IsNullOrEmpty(release.Title) && release.Title != release.TagName)
            builder.AppendLine(release.Title);
        if (!string.IsNullOrEmpty(release.PublishedAt))
            builder.AppendLine(release.GetPublishedAtDisplay());
        if (builder.Length > 0)
            builder.AppendLine();
        builder.Append(string.IsNullOrEmpty(release.Body) ? "リリースノートなし" : release.Body);
        _notesBox.Text = builder.ToString();
    }

    private void ToggleDeveloperOptions()
    {
        bool visible = !_developerWarningLabel.Visible;
        _developerWarningLabel.Visible = visible;
        _preserveUpdaterCheckBox.Visible = visible;
        _developerButton.Text = visible ? "開発者モードを閉じる" : "開発者モード...";
    }

    private void BeginApply()
    {
        if (_applyWorker.IsBusy)
            return;

        ReleaseInfo selected = _listBox.SelectedItem as ReleaseInfo;
        string requestedTag = selected != null
            ? selected.TagName
            : (_filterEdit.Text ?? string.Empty).Trim();
        if (string.IsNullOrEmpty(requestedTag) && selected != null)
            requestedTag = selected.TagName;
        if (string.IsNullOrEmpty(requestedTag))
        {
            MessageBox.Show("tag を選択または入力してください。", "Update.exe", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return;
        }

        if (!string.IsNullOrEmpty(_options.ExePath) && UpdateInstaller.IsProcessRunning(_options.ExePath))
        {
            MessageBox.Show(
                "更新前に対象アプリを終了してください。",
                "Update.exe",
                MessageBoxButtons.OK,
                MessageBoxIcon.Information);
            return;
        }

        if (MessageBox.Show(
            "tag " + requestedTag + " を適用します。\r\n続行しますか？",
            "Update.exe",
            MessageBoxButtons.OKCancel,
            MessageBoxIcon.Question) != DialogResult.OK)
        {
            return;
        }

        if (_preserveUpdaterCheckBox.Checked)
        {
            if (MessageBox.Show(
                "開発者モードの非常用オプションが有効です。\r\n"
                    + "Updater 自体は更新されません。通常は使用しません。\r\n続行しますか？",
                "Update.exe",
                MessageBoxButtons.OKCancel,
                MessageBoxIcon.Warning) != DialogResult.OK)
            {
                return;
            }
        }

        SetBusy(true, "更新パッケージを準備中...");
        _applyWorker.RunWorkerAsync(requestedTag);
    }

    private void ApplyWorker_DoWork(object sender, DoWorkEventArgs e)
    {
        string requestedTag = (string)e.Argument;
        ReleaseInfo release = GitHubReleaseClient.ResolveRelease(_options, requestedTag, _releases);
        if (string.IsNullOrEmpty(release.ZipUrl))
            throw new InvalidOperationException("このリリースにはダウンロード可能な zip アセットがありません: " + release.TagName + "\n\nGitHub リリースページで zip アセットが添付されているか確認してください。");
        string stageDir = UpdateInstaller.CreateStageDirectory();
        string zipPath = Path.Combine(stageDir, "release.zip");
        GitHubReleaseClient.DownloadFile(release.ZipUrl, zipPath);

        string helperExe = UpdateInstaller.CopySelfToTemporaryHelper();
        UpdateInstaller.LaunchHelper(
            helperExe,
            zipPath,
            _options.InstallDir,
            _options.ExePath,
            Process.GetCurrentProcess().Id,
            release.Version,
            stageDir,
            _preserveUpdaterCheckBox.Checked);

        e.Result = release;
    }

    private void ApplyWorker_RunWorkerCompleted(object sender, RunWorkerCompletedEventArgs e)
    {
        if (e.Error != null)
        {
            SetBusy(false, "更新パッケージの準備に失敗しました");
            MessageBox.Show(
                "更新の準備に失敗しました:\r\n" + e.Error.Message,
                "Update.exe",
                MessageBoxButtons.OK,
                MessageBoxIcon.Error);
            return;
        }

        ReleaseInfo release = (ReleaseInfo)e.Result;
        SetBusy(false, "外部ヘルパーを起動しました");
        MessageBox.Show(
            "v" + release.Version + " の更新を準備しました。\r\n"
                + "このダイアログを閉じると更新を適用し、自動で再起動します。",
            "Update.exe",
            MessageBoxButtons.OK,
            MessageBoxIcon.Information);
        Close();
    }
}

static class GitHubReleaseClient
{
    private const string Owner = "Buri28";
    private const string Repo = "MyBeatSaberStats";
    private static readonly JavaScriptSerializer Serializer = new JavaScriptSerializer();

    public static List<ReleaseInfo> ListReleases(CommandLineOptions options, int limit)
    {
        string url = "https://api.github.com/repos/" + Owner + "/" + Repo + "/releases?per_page=" + limit;
        string json = DownloadText(url);
        var releases = Serializer.Deserialize<List<GitHubReleaseDto>>(json) ?? new List<GitHubReleaseDto>();
        return releases
            .Where(dto => dto != null && !dto.draft && !string.IsNullOrEmpty(dto.tag_name))
            .Select(dto => ToReleaseInfo(dto, options))
            .ToList();
    }

    public static ReleaseInfo ResolveRelease(CommandLineOptions options, string requestedTag, List<ReleaseInfo> cached)
    {
        string normalized = NormalizeTag(requestedTag);
        foreach (ReleaseInfo release in cached)
        {
            if (string.Equals(release.TagName, normalized, StringComparison.OrdinalIgnoreCase))
                return release;
        }

        string url = "https://api.github.com/repos/" + Owner + "/" + Repo + "/releases/tags/" + normalized;
        string json = DownloadText(url);
        var dto = Serializer.Deserialize<GitHubReleaseDto>(json);
        if (dto == null || string.IsNullOrEmpty(dto.tag_name))
            throw new InvalidOperationException("指定タグの release が見つかりません: " + normalized);
        ReleaseInfo info = ToReleaseInfo(dto, options);
        if (string.IsNullOrEmpty(info.ZipUrl))
            throw new InvalidOperationException("release zip が見つかりません: " + normalized);
        return info;
    }

    public static void DownloadFile(string url, string path)
    {
        using (var client = CreateClient())
            client.DownloadFile(url, path);
    }

    private static string DownloadText(string url)
    {
        using (var client = CreateClient())
            return client.DownloadString(url);
    }

    private static WebClient CreateClient()
    {
        var client = new WebClient();
        client.Headers[HttpRequestHeader.UserAgent] = "MyBeatSaberStats-UpdateExe";
        client.Headers[HttpRequestHeader.Accept] = "application/vnd.github+json";
        client.Encoding = Encoding.UTF8;
        return client;
    }

    private static ReleaseInfo ToReleaseInfo(GitHubReleaseDto dto, CommandLineOptions options)
    {
        string assetPrefix = UpdateInstaller.ResolveAssetPrefix(options.InstallDir, options.ExePath);
        return new ReleaseInfo
        {
            TagName = dto.tag_name,
            Title = string.IsNullOrEmpty(dto.name) ? dto.tag_name : dto.name,
            Body = dto.body ?? string.Empty,
            PublishedAt = dto.published_at ?? string.Empty,
            Version = (dto.tag_name ?? string.Empty).TrimStart('v', 'V'),
            ZipUrl = FindZipUrl(dto.assets ?? new List<GitHubAssetDto>(), assetPrefix),
        };
    }

    private static string FindZipUrl(List<GitHubAssetDto> assets, string assetPrefix)
    {
        string prefixWithDash = assetPrefix + "-";
        foreach (GitHubAssetDto asset in assets)
        {
            string name = asset.name ?? string.Empty;
            if (!name.EndsWith(".zip", StringComparison.OrdinalIgnoreCase))
                continue;
            if (string.Equals(name, assetPrefix + ".zip", StringComparison.OrdinalIgnoreCase)
                || name.StartsWith(prefixWithDash, StringComparison.OrdinalIgnoreCase))
                return asset.browser_download_url ?? asset.url;
        }
        return null;
    }

    private static string NormalizeTag(string tag)
    {
        string value = (tag ?? string.Empty).Trim();
        if (value.Length == 0)
            return value;
        if (!value.StartsWith("v", StringComparison.OrdinalIgnoreCase))
            return "v" + value;
        return value;
    }

    private sealed class GitHubReleaseDto
    {
        public string tag_name { get; set; }
        public string name { get; set; }
        public string body { get; set; }
        public string published_at { get; set; }
        public bool draft { get; set; }
        public List<GitHubAssetDto> assets { get; set; }
    }

    private sealed class GitHubAssetDto
    {
        public string name { get; set; }
        public string browser_download_url { get; set; }
        public string url { get; set; }
    }
}

static class UpdateInstaller
{
    private static readonly string[] PreservedRootFiles = { "Update.exe", "MyBeatSaberUpdater.exe" };
    private static readonly string[] PreservedRootDirectories = { "cache", "snapshots" };
    private static readonly UTF8Encoding Utf8NoBom = new UTF8Encoding(false);

    public static string InferTargetExePath(string installDir)
    {
        if (string.IsNullOrEmpty(installDir) || !Directory.Exists(installDir))
            return null;

        foreach (string name in new[] { "MyBeatSaberStats.exe", "MyBeatSaberRanking.exe" })
        {
            string candidate = Path.Combine(installDir, name);
            if (File.Exists(candidate))
                return candidate;
        }

        string[] others = Directory.GetFiles(installDir, "*.exe")
            .Where(path => !string.Equals(Path.GetFileName(path), "Update.exe", StringComparison.OrdinalIgnoreCase)
                && !string.Equals(Path.GetFileName(path), "MyBeatSaberUpdater.exe", StringComparison.OrdinalIgnoreCase))
            .ToArray();
        return others.Length == 1 ? others[0] : null;
    }

    public static string ValidateInstallDirectory(string installDir, string exePath)
    {
        if (string.IsNullOrEmpty(installDir))
            throw new InvalidOperationException("更新先フォルダが空です。");

        string fullInstallDir = Path.GetFullPath(installDir);
        if (!Directory.Exists(fullInstallDir))
            throw new InvalidOperationException("更新先フォルダが見つかりません: " + fullInstallDir);

        string resolvedExe = exePath;
        if (!string.IsNullOrEmpty(resolvedExe))
            resolvedExe = Path.GetFullPath(resolvedExe);
        if (string.IsNullOrEmpty(resolvedExe) || !File.Exists(resolvedExe))
            resolvedExe = InferTargetExePath(fullInstallDir);

        if (string.IsNullOrEmpty(resolvedExe) || !File.Exists(resolvedExe))
        {
            throw new InvalidOperationException(
                "更新先フォルダに対象アプリの exe が見つかりません。\r\n"
                + "Update.exe と同じフォルダに MyBeatSaberStats.exe を置いて実行してください。\r\n"
                + "対象フォルダ: " + fullInstallDir);
        }

        string exeDir = Path.GetDirectoryName(resolvedExe);
        if (!string.Equals(exeDir, fullInstallDir, StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidOperationException(
                "対象アプリの exe が更新先フォルダ直下にありません。\r\n"
                + "更新先フォルダ: " + fullInstallDir + "\r\n"
                + "検出した exe: " + resolvedExe);
        }

        return resolvedExe;
    }

    public static string ResolveAssetPrefix(string installDir, string exePath)
    {
        if (!string.IsNullOrEmpty(exePath))
            return Path.GetFileNameWithoutExtension(exePath);
        string inferred = InferTargetExePath(installDir);
        if (!string.IsNullOrEmpty(inferred))
            return Path.GetFileNameWithoutExtension(inferred);
        return "MyBeatSaberStats";
    }

    public static string ReadCurrentVersion(string installDir)
    {
        try
        {
            string path = Path.Combine(installDir, "_internal", "version.json");
            if (!File.Exists(path))
                return null;
            string text = File.ReadAllText(path, Encoding.UTF8);
            Match match = Regex.Match(text, @"""version""\s*:\s*""([^""]+)""");
            if (!match.Success)
                return null;
            return match.Groups[1].Value.TrimStart('v', 'V');
        }
        catch
        {
            return null;
        }
    }

    public static string CreateStageDirectory()
    {
        string path = Path.Combine(Path.GetTempPath(), "MyBeatSaberStatsUpdate_" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(path);
        return path;
    }

    public static string CopySelfToTemporaryHelper()
    {
        string helperDir = Path.Combine(Path.GetTempPath(), "MyBeatSaberStatsUpdateHelper_" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(helperDir);
        string src = Assembly.GetExecutingAssembly().Location;
        string dest = Path.Combine(helperDir, Path.GetFileName(src));
        File.Copy(src, dest, true);
        return dest;
    }

    public static void LaunchHelper(
        string updaterExe,
        string zipPath,
        string installDir,
        string exePath,
        int waitPid,
        string targetVersion,
        string cleanupDir,
        bool preserveUpdater)
    {
        string arguments = BuildApplyArguments(zipPath, installDir, exePath, waitPid, targetVersion, cleanupDir, preserveUpdater);
        var startInfo = new ProcessStartInfo(updaterExe, arguments);
        startInfo.UseShellExecute = false;
        startInfo.CreateNoWindow = true;
        startInfo.WindowStyle = ProcessWindowStyle.Hidden;
        Process.Start(startInfo);
    }

    public static void ApplyStagedPackage(
        string zipPath,
        string installDir,
        string exePath,
        int waitPid,
        bool restart,
        string cleanupDir,
        string targetVersion,
        bool preserveUpdater)
    {
        exePath = ValidateInstallDirectory(installDir, exePath);
        WaitForProcessExit(waitPid);
        string stageRoot = !string.IsNullOrEmpty(cleanupDir) ? cleanupDir : Path.GetDirectoryName(zipPath);
        string extractDir = Path.Combine(stageRoot, "unzipped");
        string sourceRoot = ExtractReleaseRoot(zipPath, extractDir);
        MirrorDirectory(sourceRoot, installDir, preserveUpdater);
        if (!string.IsNullOrEmpty(targetVersion))
            WriteVersion(installDir, targetVersion);
        if (!string.IsNullOrEmpty(cleanupDir) && Directory.Exists(cleanupDir))
            TryDeleteDirectory(cleanupDir);
        if (restart && !string.IsNullOrEmpty(exePath) && File.Exists(exePath))
            Process.Start(exePath);
    }

    public static bool IsProcessRunning(string exePath)
    {
        if (string.IsNullOrEmpty(exePath) || !File.Exists(exePath))
            return false;

        string fullPath = Path.GetFullPath(exePath);
        string processName = Path.GetFileNameWithoutExtension(fullPath);
        foreach (Process process in Process.GetProcessesByName(processName))
        {
            try
            {
                if (string.Equals(process.MainModule.FileName, fullPath, StringComparison.OrdinalIgnoreCase))
                    return true;
            }
            catch
            {
            }
            finally
            {
                process.Dispose();
            }
        }
        return false;
    }

    private static void WaitForProcessExit(int pid)
    {
        if (pid <= 0)
            return;
        try
        {
            using (Process process = Process.GetProcessById(pid))
            {
                process.WaitForExit();
            }
        }
        catch
        {
        }
    }

    private static string ExtractReleaseRoot(string zipPath, string extractDir)
    {
        TryDeleteDirectory(extractDir);
        Directory.CreateDirectory(extractDir);
        ZipFile.ExtractToDirectory(zipPath, extractDir);
        string[] directories = Directory.GetDirectories(extractDir);
        if (directories.Length == 1)
            return directories[0];
        if (directories.Length > 0)
            return extractDir;
        throw new InvalidOperationException("展開したリリースフォルダが見つかりません。");
    }

    private static void MirrorDirectory(string sourceDir, string destDir, bool preserveUpdater)
    {
        var expected = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var preserved = new HashSet<string>(PreservedRootFiles, StringComparer.OrdinalIgnoreCase);
        var preservedDirectories = new HashSet<string>(PreservedRootDirectories, StringComparer.OrdinalIgnoreCase);
        Directory.CreateDirectory(destDir);

        foreach (string dir in Directory.GetDirectories(sourceDir, "*", SearchOption.AllDirectories))
        {
            string rel = GetRelativePath(sourceDir, dir);
            if (IsUnderPreservedDirectory(rel, preservedDirectories))
                continue;
            expected.Add(rel);
            Directory.CreateDirectory(Path.Combine(destDir, rel));
        }

        foreach (string file in Directory.GetFiles(sourceDir, "*", SearchOption.AllDirectories))
        {
            string rel = GetRelativePath(sourceDir, file);
            if (IsUnderPreservedDirectory(rel, preservedDirectories))
                continue;
            if (preserveUpdater && preserved.Contains(rel))
                continue;
            expected.Add(rel);
            string target = Path.Combine(destDir, rel);
            Directory.CreateDirectory(Path.GetDirectoryName(target));
            byte[] bytes = File.ReadAllBytes(file);
            byte[] normalized = NormalizeStagedFileBytes(rel, bytes);
            if (!ReferenceEquals(bytes, normalized))
            {
                File.WriteAllBytes(target, normalized);
                File.SetLastWriteTime(target, File.GetLastWriteTime(file));
                File.SetAttributes(target, File.GetAttributes(file));
            }
            else
            {
                File.Copy(file, target, true);
            }
        }

        foreach (string path in Directory.GetFiles(destDir, "*", SearchOption.AllDirectories))
        {
            string rel = GetRelativePath(destDir, path);
            if (IsUnderPreservedDirectory(rel, preservedDirectories))
                continue;
            if (preserved.Contains(rel))
                continue;
            if (!expected.Contains(rel))
                File.Delete(path);
        }

        foreach (string path in Directory.GetDirectories(destDir, "*", SearchOption.AllDirectories)
            .OrderByDescending(value => value.Length))
        {
            string rel = GetRelativePath(destDir, path);
            if (IsUnderPreservedDirectory(rel, preservedDirectories))
                continue;
            if (!expected.Contains(rel) && Directory.Exists(path) && Directory.GetFileSystemEntries(path).Length == 0)
                Directory.Delete(path, true);
        }
    }

    private static bool IsUnderPreservedDirectory(string relPath, HashSet<string> preservedDirectories)
    {
        if (string.IsNullOrEmpty(relPath))
            return false;
        string normalized = relPath.Replace(Path.AltDirectorySeparatorChar, Path.DirectorySeparatorChar);
        string[] parts = normalized.Split(new[] { Path.DirectorySeparatorChar }, StringSplitOptions.RemoveEmptyEntries);
        return parts.Length > 0 && preservedDirectories.Contains(parts[0]);
    }

    private static string GetRelativePath(string root, string path)
    {
        string normalizedRoot = root.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar) + Path.DirectorySeparatorChar;
        Uri rootUri = new Uri(normalizedRoot);
        Uri pathUri = new Uri(path);
        return Uri.UnescapeDataString(rootUri.MakeRelativeUri(pathUri).ToString().Replace('/', Path.DirectorySeparatorChar));
    }

    private static byte[] NormalizeStagedFileBytes(string relPath, byte[] content)
    {
        string normalized = relPath.Replace(Path.AltDirectorySeparatorChar, Path.DirectorySeparatorChar);
        bool isPython = normalized.EndsWith(".py", StringComparison.OrdinalIgnoreCase);
        bool isVersionJson = normalized.Equals(Path.Combine("_internal", "version.json"), StringComparison.OrdinalIgnoreCase);
        if (!isPython && !isVersionJson)
            return content;

        int offset = HasUtf8Bom(content) ? 3 : 0;
        string text = Encoding.UTF8.GetString(content, offset, content.Length - offset);
        text = text.Replace("\r\n", "\n").Replace("\r", "\n");
        text = text.Replace("\n", "\r\n");
        return Utf8NoBom.GetBytes(text);
    }

    private static bool HasUtf8Bom(byte[] content)
    {
        return content.Length >= 3 && content[0] == 0xEF && content[1] == 0xBB && content[2] == 0xBF;
    }

    private static void WriteVersion(string installDir, string version)
    {
        string path = Path.Combine(installDir, "_internal", "version.json");
        Directory.CreateDirectory(Path.GetDirectoryName(path));
        string json = "{\r\n  \"version\": \"" + version.TrimStart('v', 'V') + "\"\r\n}";
        File.WriteAllText(path, json, Utf8NoBom);
    }

    private static string BuildApplyArguments(
        string zipPath,
        string installDir,
        string exePath,
        int waitPid,
        string targetVersion,
        string cleanupDir,
        bool preserveUpdater)
    {
        var parts = new List<string>();
        parts.Add("--apply-staged");
        parts.Add("--zip");
        parts.Add(Quote(zipPath));
        parts.Add("--install-dir");
        parts.Add(Quote(installDir));
        if (!string.IsNullOrEmpty(exePath))
        {
            parts.Add("--exe-path");
            parts.Add(Quote(exePath));
        }
        parts.Add("--wait-pid");
        parts.Add(waitPid.ToString());
        if (!string.IsNullOrEmpty(targetVersion))
        {
            parts.Add("--target-version");
            parts.Add(Quote(targetVersion));
        }
        if (!string.IsNullOrEmpty(cleanupDir))
        {
            parts.Add("--cleanup-dir");
            parts.Add(Quote(cleanupDir));
        }
        if (preserveUpdater)
            parts.Add("--preserve-updater");
        return string.Join(" ", parts.ToArray());
    }

    private static string Quote(string value)
    {
        return "\"" + (value ?? string.Empty).Replace("\"", "\\\"") + "\"";
    }

    private static void TryDeleteDirectory(string path)
    {
        try
        {
            if (Directory.Exists(path))
                Directory.Delete(path, true);
        }
        catch
        {
        }
    }
}