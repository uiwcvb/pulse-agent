using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Text;
using System.IO;
using System.Net;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Win32;

[assembly: AssemblyTitle("DSB · DeepSeek Bot 连接守护")]
[assembly: AssemblyProduct("DSB · DeepSeek Bot Sentinel v4.0")]
[assembly: AssemblyVersion("4.0.0.0")]

namespace DeepSeekClawSentinel
{
    internal static class NativeMethods
    {
        [DllImport("user32.dll")]
        public static extern bool SetProcessDpiAwarenessContext(IntPtr value);

        [DllImport("user32.dll")]
        public static extern bool SetProcessDPIAware();

        [DllImport("user32.dll")]
        public static extern bool DestroyIcon(IntPtr handle);

        [DllImport("user32.dll")]
        public static extern bool SetForegroundWindow(IntPtr hWnd);

        [DllImport("dwmapi.dll")]
        public static extern int DwmSetWindowAttribute(IntPtr hwnd, int attr, ref int attrValue, int attrSize);

        public static void EnableDarkTitleBar(IntPtr handle)
        {
            if (handle == IntPtr.Zero) return;
            try
            {
                int useDark = 1;
                // DWMWA_USE_IMMERSIVE_DARK_MODE (20 on Win10 2004+/Win11, 19 on older Win10)
                if (DwmSetWindowAttribute(handle, 20, ref useDark, sizeof(int)) != 0)
                {
                    DwmSetWindowAttribute(handle, 19, ref useDark, sizeof(int));
                }
            }
            catch { }
        }
    }

    internal static class UiTheme
    {
        public static readonly Color Bg = Color.FromArgb(15, 17, 22);
        public static readonly Color Card = Color.FromArgb(22, 25, 34);
        public static readonly Color CardBorder = Color.FromArgb(38, 44, 58);
        public static readonly Color PillBg = Color.FromArgb(30, 34, 46);
        public static readonly Color PillHover = Color.FromArgb(40, 46, 62);
        public static readonly Color Accent = Color.FromArgb(10, 132, 255);
        public static readonly Color AccentHover = Color.FromArgb(36, 148, 255);
        public static readonly Color AccentCyan = Color.FromArgb(56, 189, 248);
        public static readonly Color Ink = Color.FromArgb(244, 246, 250);
        public static readonly Color Muted = Color.FromArgb(148, 163, 184);
        public static readonly Color Faint = Color.FromArgb(100, 116, 139);
        public static readonly Color Ok = Color.FromArgb(48, 209, 88);
        public static readonly Color Warn = Color.FromArgb(255, 185, 48);
        public static readonly Color Err = Color.FromArgb(255, 69, 58);

        public static GraphicsPath RoundedRect(RectangleF r, float radius)
        {
            GraphicsPath path = new GraphicsPath();
            float d = Math.Max(1f, radius * 2f);
            path.AddArc(r.X, r.Y, d, d, 180, 90);
            path.AddArc(r.Right - d, r.Y, d, d, 270, 90);
            path.AddArc(r.Right - d, r.Bottom - d, d, d, 0, 90);
            path.AddArc(r.X, r.Bottom - d, d, d, 90, 90);
            path.CloseFigure();
            return path;
        }

        public static void DrawDsbMark(Graphics g, float x, float y, float size, Color accentColor)
        {
            g.SmoothingMode = SmoothingMode.HighQuality;
            float s = size / 24f;
            PointF[] earShell = new PointF[]
            {
                new PointF(x + 4.5f * s, y + 9.8f * s),
                new PointF(x + 7.2f * s, y + 4.0f * s),
                new PointF(x + 11.1f * s, y + 7.9f * s),
                new PointF(x + 12.9f * s, y + 7.9f * s),
                new PointF(x + 16.8f * s, y + 4.0f * s),
                new PointF(x + 19.5f * s, y + 9.8f * s),
                new PointF(x + 19.5f * s, y + 13.8f * s),
                new PointF(x + 16.5f * s, y + 19.8f * s),
                new PointF(x + 12.0f * s, y + 20.6f * s),
                new PointF(x + 7.5f * s, y + 19.8f * s),
                new PointF(x + 4.5f * s, y + 13.8f * s),
            };
            using (Pen p = new Pen(Ink, Math.Max(1.6f, 1.85f * s)))
            {
                p.LineJoin = LineJoin.Round;
                p.StartCap = LineCap.Round;
                p.EndCap = LineCap.Round;
                g.DrawPolygon(p, earShell);
            }
            PointF[] diamond = new PointF[]
            {
                new PointF(x + 12.0f * s, y + 10.4f * s),
                new PointF(x + 13.8f * s, y + 13.4f * s),
                new PointF(x + 12.0f * s, y + 16.4f * s),
                new PointF(x + 10.2f * s, y + 13.4f * s),
            };
            using (SolidBrush br = new SolidBrush(accentColor))
            {
                g.FillPolygon(br, diamond);
            }
        }
    }

    internal sealed class RoundedCardPanel : Panel
    {
        public int CornerRadius { get; set; }
        public Color FillColor { get; set; }
        public Color BorderColor { get; set; }

