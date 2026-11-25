$ErrorActionPreference = "Stop"
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$csCode = @'
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Automation;
using System.Windows.Forms;

public static class DsbWin32Cua {
    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }

    [StructLayout(LayoutKind.Sequential)]
    public struct INPUT {
        public uint type;
        public INPUTUNION U;
    }

    [StructLayout(LayoutKind.Explicit)]
    public struct INPUTUNION {
        [FieldOffset(0)] public MOUSEINPUT mi;
        [FieldOffset(0)] public KEYBDINPUT ki;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct MOUSEINPUT {
        public int dx;
        public int dy;
        public int mouseData;
        public uint dwFlags;
        public uint time;
        public IntPtr dwExtraInfo;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct KEYBDINPUT {
        public ushort wVk;
        public ushort wScan;
        public uint dwFlags;
        public uint time;
        public IntPtr dwExtraInfo;
    }

    private delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    [DllImport("user32.dll")] private static extern bool SetProcessDpiAwarenessContext(IntPtr value);
    [DllImport("user32.dll")] private static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] private static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] private static extern bool IsIconic(IntPtr hWnd);
    [DllImport("user32.dll")] private static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] private static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] private static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);
    [DllImport("user32.dll")] private static extern bool PrintWindow(IntPtr hwnd, IntPtr hdcBlt, uint nFlags);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetWindowTextLength(IntPtr hWnd);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);
    [DllImport("user32.dll")] private static extern IntPtr GetShellWindow();
    [DllImport("user32.dll")] private static extern uint SendInput(uint nInputs, INPUT[] pInputs, int cbSize);
    [DllImport("user32.dll")] private static extern int GetSystemMetrics(int nIndex);
    [DllImport("dwmapi.dll")] private static extern int DwmGetWindowAttribute(IntPtr hwnd, int dwAttribute, out RECT pvAttribute, int cbAttribute);
    [DllImport("dwmapi.dll")] private static extern int DwmGetWindowAttribute(IntPtr hwnd, int dwAttribute, out int pvAttribute, int cbAttribute);

    private const uint INPUT_MOUSE = 0;
    private const uint INPUT_KEYBOARD = 1;
    private const uint MOUSEEVENTF_MOVE = 0x0001;
    private const uint MOUSEEVENTF_LEFTDOWN = 0x0002;
    private const uint MOUSEEVENTF_LEFTUP = 0x0004;
    private const uint MOUSEEVENTF_RIGHTDOWN = 0x0008;
    private const uint MOUSEEVENTF_RIGHTUP = 0x0010;
    private const uint MOUSEEVENTF_MIDDLEDOWN = 0x0020;
    private const uint MOUSEEVENTF_MIDDLEUP = 0x0040;
    private const uint MOUSEEVENTF_WHEEL = 0x0800;
    private const uint MOUSEEVENTF_HWHEEL = 0x1000;
    private const uint MOUSEEVENTF_ABSOLUTE = 0x8000;
    private const uint MOUSEEVENTF_VIRTUALDESK = 0x4000;
    private const uint KEYEVENTF_KEYUP = 0x0002;
    private const int SW_RESTORE = 9;
    private const int DWMWA_EXTENDED_FRAME_BOUNDS = 9;
    private const int DWMWA_CLOAKED = 14;

    static DsbWin32Cua() {
        try {
            if (!SetProcessDpiAwarenessContext(new IntPtr(-4))) {
                SetProcessDPIAware();
            }
        } catch {
            try { SetProcessDPIAware(); } catch {}
        }
    }

    public static void Init() {}

    public static RECT GetTrueRect(long winId) {
        if (winId <= 0) {
            int vx = GetSystemMetrics(76);
            int vy = GetSystemMetrics(77);
            int vw = Math.Max(1, GetSystemMetrics(78));
            int vh = Math.Max(1, GetSystemMetrics(79));
            return new RECT { Left = vx, Top = vy, Right = vx + vw, Bottom = vy + vh };
        }
        IntPtr h = new IntPtr(winId);
        RECT r;
        if (DwmGetWindowAttribute(h, DWMWA_EXTENDED_FRAME_BOUNDS, out r, Marshal.SizeOf(typeof(RECT))) == 0 && r.Right > r.Left && r.Bottom > r.Top) {
            return r;
        }
        GetWindowRect(h, out r);
        return r;
    }

    public static List<Dictionary<string, object>> ListWindows() {
        var result = new List<Dictionary<string, object>>();
        RECT vr = GetTrueRect(0);
        result.Add(new Dictionary<string, object> {
            { "id", 0L },
            { "title", "Windows Desktop (Full Screen)" },
            { "app", "desktop" },
            { "x", vr.Left },
            { "y", vr.Top },
            { "width", Math.Max(1, vr.Right - vr.Left) },
            { "height", Math.Max(1, vr.Bottom - vr.Top) },
            { "is_minimized", false },
        });
        IntPtr shell = GetShellWindow();
        EnumWindows((hWnd, lParam) => {
            if (hWnd == shell || !IsWindowVisible(hWnd)) return true;
            int cloaked = 0;
            if (DwmGetWindowAttribute(hWnd, DWMWA_CLOAKED, out cloaked, sizeof(int)) == 0 && cloaked != 0) return true;
            int len = GetWindowTextLength(hWnd);
            if (len <= 0) return true;
            var sb = new StringBuilder(len + 1);
            GetWindowText(hWnd, sb, sb.Capacity);
            string title = sb.ToString().Trim();
            if (string.IsNullOrEmpty(title)) return true;
            RECT r = GetTrueRect(hWnd.ToInt64());
            int w = r.Right - r.Left;
            int h = r.Bottom - r.Top;
            if (w < 60 || h < 40) return true;
            uint pid = 0;
            GetWindowThreadProcessId(hWnd, out pid);
            string appName = "app";
            try { appName = Process.GetProcessById((int)pid).ProcessName; } catch {}
            result.Add(new Dictionary<string, object> {
                { "id", hWnd.ToInt64() },
                { "title", title },
                { "app", appName },
                { "pid", (int)pid },
                { "x", r.Left },
                { "y", r.Top },
                { "width", w },
                { "height", h },
                { "is_minimized", IsIconic(hWnd) },
            });
            return true;
        }, IntPtr.Zero);
        return result;
    }

    public static Dictionary<string, object> GetWindowMeta(long winId) {
        if (winId <= 0) {
            RECT vr = GetTrueRect(0);
            return new Dictionary<string, object> {
                { "id", 0L }, { "title", "Windows Desktop (Full Screen)" }, { "app", "desktop" },
                { "x", vr.Left }, { "y", vr.Top }, { "width", Math.Max(1, vr.Right - vr.Left) }, { "height", Math.Max(1, vr.Bottom - vr.Top) }, { "is_minimized", false }
            };
        }
        IntPtr h = new IntPtr(winId);
        int len = GetWindowTextLength(h);
        var sb = new StringBuilder(Math.Max(1, len + 1));
        if (len > 0) GetWindowText(h, sb, sb.Capacity);
        uint pid = 0;
        GetWindowThreadProcessId(h, out pid);
        string appName = "app";
        try { appName = Process.GetProcessById((int)pid).ProcessName; } catch {}
        RECT r = GetTrueRect(winId);
        return new Dictionary<string, object> {
            { "id", winId }, { "title", sb.ToString() }, { "app", appName },
            { "x", r.Left }, { "y", r.Top }, { "width", Math.Max(1, r.Right - r.Left) }, { "height", Math.Max(1, r.Bottom - r.Top) }, { "is_minimized", IsIconic(h) }
        };
    }

    public static void ActivateWindow(long winId) {
        if (winId <= 0) return;
        IntPtr h = new IntPtr(winId);
        if (IsIconic(h)) ShowWindow(h, SW_RESTORE);
        SetForegroundWindow(h);
        Thread.Sleep(90);
    }

    private static ImageCodecInfo GetJpegEncoder() {
        foreach (var codec in ImageCodecInfo.GetImageEncoders()) {
            if (codec.FormatID == ImageFormat.Jpeg.Guid) return codec;
        }
        return null;
    }

    private static void DrawEdgeRuler(Bitmap bmp) {
        int w = bmp.Width;
        int h = bmp.Height;
        using (Graphics g = Graphics.FromImage(bmp)) {
            g.SmoothingMode = SmoothingMode.AntiAlias;
            using (var font = new Font("Consolas", 8f, FontStyle.Bold))
            using (var bgBrush = new SolidBrush(Color.FromArgb(145, 15, 20, 28)))
            using (var fgBrushX = new SolidBrush(Color.FromArgb(220, 255, 232, 130)))
            using (var fgBrushY = new SolidBrush(Color.FromArgb(220, 145, 240, 255)))
            using (var tickPen = new Pen(Color.FromArgb(190, 110, 225, 255), 1f)) {
                for (int n = 100; n <= 900; n += 100) {
                    int px = (int)Math.Round(n * (w - 1) / 1000.0);
                    string lbl = n.ToString();
                    g.DrawLine(tickPen, px, 0, px, 4);
                    g.DrawLine(tickPen, px, Math.Max(0, h - 5), px, h - 1);
                    int tw = lbl.Length * 7 + 4;
                    int bx = Math.Max(0, Math.Min(w - tw, px - tw / 2));
                    g.FillRectangle(bgBrush, bx, 0, tw, 13);
                    g.DrawString(lbl, font, fgBrushX, bx + 1, 0);
                    g.FillRectangle(bgBrush, bx, Math.Max(0, h - 13), tw, 13);
                    g.DrawString(lbl, font, fgBrushX, bx + 1, Math.Max(0, h - 13));

                    int py = (int)Math.Round(n * (h - 1) / 1000.0);
                    g.DrawLine(tickPen, 0, py, 4, py);
                    g.DrawLine(tickPen, Math.Max(0, w - 5), py, w - 1, py);
                    int by = Math.Max(14, Math.Min(Math.Max(14, h - 27), py - 6));
                    g.FillRectangle(bgBrush, 0, by, tw, 12);
                    g.DrawString(lbl, font, fgBrushY, 1, by - 1);
                    g.FillRectangle(bgBrush, Math.Max(0, w - tw), by, tw, 12);
                    g.DrawString(lbl, font, fgBrushY, Math.Max(0, w - tw + 1), by - 1);
                }
            }
        }
    }

    public static Dictionary<string, object> CaptureWindowState(long winId, bool includeScreenshot) {
        if (winId > 0 && IsIconic(new IntPtr(winId))) {
            ActivateWindow(winId);
        }
        RECT r = GetTrueRect(winId);
        int w = Math.Max(1, r.Right - r.Left);
        int h = Math.Max(1, r.Bottom - r.Top);
        string b64 = "";
        string captureNote = "";
        if (includeScreenshot) {
            using (var bmp = new Bitmap(w, h, PixelFormat.Format32bppArgb)) {
                using (var g = Graphics.FromImage(bmp)) {
                    bool captured = false;
                    if (winId > 0) {
                        IntPtr hdc = g.GetHdc();
                        try { captured = PrintWindow(new IntPtr(winId), hdc, 2); }
                        catch {}
                        finally { g.ReleaseHdc(hdc); }
                    }
                    if (!captured) {
                        try {
                            g.CopyFromScreen(r.Left, r.Top, 0, 0, new Size(w, h), CopyPixelOperation.SourceCopy);
                            captured = true;
                        } catch (Exception ex) {
                            g.Clear(Color.FromArgb(255, 18, 22, 30));
                            captureNote = "screen_dc_unavailable_or_locked: " + ex.Message;
                        }
                    }
                }
                DrawEdgeRuler(bmp);
                using (var ms = new MemoryStream()) {
                    var enc = GetJpegEncoder();
                    if (enc != null) {
                        var eps = new EncoderParameters(1);
                        eps.Param[0] = new EncoderParameter(System.Drawing.Imaging.Encoder.Quality, 86L);
                        bmp.Save(ms, enc, eps);
                    } else {
                        bmp.Save(ms, ImageFormat.Png);
                    }
                    b64 = Convert.ToBase64String(ms.ToArray());
                }
            }
        }
        string focused = "";
        try {
            var el = AutomationElement.FocusedElement;
            if (el != null) {
                focused = el.Current.ControlType.ProgrammaticName.Replace("ControlType.", "") + ":" + (el.Current.Name ?? "");
            }
        } catch {}
        return new Dictionary<string, object> {
            { "window", GetWindowMeta(winId) },
            { "screenshot_b64", b64 },
            { "mime", "image/jpeg" },
            { "width", w },
            { "height", h },
            { "focused_element", focused },
            { "capture_note", captureNote }
        };
    }

    private static void ToAbsoluteVirtual(int sx, int sy, out int ax, out int ay) {
        int vx = GetSystemMetrics(76);
        int vy = GetSystemMetrics(77);
        int vw = Math.Max(2, GetSystemMetrics(78));
        int vh = Math.Max(2, GetSystemMetrics(79));
        ax = (int)Math.Round(((sx - vx) * 65535.0) / (vw - 1));
        ay = (int)Math.Round(((sy - vy) * 65535.0) / (vh - 1));
    }

    private static void SendMouseFlags(int sx, int sy, uint flags, int data) {
        int ax, ay;
        ToAbsoluteVirtual(sx, sy, out ax, out ay);
        var inp = new INPUT[1];
        inp[0].type = INPUT_MOUSE;
        inp[0].U.mi = new MOUSEINPUT { dx = ax, dy = ay, mouseData = data, dwFlags = flags | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, time = 0, dwExtraInfo = IntPtr.Zero };
        SendInput(1, inp, Marshal.SizeOf(typeof(INPUT)));
    }

    public static void MouseClick(long winId, int relX, int relY, string btn, int count) {
        if (winId > 0) ActivateWindow(winId);
        RECT r = GetTrueRect(winId);
        int sx = Math.Max(r.Left, Math.Min(r.Right - 1, r.Left + relX));
        int sy = Math.Max(r.Top, Math.Min(r.Bottom - 1, r.Top + relY));
        uint downF = MOUSEEVENTF_LEFTDOWN, upF = MOUSEEVENTF_LEFTUP;
        if (btn == "right") { downF = MOUSEEVENTF_RIGHTDOWN; upF = MOUSEEVENTF_RIGHTUP; }
        else if (btn == "middle") { downF = MOUSEEVENTF_MIDDLEDOWN; upF = MOUSEEVENTF_MIDDLEUP; }
        SendMouseFlags(sx, sy, MOUSEEVENTF_MOVE, 0);
        Thread.Sleep(25);
        for (int i = 0; i < Math.Max(1, count); i++) {
            SendMouseFlags(sx, sy, downF, 0);
            Thread.Sleep(20);
            SendMouseFlags(sx, sy, upF, 0);
            if (i + 1 < count) Thread.Sleep(55);
        }
    }

    public static void MouseScroll(long winId, int relX, int relY, int scrollX, int scrollY) {
        RECT r = GetTrueRect(winId);
        int sx = Math.Max(r.Left, Math.Min(r.Right - 1, r.Left + relX));
        int sy = Math.Max(r.Top, Math.Min(r.Bottom - 1, r.Top + relY));
        SendMouseFlags(sx, sy, MOUSEEVENTF_MOVE, 0);
        Thread.Sleep(20);
        if (scrollY != 0) SendMouseFlags(sx, sy, MOUSEEVENTF_WHEEL, -scrollY * 120);
        if (scrollX != 0) SendMouseFlags(sx, sy, MOUSEEVENTF_HWHEEL, scrollX * 120);
    }

    public static void MouseDrag(long winId, int fx, int fy, int tx, int ty) {
        if (winId > 0) ActivateWindow(winId);
        RECT r = GetTrueRect(winId);
        int sx1 = r.Left + fx, sy1 = r.Top + fy;
        int sx2 = r.Left + tx, sy2 = r.Top + ty;
        SendMouseFlags(sx1, sy1, MOUSEEVENTF_MOVE, 0);
        Thread.Sleep(30);
        SendMouseFlags(sx1, sy1, MOUSEEVENTF_LEFTDOWN, 0);
        Thread.Sleep(45);
        for (int step = 1; step <= 8; step++) {
            int ix = sx1 + ((sx2 - sx1) * step) / 8;
            int iy = sy1 + ((sy2 - sy1) * step) / 8;
            SendMouseFlags(ix, iy, MOUSEEVENTF_MOVE, 0);
            Thread.Sleep(16);
        }
        SendMouseFlags(sx2, sy2, MOUSEEVENTF_LEFTUP, 0);
    }

    private static void SendKeyVk(ushort vk, bool up) {
        var inp = new INPUT[1];
        inp[0].type = INPUT_KEYBOARD;
        inp[0].U.ki = new KEYBDINPUT { wVk = vk, wScan = 0, dwFlags = up ? KEYEVENTF_KEYUP : 0, time = 0, dwExtraInfo = IntPtr.Zero };
        SendInput(1, inp, Marshal.SizeOf(typeof(INPUT)));
    }

    private static ushort MapKeyToken(string tok) {
        string t = (tok ?? "").Trim().ToLowerInvariant();
        switch (t) {
            case "ctrl": case "control": case "control_l": case "lcontrol": return 0x11;
            case "alt": case "alt_l": case "menu": return 0x12;
            case "shift": case "shift_l": return 0x10;
            case "win": case "super": case "super_l": return 0x5B;
            case "return": case "enter": return 0x0D;
            case "tab": return 0x09;
            case "escape": case "esc": return 0x1B;
            case "backspace": return 0x08;
            case "delete": case "del": return 0x2E;
            case "space": return 0x20;
            case "up": return 0x26;
            case "down": return 0x28;
            case "left": return 0x25;
            case "right": return 0x27;
            case "home": return 0x24;
            case "end": return 0x23;
            case "prior": case "page_up": case "pageup": return 0x21;
            case "next": case "page_down": case "pagedown": return 0x22;
            case "f1": return 0x70; case "f2": return 0x71; case "f3": return 0x72; case "f4": return 0x73;
            case "f5": return 0x74; case "f6": return 0x75; case "f7": return 0x76; case "f8": return 0x77;
            case "f9": return 0x78; case "f10": return 0x79; case "f11": return 0x7A; case "f12": return 0x7B;
            default:
                if (t.Length == 1) {
                    char ch = char.ToUpperInvariant(t[0]);
                    if ((ch >= 'A' && ch <= 'Z') || (ch >= '0' && ch <= '9')) return (ushort)ch;
                }
                throw new ArgumentException("Unknown key token: " + tok);
        }
    }

    public static void PressKeyChord(long winId, string chord) {
        if (winId > 0) ActivateWindow(winId);
        var parts = (chord ?? "Return").Split('+');
        var vks = new List<ushort>();
        foreach (var p in parts) {
            if (!string.IsNullOrWhiteSpace(p)) vks.Add(MapKeyToken(p));
        }
        foreach (var vk in vks) { SendKeyVk(vk, false); Thread.Sleep(15); }
        Thread.Sleep(25);
        for (int i = vks.Count - 1; i >= 0; i--) { SendKeyVk(vks[i], true); Thread.Sleep(12); }
    }

    public static void PasteText(long winId, string txt) {
        if (winId > 0) ActivateWindow(winId);
        if (string.IsNullOrEmpty(txt)) return;
        string prev = null;
        bool hadText = false;
        try {
            if (Clipboard.ContainsText()) {
                hadText = true;
                prev = Clipboard.GetText();
            }
        } catch {}
        Clipboard.SetText(txt, TextDataFormat.UnicodeText);
        Thread.Sleep(35);
        PressKeyChord(0, "Control_L+v");
        Thread.Sleep(110);
        if (hadText && prev != null) {
            try { Clipboard.SetText(prev, TextDataFormat.UnicodeText); } catch {}
        }
    }
}
'@

Add-Type -TypeDefinition $csCode -ReferencedAssemblies @("System.Drawing", "System.Windows.Forms", "UIAutomationClient", "UIAutomationTypes")
[DsbWin32Cua]::Init()
[Console]::Out.WriteLine("DSB_WIN32_READY")
[Console]::Out.Flush()

while ($null -ne ($line = [Console]::In.ReadLine())) {
    $line = $line.Trim()
    if (-not $line) { continue }
    $reqId = $null
    try {
        $req = ConvertFrom-Json -InputObject $line
        $reqId = $req.id
        $method = [string]$req.method
        $p = $req.params
        $res = $null
        switch ($method) {
            "list_windows" { $res = [DsbWin32Cua]::ListWindows() }
            "get_window" { $res = [DsbWin32Cua]::GetWindowMeta([int64]$p.id) }
            "activate_window" { [DsbWin32Cua]::ActivateWindow([int64]$p.window_id); $res = @{ ok = $true } }
            "get_window_state" {
                $incShot = $true
                if ($null -ne $p.include_screenshot) { $incShot = [bool]$p.include_screenshot }
                $res = [DsbWin32Cua]::CaptureWindowState([int64]$p.window_id, $incShot)
            }
            "click" {
                $btn = "left"
                if ($p.mouse_button) { $btn = [string]$p.mouse_button }
                $cnt = 1
                if ($null -ne $p.click_count) { $cnt = [int]$p.click_count }
                [DsbWin32Cua]::MouseClick([int64]$p.window_id, [int]$p.x, [int]$p.y, $btn, $cnt)
                $res = @{ ok = $true }
            }
            "scroll" {
                $sx = 0
                if ($null -ne $p.scrollX) { $sx = [int]$p.scrollX }
                $sy = 0
                if ($null -ne $p.scrollY) { $sy = [int]$p.scrollY }
                [DsbWin32Cua]::MouseScroll([int64]$p.window_id, [int]$p.x, [int]$p.y, $sx, $sy)
                $res = @{ ok = $true }
            }
            "drag" {
                [DsbWin32Cua]::MouseDrag([int64]$p.window_id, [int]$p.from_x, [int]$p.from_y, [int]$p.to_x, [int]$p.to_y)
                $res = @{ ok = $true }
            }
            "press_key" {
                [DsbWin32Cua]::PressKeyChord([int64]$p.window_id, [string]$p.key)
                $res = @{ ok = $true }
            }
            "type_text" {
                [DsbWin32Cua]::PasteText([int64]$p.window_id, [string]$p.text)
                $res = @{ ok = $true }
            }
            "launch_app" {
                Start-Process -FilePath ([string]$p.app)
                Start-Sleep -Milliseconds 450
                $res = @{ ok = $true; launched = [string]$p.app }
            }
            default { throw "Unsupported method: $method" }
        }
        $out = @{ id = $reqId; ok = $true; result = $res } | ConvertTo-Json -Depth 8 -Compress
        [Console]::Out.WriteLine($out)
    } catch {
        $errOut = @{ id = $reqId; ok = $false; error = $_.Exception.Message } | ConvertTo-Json -Compress
        [Console]::Out.WriteLine($errOut)
    }
    [Console]::Out.Flush()
}