        public RoundedCardPanel()
        {
            this.DoubleBuffered = true;
            this.SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.SupportsTransparentBackColor, true);
            this.BackColor = Color.Transparent;
            this.CornerRadius = 12;
            this.FillColor = UiTheme.Card;
            this.BorderColor = UiTheme.CardBorder;
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            e.Graphics.SmoothingMode = SmoothingMode.HighQuality;
            RectangleF rect = new RectangleF(0.5f, 0.5f, Math.Max(4, this.Width - 2), Math.Max(4, this.Height - 2));
            using (GraphicsPath gp = UiTheme.RoundedRect(rect, this.CornerRadius))
            using (SolidBrush fill = new SolidBrush(this.FillColor))
            using (Pen border = new Pen(this.BorderColor, 1f))
            {
                e.Graphics.FillPath(fill, gp);
                e.Graphics.DrawPath(border, gp);
            }
        }
    }

    internal sealed class ModernPillButton : Control
    {
        private bool hovered = false;
        private bool pressed = false;
        public int CornerRadius { get; set; }
        public Color BaseColor { get; set; }
        public Color HoverColor { get; set; }
        public Color BorderColor { get; set; }
        public bool ShowBorder { get; set; }

        public ModernPillButton()
        {
            this.DoubleBuffered = true;
            this.SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw | ControlStyles.SupportsTransparentBackColor, true);
            this.Cursor = Cursors.Hand;
            this.CornerRadius = 9;
            this.BaseColor = UiTheme.PillBg;
            this.HoverColor = UiTheme.PillHover;
            this.BorderColor = UiTheme.CardBorder;
            this.ShowBorder = true;
            this.ForeColor = UiTheme.Ink;
            this.BackColor = Color.Transparent;
        }

        protected override void OnMouseEnter(EventArgs e) { this.hovered = true; this.Invalidate(); base.OnMouseEnter(e); }
        protected override void OnMouseLeave(EventArgs e) { this.hovered = false; this.pressed = false; this.Invalidate(); base.OnMouseLeave(e); }
        protected override void OnMouseDown(MouseEventArgs e) { if (e.Button == MouseButtons.Left) { this.pressed = true; this.Invalidate(); } base.OnMouseDown(e); }
        protected override void OnMouseUp(MouseEventArgs e) { this.pressed = false; this.Invalidate(); base.OnMouseUp(e); }
        protected override void OnTextChanged(EventArgs e) { this.Invalidate(); base.OnTextChanged(e); }

        protected override void OnPaint(PaintEventArgs e)
        {
            e.Graphics.SmoothingMode = SmoothingMode.HighQuality;
            e.Graphics.TextRenderingHint = TextRenderingHint.ClearTypeGridFit;
            Color bg = this.pressed ? ControlPaint.Dark(this.HoverColor, 0.08f) : (this.hovered ? this.HoverColor : this.BaseColor);
            RectangleF rect = new RectangleF(0.5f, 0.5f, Math.Max(4, this.Width - 2), Math.Max(4, this.Height - 2));
            using (GraphicsPath gp = UiTheme.RoundedRect(rect, this.CornerRadius))
            using (SolidBrush br = new SolidBrush(bg))
            {
                e.Graphics.FillPath(br, gp);
                if (this.ShowBorder)
                {
                    using (Pen pen = new Pen(this.hovered ? Color.FromArgb(72, 132, 255) : this.BorderColor, 1f))
                    {
                        e.Graphics.DrawPath(pen, gp);
                    }
                }
            }
            TextFormatFlags flags = TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter | TextFormatFlags.EndEllipsis | TextFormatFlags.NoPrefix;
            TextRenderer.DrawText(e.Graphics, this.Text ?? "", this.Font, this.ClientRectangle, this.ForeColor, flags);
        }
    }

    internal sealed class DsbBrandBadgeBox : Control
    {
        public Color DotColor { get; set; }

        public DsbBrandBadgeBox()
        {
            this.SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.SupportsTransparentBackColor, true);
            this.BackColor = Color.Transparent;
            this.DotColor = UiTheme.AccentCyan;
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            e.Graphics.SmoothingMode = SmoothingMode.HighQuality;
            float sz = Math.Min(this.Width, this.Height) - 2f;
            RectangleF r = new RectangleF(1f, 1f, sz, sz);
            using (GraphicsPath gp = UiTheme.RoundedRect(r, sz * 0.28f))
            using (SolidBrush br = new SolidBrush(Color.FromArgb(24, 28, 38)))
            using (Pen pen = new Pen(UiTheme.CardBorder, 1f))
            {
                e.Graphics.FillPath(br, gp);
                e.Graphics.DrawPath(pen, gp);
            }
            float inset = sz * 0.16f;
            UiTheme.DrawDsbMark(e.Graphics, 1f + inset, 1f + inset, sz - inset * 2f, this.DotColor);
        }
    }

    public sealed class SentinelForm : Form
    {
        private const string RunRegPath = @"Software\Microsoft\Windows\CurrentVersion\Run";
        private const string RunRegName = "DeepSeekClawSentinel";
        private const int GatewayPort = 18195;
        private const int ControlPort = 18197;

        private readonly string pluginDir;
        private readonly string sentinelScriptPath;
        private readonly string logFilePath;
        private readonly EventWaitHandle showEvent;
        private readonly JavaScriptSerializer json = new JavaScriptSerializer();

        private bool suppressInitialShow;
        private NotifyIcon trayIcon;
        private System.Windows.Forms.Timer pollTimer;

        private DsbBrandBadgeBox brandBadge;
        private RoundedCardPanel statusBannerCard;
        private Label lblStatusDot;
        private Label lblStatusText;
        private Label lblStatusSub;
        private Label lblGatewayVal;
        private Label lblCloudVal;
        private Label lblPeersVal;
        private Label lblTrafficVal;
        private Label lblErrorHint;

        private ModernPillButton btnReconnect;
        private ModernPillButton btnRestartCore;
        private ModernPillButton btnWakeDsh;
        private ModernPillButton btnOpenLog;
        private ModernPillButton btnToggleAutostart;
        private ModernPillButton btnHideToTray;

        private Icon iconOnline;
        private Icon iconWarn;
        private Icon iconOffline;
        private int currentIconState = -1;

        private bool allowRealExit = false;
        private bool pollBusy = false;
        private int consecutiveGatewayFailures = 0;
        private int consecutiveCloudOfflineTicks = 0;
        private DateTime lastSpawnAttemptUtc = DateTime.MinValue;
        private DateTime lastAutoReconnectUtc = DateTime.MinValue;
        private int lastKnownSentinelPid = 0;

        public SentinelForm(bool minimized, EventWaitHandle wakeEvent)
        {
            this.suppressInitialShow = minimized;
            this.showEvent = wakeEvent;
            this.pluginDir = Path.GetDirectoryName(Application.ExecutablePath) ?? @"D:\dsh-baike-plugin";
            this.sentinelScriptPath = Path.Combine(this.pluginDir, "sentinel.mjs");
            this.logFilePath = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".dsh", "sentinel.log");

            this.iconOnline = CreateDsbStatusIcon(UiTheme.Ok);
            this.iconWarn = CreateDsbStatusIcon(UiTheme.Warn);
            this.iconOffline = CreateDsbStatusIcon(UiTheme.Err);

            BuildUi();
            SetupTray();
            EnsureAutostartSynced();
            UpdateAutostartButtonText();

            IntPtr dummy = this.Handle;
            NativeMethods.EnableDarkTitleBar(dummy);
            RegisterWakeListener();
            Task.Run(new Action(InitialBootAndRefresh));

            this.pollTimer = new System.Windows.Forms.Timer();
            this.pollTimer.Interval = 2000;
            this.pollTimer.Tick += delegate { TriggerRefreshAsync(false); };
            this.pollTimer.Start();
        }

        private float GetDpiScale()
        {
            using (Graphics g = this.CreateGraphics())
            {
                return Math.Max(1.0f, g.DpiX / 96f);
            }
        }

        private int S(int px)
        {
            return (int)Math.Round(px * GetDpiScale());
        }

        protected override void SetVisibleCore(bool value)
        {
            if (this.suppressInitialShow)
            {
                value = false;
                if (!this.IsHandleCreated) CreateHandle();
            }
            base.SetVisibleCore(value);
        }

        private void InitialBootAndRefresh()
        {
            EnsureNodeGatewayHealthy(true);
            Thread.Sleep(450);
            RefreshStatusCore();
        }

        private void RegisterWakeListener()
        {
            if (this.showEvent == null) return;
            Thread t = new Thread(delegate()
            {
                while (!this.IsDisposed)
                {
                    try
                    {
                        if (this.showEvent.WaitOne(1000))
                        {
                            if (this.IsHandleCreated && !this.IsDisposed)
                            {
                                this.BeginInvoke(new Action(ShowFromTray));
                            }
                        }
                    }
                    catch
                    {
                        break;
                    }
                }
            });
            t.IsBackground = true;
            t.Start();
        }

        private void BuildUi()
        {
            this.AutoScaleMode = AutoScaleMode.None;
            this.DoubleBuffered = true;
            this.Text = "DSB · DeepSeek Bot";
            this.Font = new Font("Microsoft YaHei UI", 9.5f, FontStyle.Regular);
            int winW = S(580);
            int winH = S(420);
            this.ClientSize = new Size(winW, winH);
            this.MinimumSize = new Size(winW, winH);
            this.FormBorderStyle = FormBorderStyle.FixedSingle;
            this.MaximizeBox = false;
            this.StartPosition = FormStartPosition.CenterScreen;
            this.BackColor = UiTheme.Bg;
            this.ForeColor = UiTheme.Ink;
            this.Icon = this.iconWarn;
            if (this.suppressInitialShow)
            {
                this.ShowInTaskbar = false;
                this.WindowState = FormWindowState.Minimized;
            }

            int pad = S(22);
            int innerW = winW - pad * 2;

            // Header row: sleek DSB badge + Title/Subtitle + live pill on right
            this.brandBadge = new DsbBrandBadgeBox();
            this.brandBadge.Location = new Point(pad, S(18));
            this.brandBadge.Size = new Size(S(40), S(40));
            this.Controls.Add(this.brandBadge);

            Label lblTitle = new Label();
            lblTitle.Text = "DSB · DeepSeek Bot";
            lblTitle.Font = new Font("Microsoft YaHei UI", 13.5f, FontStyle.Bold);
            lblTitle.ForeColor = UiTheme.Ink;
            lblTitle.AutoSize = true;
            lblTitle.Location = new Point(pad + S(52), S(16));
            this.Controls.Add(lblTitle);

            Label lblSub = new Label();
            lblSub.Text = "桌面常驻连接守护 · 127.0.0.1:" + GatewayPort + " · 直连/代理双路自愈";
            lblSub.Font = new Font("Microsoft YaHei UI", 8.5f, FontStyle.Regular);
            lblSub.ForeColor = UiTheme.Muted;
            lblSub.AutoSize = false;
            lblSub.Size = new Size(innerW - S(52), S(18));
            lblSub.AutoEllipsis = true;
            lblSub.Location = new Point(pad + S(52), S(42));
            this.Controls.Add(lblSub);

            // Status banner pill
            this.statusBannerCard = new RoundedCardPanel();
            this.statusBannerCard.Location = new Point(pad, S(72));
            this.statusBannerCard.Size = new Size(innerW, S(44));
            this.statusBannerCard.CornerRadius = 11;
            this.statusBannerCard.FillColor = Color.FromArgb(24, 28, 38);
            this.statusBannerCard.BorderColor = UiTheme.CardBorder;

            this.lblStatusDot = new Label();
            this.lblStatusDot.Text = "●";
            this.lblStatusDot.Font = new Font("Microsoft YaHei UI", 10.5f, FontStyle.Bold);
            this.lblStatusDot.ForeColor = UiTheme.Warn;
            this.lblStatusDot.AutoSize = true;
            this.lblStatusDot.Location = new Point(S(14), S(11));
            this.statusBannerCard.Controls.Add(this.lblStatusDot);

            this.lblStatusText = new Label();
            this.lblStatusText.Text = "正在握手本机常驻网关与云端中枢…";
            this.lblStatusText.Font = new Font("Microsoft YaHei UI", 9.5f, FontStyle.Bold);
            this.lblStatusText.ForeColor = UiTheme.Ink;
            this.lblStatusText.AutoSize = false;
            this.lblStatusText.AutoEllipsis = true;
            this.lblStatusText.Size = new Size(innerW - S(150), S(22));
            this.lblStatusText.Location = new Point(S(36), S(12));
            this.statusBannerCard.Controls.Add(this.lblStatusText);

            this.lblStatusSub = new Label();
            this.lblStatusSub.Text = "初始化";
            this.lblStatusSub.Font = new Font("Microsoft YaHei UI", 8.5f, FontStyle.Regular);
            this.lblStatusSub.ForeColor = UiTheme.Muted;
            this.lblStatusSub.TextAlign = ContentAlignment.MiddleRight;
            this.lblStatusSub.AutoSize = false;
            this.lblStatusSub.Size = new Size(S(102), S(22));
            this.lblStatusSub.Location = new Point(innerW - S(116), S(12));
            this.statusBannerCard.Controls.Add(this.lblStatusSub);
            this.Controls.Add(this.statusBannerCard);

            // Metrics glass card using clean TableLayoutPanel (zero clipping!)
            RoundedCardPanel card = new RoundedCardPanel();
            card.Location = new Point(pad, S(128));
            card.Size = new Size(innerW, S(172));
            card.CornerRadius = 12;

            TableLayoutPanel tbl = new TableLayoutPanel();
            tbl.Dock = DockStyle.Fill;
            tbl.BackColor = Color.Transparent;
            tbl.Padding = new Padding(S(16), S(12), S(16), S(10));
            tbl.ColumnCount = 2;
            tbl.RowCount = 5;
            tbl.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, S(86)));
            tbl.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100f));
            for (int r = 0; r < 4; r++) tbl.RowStyles.Add(new RowStyle(SizeType.Absolute, S(31)));
            tbl.RowStyles.Add(new RowStyle(SizeType.Percent, 100f));

            this.lblGatewayVal = AddMetricRow(tbl, 0, "本机网关", "正在检测 127.0.0.1:" + GatewayPort + "…");
            this.lblCloudVal = AddMetricRow(tbl, 1, "云端链路", "正在探测直连与代理通道…");
            this.lblPeersVal = AddMetricRow(tbl, 2, "跨端协同", "手机端检测中 · DeepSeek Bot 桌面端检测中");
            this.lblTrafficVal = AddMetricRow(tbl, 3, "会话流量", "本地会话统计中 · 累计心跳 0 次");

            this.lblErrorHint = new Label();
            this.lblErrorHint.Dock = DockStyle.Fill;
            this.lblErrorHint.Font = new Font("Microsoft YaHei UI", 8.2f, FontStyle.Regular);
            this.lblErrorHint.ForeColor = UiTheme.Err;
            this.lblErrorHint.AutoEllipsis = true;
            this.lblErrorHint.Text = "";
            tbl.Controls.Add(this.lblErrorHint, 0, 4);
            tbl.SetColumnSpan(this.lblErrorHint, 2);

            card.Controls.Add(tbl);
            this.Controls.Add(card);

            // Action button grid Row 1 (4 responsive equal pills)
            TableLayoutPanel btnRow1 = new TableLayoutPanel();
            btnRow1.Location = new Point(pad, S(314));
            btnRow1.Size = new Size(innerW, S(40));
            btnRow1.ColumnCount = 4;
            btnRow1.RowCount = 1;
            btnRow1.BackColor = Color.Transparent;
            for (int c = 0; c < 4; c++) btnRow1.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 25f));
            btnRow1.RowStyles.Add(new RowStyle(SizeType.Percent, 100f));

            this.btnReconnect = CreatePillBtn("立即重连", UiTheme.Accent, UiTheme.AccentHover, Color.White, false);
            this.btnReconnect.Margin = new Padding(0, 0, S(6), 0);
            this.btnReconnect.Click += delegate { ManualReconnectClicked(); };

            this.btnRestartCore = CreatePillBtn("重启内核", UiTheme.PillBg, UiTheme.PillHover, UiTheme.Ink, true);
            this.btnRestartCore.Margin = new Padding(S(2), 0, S(4), 0);
            this.btnRestartCore.Click += delegate { ManualRestartCoreClicked(); };

            this.btnWakeDsh = CreatePillBtn("打开桌面端", UiTheme.PillBg, UiTheme.PillHover, UiTheme.Ink, true);
            this.btnWakeDsh.Margin = new Padding(S(4), 0, S(2), 0);
            this.btnWakeDsh.Click += delegate { LaunchDshGui(); };

            this.btnOpenLog = CreatePillBtn("运行日志", UiTheme.PillBg, UiTheme.PillHover, UiTheme.Ink, true);
            this.btnOpenLog.Margin = new Padding(S(6), 0, 0, 0);
            this.btnOpenLog.Click += delegate { OpenLogFile(); };

            btnRow1.Controls.Add(this.btnReconnect, 0, 0);
            btnRow1.Controls.Add(this.btnRestartCore, 1, 0);
            btnRow1.Controls.Add(this.btnWakeDsh, 2, 0);
            btnRow1.Controls.Add(this.btnOpenLog, 3, 0);
            this.Controls.Add(btnRow1);

            // Action button grid Row 2 (Autostart toggle + Hide to tray)
            TableLayoutPanel btnRow2 = new TableLayoutPanel();
            btnRow2.Location = new Point(pad, S(362));
            btnRow2.Size = new Size(innerW, S(38));
            btnRow2.ColumnCount = 2;
            btnRow2.RowCount = 1;
            btnRow2.BackColor = Color.Transparent;
            btnRow2.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 64f));
            btnRow2.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 36f));
            btnRow2.RowStyles.Add(new RowStyle(SizeType.Percent, 100f));

            this.btnToggleAutostart = CreatePillBtn("开机后台自启：检测中", UiTheme.Card, UiTheme.PillHover, UiTheme.Muted, true);
            this.btnToggleAutostart.Margin = new Padding(0, 0, S(6), 0);
            this.btnToggleAutostart.Click += delegate { ToggleAutostart(); };

            this.btnHideToTray = CreatePillBtn("隐藏到托盘", UiTheme.Card, UiTheme.PillHover, UiTheme.Muted, true);
            this.btnHideToTray.Margin = new Padding(S(6), 0, 0, 0);
            this.btnHideToTray.Click += delegate { HideToTray(); };

            btnRow2.Controls.Add(this.btnToggleAutostart, 0, 0);
            btnRow2.Controls.Add(this.btnHideToTray, 1, 0);
            this.Controls.Add(btnRow2);
        }

        private Label AddMetricRow(TableLayoutPanel tbl, int rowIdx, string keyTitle, string initValue)
        {
            Label lblK = new Label();
            lblK.Text = keyTitle;
            lblK.Dock = DockStyle.Fill;
            lblK.TextAlign = ContentAlignment.MiddleLeft;
            lblK.Font = new Font("Microsoft YaHei UI", 8.8f, FontStyle.Regular);
            lblK.ForeColor = UiTheme.Muted;

            Label lblV = new Label();
            lblV.Text = initValue;
            lblV.Dock = DockStyle.Fill;
            lblV.TextAlign = ContentAlignment.MiddleLeft;
            lblV.AutoEllipsis = true;
            lblV.Font = new Font("Microsoft YaHei UI", 9.0f, FontStyle.Regular);
            lblV.ForeColor = UiTheme.Ink;

            tbl.Controls.Add(lblK, 0, rowIdx);
            tbl.Controls.Add(lblV, 1, rowIdx);
            return lblV;
        }

        private ModernPillButton CreatePillBtn(string text, Color bg, Color hover, Color fg, bool border)
        {
            ModernPillButton b = new ModernPillButton();
            b.Text = text;
            b.Dock = DockStyle.Fill;
            b.Font = new Font("Microsoft YaHei UI", 9.0f, FontStyle.Regular);
            b.BaseColor = bg;
            b.HoverColor = hover;
            b.ForeColor = fg;
            b.ShowBorder = border;
            return b;
        }

        private void SetupTray()
        {
            ContextMenu menu = new ContextMenu();
            menu.MenuItems.Add("打开 DSB · DeepSeek Bot 守护面板", delegate { ShowFromTray(); });
            menu.MenuItems.Add("立即重连云端", delegate { ManualReconnectClicked(); });
            menu.MenuItems.Add("重启网关内核 (18195)", delegate { ManualRestartCoreClicked(); });
            menu.MenuItems.Add("-");
            menu.MenuItems.Add("打开 DeepSeek Bot 桌面端", delegate { LaunchDshGui(); });
            menu.MenuItems.Add("查看运行日志 (sentinel.log)", delegate { OpenLogFile(); });
            menu.MenuItems.Add("-");
            menu.MenuItems.Add("彻底退出守护", delegate
            {
                this.allowRealExit = true;
                ShutdownNodeSentinelGracefully();
                this.trayIcon.Visible = false;
                Application.Exit();
            });

            this.trayIcon = new NotifyIcon();
            this.trayIcon.Icon = this.iconWarn;
            this.trayIcon.Text = "DSB · DeepSeek Bot 连接守护";
            this.trayIcon.ContextMenu = menu;
            this.trayIcon.Visible = true;
            this.trayIcon.DoubleClick += delegate { ShowFromTray(); };
        }

        private Icon CreateDsbStatusIcon(Color badgeColor)
        {
            using (Bitmap bmp = new Bitmap(64, 64))
            {
                using (Graphics g = Graphics.FromImage(bmp))
                {
                    g.SmoothingMode = SmoothingMode.HighQuality;
                    g.PixelOffsetMode = PixelOffsetMode.HighQuality;
                    g.Clear(Color.Transparent);

                    RectangleF bg = new RectangleF(4, 4, 52, 52);
                    using (GraphicsPath gp = UiTheme.RoundedRect(bg, 14f))
                    using (SolidBrush br = new SolidBrush(Color.FromArgb(18, 21, 29)))
                    using (Pen pen = new Pen(Color.FromArgb(46, 54, 72), 2.5f))
                    {
                        g.FillPath(br, gp);
                        g.DrawPath(pen, gp);
                    }
                    UiTheme.DrawDsbMark(g, 11f, 11f, 38f, UiTheme.AccentCyan);

                    using (SolidBrush rim = new SolidBrush(UiTheme.Bg))
                    {
                        g.FillEllipse(rim, 38, 38, 22, 22);
                    }
                    using (SolidBrush dot = new SolidBrush(badgeColor))
                    {
                        g.FillEllipse(dot, 42, 42, 14, 14);
                    }
                }
                IntPtr hIcon = bmp.GetHicon();
                Icon cloned = (Icon)Icon.FromHandle(hIcon).Clone();
                NativeMethods.DestroyIcon(hIcon);
                return cloned;
            }
        }

        private void SetIconState(int stateCode, string tooltip)
        {
            string safeTip = tooltip.Length > 60 ? tooltip.Substring(0, 60) : tooltip;
            if (this.trayIcon != null)
            {
                try { this.trayIcon.Text = safeTip; } catch { }
                if (this.currentIconState != stateCode)
                {
                    this.currentIconState = stateCode;
                    Icon target = stateCode == 2 ? this.iconOnline : (stateCode == 1 ? this.iconWarn : this.iconOffline);
                    this.trayIcon.Icon = target;
                    this.Icon = target;
                    if (this.brandBadge != null)
                    {
                        this.brandBadge.DotColor = stateCode == 2 ? UiTheme.Ok : (stateCode == 1 ? UiTheme.Warn : UiTheme.Err);
                        this.brandBadge.Invalidate();
                    }
                }
            }
        }

        protected override void OnFormClosing(FormClosingEventArgs e)
        {
            if (!this.allowRealExit && e.CloseReason == CloseReason.UserClosing)
            {
                e.Cancel = true;
                HideToTray();
                return;
            }
            base.OnFormClosing(e);
        }

        private void HideToTray()
        {
            this.suppressInitialShow = true;
            this.ShowInTaskbar = false;
            this.WindowState = FormWindowState.Minimized;
            this.Hide();
        }

        private void ShowFromTray()
        {
            this.suppressInitialShow = false;
            this.ShowInTaskbar = true;
            this.Show();
            this.WindowState = FormWindowState.Normal;
            this.Activate();
            NativeMethods.EnableDarkTitleBar(this.Handle);
            NativeMethods.SetForegroundWindow(this.Handle);
            TriggerRefreshAsync(false);
        }

        private string HttpRequestLocal(string url, string method, int timeoutMs)
        {
            HttpWebRequest req = (HttpWebRequest)WebRequest.Create(url);
            req.Method = method;
            req.Timeout = timeoutMs;
            req.ReadWriteTimeout = timeoutMs;
            req.KeepAlive = false;
            req.Proxy = null;
            if (method == "POST") req.ContentLength = 0;
            using (HttpWebResponse resp = (HttpWebResponse)req.GetResponse())
            using (Stream s = resp.GetResponseStream())
            using (StreamReader sr = new StreamReader(s, Encoding.UTF8))
            {
                return sr.ReadToEnd();
            }
        }

        private string ResolveNodeExePath()
        {
            string[] candidates = new string[]
            {
                @"C:\Program Files\nodejs\node.exe",
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "nodejs", "node.exe"),
                "node.exe"
            };
            foreach (string c in candidates)
            {
                if (c == "node.exe" || File.Exists(c)) return c;
            }
            return "node.exe";
        }

        private void KillWedgedSentinelNode()
        {
            try
            {
                string ctrlRaw = HttpRequestLocal(string.Format("http://127.0.0.1:{0}/", ControlPort), "GET", 800);
                Dictionary<string, object> ctrlObj = this.json.Deserialize<Dictionary<string, object>>(ctrlRaw);
                if (ctrlObj != null && ctrlObj.ContainsKey("pid"))
                {
                    int pid = Convert.ToInt32(ctrlObj["pid"]);
                    if (pid > 0) { try { Process.GetProcessById(pid).Kill(); } catch { } }
                }
            }
            catch { }

            if (this.lastKnownSentinelPid > 0)
            {
                try { Process.GetProcessById(this.lastKnownSentinelPid).Kill(); } catch { }
            }
        }

        private void ShutdownNodeSentinelGracefully()
        {
            try { HttpRequestLocal(string.Format("http://127.0.0.1:{0}/shutdown", ControlPort), "POST", 800); }
            catch { KillWedgedSentinelNode(); }
        }

        private void SpawnNodeSentinelProcess()
        {
            if (!File.Exists(this.sentinelScriptPath)) return;
            if ((DateTime.UtcNow - this.lastSpawnAttemptUtc).TotalSeconds < 2.5) return;
            this.lastSpawnAttemptUtc = DateTime.UtcNow;

            ProcessStartInfo psi = new ProcessStartInfo();
            psi.FileName = ResolveNodeExePath();
            psi.Arguments = "\"" + this.sentinelScriptPath + "\"";
            psi.WorkingDirectory = this.pluginDir;
            psi.CreateNoWindow = true;
            psi.UseShellExecute = false;
            psi.WindowStyle = ProcessWindowStyle.Hidden;
            try
            {
                Process p = Process.Start(psi);
                if (p != null) this.lastKnownSentinelPid = p.Id;
            }
            catch { }
        }

        private bool EnsureNodeGatewayHealthy(bool forceSpawnIfDown)
        {
            try
            {
                string raw = HttpRequestLocal(string.Format("http://127.0.0.1:{0}/status", GatewayPort), "GET", 1200);
                Dictionary<string, object> data = this.json.Deserialize<Dictionary<string, object>>(raw);
                if (data != null && GetBool(data, "ok") && GetBool(data, "bridge_running"))
                {
                    this.consecutiveGatewayFailures = 0;
                    int pid = GetInt(data, "pid");
                    if (pid > 0) this.lastKnownSentinelPid = pid;
                    return true;
                }
            }
            catch { }

            this.consecutiveGatewayFailures++;
            if (forceSpawnIfDown || this.consecutiveGatewayFailures >= 2)
            {
                if (this.consecutiveGatewayFailures >= 2)
                {
                    KillWedgedSentinelNode();
                    Thread.Sleep(150);
                }
                SpawnNodeSentinelProcess();
                this.consecutiveGatewayFailures = 0;
            }
            return false;
        }

        private void TriggerRefreshAsync(bool forceSpawn)
        {
            if (this.pollBusy) return;
            this.pollBusy = true;
            Task.Run(delegate
            {
                try
                {
                    if (forceSpawn) EnsureNodeGatewayHealthy(true);
                    RefreshStatusCore();
                }
                finally { this.pollBusy = false; }
            });
        }

        private void RefreshStatusCore()
        {
            Dictionary<string, object> st = null;
            try
            {
                string raw = HttpRequestLocal(string.Format("http://127.0.0.1:{0}/status", GatewayPort), "GET", 1400);
                st = this.json.Deserialize<Dictionary<string, object>>(raw);
            }
            catch { st = null; }

            if (st == null || !GetBool(st, "ok"))
            {
                EnsureNodeGatewayHealthy(false);
                UpdateUiSafe(delegate
                {
                    this.lblStatusDot.ForeColor = UiTheme.Err;
                    this.lblStatusText.Text = "本机网关未响应 · 正在自动拉起恢复…";
                    this.lblStatusSub.Text = "自愈中";
                    this.lblGatewayVal.Text = "127.0.0.1:" + GatewayPort + " 正在重启恢复";
                    this.lblCloudVal.Text = "等待本机网关就绪";
                    this.lblErrorHint.Text = "提示：若持续未恢复，可点击「重启内核」或查看日志";
                    SetIconState(0, "DSB · 桌面守护 (正在重启本机网关…)");
                });
                return;
            }

            this.consecutiveGatewayFailures = 0;
            int pid = GetInt(st, "pid");
            if (pid > 0) this.lastKnownSentinelPid = pid;
            bool vpsOnline = GetBool(st, "vps_online");
            bool phoneOnline = GetBool(st, "phone_online");
            bool dshAlive = GetBool(st, "dsh_host_alive") || IsDshProcessRunning();
            string route = GetStr(st, "active_route", "direct");
            int rtt = GetInt(st, "last_rtt_ms");
            int hbCount = GetInt(st, "heartbeat_count");
            string lastHb = GetStr(st, "last_heartbeat_at", "--");
            string lastErr = GetStr(st, "last_error", "");
            int sessions = GetInt(st, "sessions_count");
            int runningSessions = GetInt(st, "running_sessions_count");
            long totalBytes = GetLong(st, "bytes_sent") + GetLong(st, "bytes_received");

            if (!vpsOnline)
            {
                this.consecutiveCloudOfflineTicks++;
                if (this.consecutiveCloudOfflineTicks >= 12 && (DateTime.UtcNow - this.lastAutoReconnectUtc).TotalSeconds > 30)
                {
                    this.lastAutoReconnectUtc = DateTime.UtcNow;
                    try { HttpRequestLocal(string.Format("http://127.0.0.1:{0}/local/reconnect", GatewayPort), "POST", 1200); } catch { }
                }
            }
            else { this.consecutiveCloudOfflineTicks = 0; }

            UpdateUiSafe(delegate
            {
                bool isProxy = route.StartsWith("proxy");
                string routeShort = isProxy ? route.Replace("proxy ", "") : "Direct";
                string routeLabel = isProxy ? "代理 (" + routeShort + ")" : "高速直连 (Direct)";
                if (vpsOnline)
                {
                    this.lblStatusDot.ForeColor = UiTheme.Ok;
                    this.lblStatusText.Text = "云端在线守护中 · " + routeLabel;
                    this.lblStatusSub.Text = rtt > 0 ? (rtt + " ms") : "已连接";
                    SetIconState(2, string.Format("DSB · DeepSeek Bot 在线 ({0}, {1}ms)", isProxy ? "代理" : "直连", rtt));
                }
                else
                {
                    this.lblStatusDot.ForeColor = UiTheme.Warn;
                    this.lblStatusText.Text = "本机网关就绪 · 正在重连云端…";
                    this.lblStatusSub.Text = "重连中";
                    SetIconState(1, "DSB · 本机网关就绪 (正在重连云端…)");
                }

                this.lblGatewayVal.Text = string.Format("127.0.0.1:{0} · 常驻正常 (PID {1})", GatewayPort, pid);
                this.lblCloudVal.Text = string.Format("{0} · 心跳 {1} (#{2})", routeLabel, lastHb, hbCount);
                this.lblPeersVal.Text = string.Format(
                    "手机端：{0}   ·   桌面端：{1}",
                    phoneOnline ? "在线" : "待机",
                    dshAlive ? "运行中" : "后台守护在线"
                );
                this.lblTrafficVal.Text = string.Format(
                    "本地会话 {0} 个 ({1} 运行中) · 收发 {2:0.0} KB",
                    sessions,
                    runningSessions,
                    totalBytes / 1024.0
                );
                this.lblErrorHint.Text = (!vpsOnline && !string.IsNullOrEmpty(lastErr))
                    ? ("诊断：" + (lastErr.Length > 64 ? lastErr.Substring(0, 64) + "…" : lastErr))
                    : "";
            });
        }

        private void ManualReconnectClicked()
        {
            this.lblStatusText.Text = "正在重新探测直连/代理路由并握手云端…";
            this.lblStatusSub.Text = "重连中";
            Task.Run(delegate
            {
                try { HttpRequestLocal(string.Format("http://127.0.0.1:{0}/local/reconnect", GatewayPort), "POST", 1500); }
                catch { EnsureNodeGatewayHealthy(true); }
                Thread.Sleep(600);
                RefreshStatusCore();
            });
        }

        private void ManualRestartCoreClicked()
        {
            this.lblStatusText.Text = "正在重启常驻网关内核 (sentinel.mjs)…";
            this.lblStatusSub.Text = "重启中";
            Task.Run(delegate
            {
                ShutdownNodeSentinelGracefully();
                Thread.Sleep(350);
                KillWedgedSentinelNode();
                this.lastSpawnAttemptUtc = DateTime.MinValue;
                SpawnNodeSentinelProcess();
                Thread.Sleep(900);
                RefreshStatusCore();
            });
        }

        private bool IsDshProcessRunning()
        {
            try { return Process.GetProcessesByName("DeepSeek Harness").Length > 0; }
            catch { return false; }
        }

        private void LaunchDshGui()
        {
            string[] candidates = new string[]
            {
                @"D:\dsh\DeepSeek Harness.exe",
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", "DeepSeek Harness", "DeepSeek Harness.exe")
            };
            foreach (string exe in candidates)
            {
                if (File.Exists(exe))
                {
                    try
                    {
                        ProcessStartInfo psi = new ProcessStartInfo(exe);
                        psi.WorkingDirectory = Path.GetDirectoryName(exe);
                        psi.UseShellExecute = true;
                        Process.Start(psi);
                        return;
                    }
                    catch { }
                }
            }
            MessageBox.Show("未找到桌面客户端主程序，请检查安装目录。", "DSB · DeepSeek Bot", MessageBoxButtons.OK, MessageBoxIcon.Information);
        }

        private void OpenLogFile()
        {
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(this.logFilePath));
                if (!File.Exists(this.logFilePath)) File.WriteAllText(this.logFilePath, "", Encoding.UTF8);
                Process.Start("notepad.exe", "\"" + this.logFilePath + "\"");
            }
            catch { }
        }

        private bool IsAutostartEnabled()
        {
            try
            {
                using (RegistryKey k = Registry.CurrentUser.OpenSubKey(RunRegPath, false))
                {
                    if (k == null) return false;
                    object val = k.GetValue(RunRegName);
                    return val != null && val.ToString().IndexOf("DeepSeekClawSentinel", StringComparison.OrdinalIgnoreCase) >= 0;
                }
            }
            catch { return false; }
        }

        private void EnsureAutostartSynced()
        {
            try
            {
                if (IsAutostartEnabled())
                {
                    using (RegistryKey k = Registry.CurrentUser.OpenSubKey(RunRegPath, true))
                    {
                        if (k != null) k.SetValue(RunRegName, "\"" + Application.ExecutablePath + "\" --minimized");
                    }
                }
            }
            catch { }
        }

        private void ToggleAutostart()
        {
            try
            {
                bool currentlyOn = IsAutostartEnabled();
                using (RegistryKey k = Registry.CurrentUser.OpenSubKey(RunRegPath, true))
                {
                    if (k != null)
                    {
                        if (currentlyOn) k.DeleteValue(RunRegName, false);
                        else k.SetValue(RunRegName, "\"" + Application.ExecutablePath + "\" --minimized");
                    }
                }
            }
            catch { }
            UpdateAutostartButtonText();
        }

        private void UpdateAutostartButtonText()
        {
            bool on = IsAutostartEnabled();
            this.btnToggleAutostart.Text = on ? "开机后台自启：已开启" : "开机后台自启：已关闭";
            this.btnToggleAutostart.ForeColor = on ? UiTheme.Ok : UiTheme.Muted;
        }

        private void UpdateUiSafe(Action action)
        {
            if (this.IsDisposed) return;
            try
            {
                if (this.InvokeRequired) this.BeginInvoke(action);
                else action();
            }
            catch { }
        }

        private static bool GetBool(Dictionary<string, object> d, string k)
        {
            object v;
            if (d == null || !d.TryGetValue(k, out v) || v == null) return false;
            if (v is bool) return (bool)v;
            return string.Equals(v.ToString(), "true", StringComparison.OrdinalIgnoreCase);
        }

        private static int GetInt(Dictionary<string, object> d, string k)
        {
            object v;
            if (d == null || !d.TryGetValue(k, out v) || v == null) return 0;
            try { return Convert.ToInt32(v); } catch { return 0; }
        }

        private static long GetLong(Dictionary<string, object> d, string k)
        {
            object v;
            if (d == null || !d.TryGetValue(k, out v) || v == null) return 0L;
            try { return Convert.ToInt64(v); } catch { return 0L; }
        }

        private static string GetStr(Dictionary<string, object> d, string k, string defVal)
        {
            object v;
            if (d == null || !d.TryGetValue(k, out v) || v == null) return defVal;
            return v.ToString();
        }
    }

    internal static class Program
    {
        [STAThread]
        private static void Main(string[] args)
        {
            try
            {
                // DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
                if (!NativeMethods.SetProcessDpiAwarenessContext(new IntPtr(-4)))
                {
                    NativeMethods.SetProcessDPIAware();
                }
            }
            catch
            {
                try { NativeMethods.SetProcessDPIAware(); } catch { }
            }

            bool minimized = false;
            if (args != null)
            {
                foreach (string a in args)
                {
                    if (string.Equals(a, "--minimized", StringComparison.OrdinalIgnoreCase) ||
                        string.Equals(a, "-m", StringComparison.OrdinalIgnoreCase))
                    {
                        minimized = true;
                    }
                }
            }

            bool createdNew = false;
            EventWaitHandle wakeEvent = new EventWaitHandle(false, EventResetMode.AutoReset, @"Local\DeepSeekClawSentinel_ShowEvent_v3");
            using (Mutex singleMutex = new Mutex(true, @"Local\DeepSeekClawSentinel_Mutex_v3", out createdNew))
            {
                if (!createdNew)
                {
                    if (!minimized)
                    {
                        try { wakeEvent.Set(); } catch { }
                    }
                    return;
                }

                ServicePointManager.Expect100Continue = false;
                ServicePointManager.DefaultConnectionLimit = 16;
                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                Application.Run(new SentinelForm(minimized, wakeEvent));
            }
        }
    }
}
