using System.Runtime.InteropServices;
using System.Text.Json;
using Microsoft.Win32;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace JARVIS.Orb;

/// <summary>
/// Phase 5 floating HUD: a small, rounded, always-on-top WebView2 surface that
/// never activates when it appears. Placement is remembered per monitor and
/// re-clamped when displays change. The page is the same orb, in ?mode=hud.
/// </summary>
public sealed class OverlayForm : Form
{
    private const int WS_EX_TOOLWINDOW = 0x00000080;
    private const int WS_EX_NOACTIVATE = 0x08000000;

    private const int WM_MOUSEACTIVATE = 0x0021;
    private const int MA_NOACTIVATE = 3;

    private const uint SWP_NOSIZE = 0x0001;
    private const uint SWP_NOMOVE = 0x0002;
    private const uint SWP_NOZORDER = 0x0004;
    private const uint SWP_NOACTIVATE = 0x0010;

    private static readonly IntPtr HWND_TOPMOST = new(-1);

    // Matches the canvas' 440x460 aspect so the orb is not distorted.
    private const float WidthDip = 352f;
    private const float HeightDip = 368f;

    private readonly WebView2 _web;
    private readonly System.Windows.Forms.Timer _dragTimer;
    private bool _ready;
    private bool _micWanted;
    private bool _dragging;
    private Point _dragWindowStart;
    private POINT _dragCursorStart;

    public event Action? HideRequested;
    /// <summary>Backend overlay op relayed by the page to the native shell.</summary>
    public event EventHandler<string>? CursorCommandReceived;
    public event Action? OpenFullRequested;
    private readonly System.Windows.Forms.Timer _heartbeat;

    public OverlayForm()
    {
        FormBorderStyle = FormBorderStyle.None;
        ShowInTaskbar = false;
        TopMost = true;
        StartPosition = FormStartPosition.Manual;
        AutoScaleMode = AutoScaleMode.None;
        BackColor = Color.FromArgb(0x0b, 0x11, 0x19);
        Size = PhysicalSize(DeviceDpi);

        _web = new WebView2 { Dock = DockStyle.Fill, TabStop = false };
        Controls.Add(_web);

        _dragTimer = new System.Windows.Forms.Timer { Interval = 15 };
        _dragTimer.Tick += (_, _) => DragTick();

        // A 2 s heartbeat proves this shell is alive. The backend blocks every
        // desktop action when it stops receiving these (fail closed).
        _heartbeat = new System.Windows.Forms.Timer { Interval = 2000 };
        _heartbeat.Tick += (_, _) => PostBridge("overlay_heartbeat");

        SystemEvents.DisplaySettingsChanged += OnDisplaySettingsChanged;
    }

    // ------------------------------------------------------------ windowing

    protected override bool ShowWithoutActivation => true;

    protected override CreateParams CreateParams
    {
        get
        {
            var cp = base.CreateParams;
            // Tool window keeps it out of Alt-Tab; NOACTIVATE keeps focus with
            // whatever the user is typing in when the HUD appears.
            cp.ExStyle |= WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE;
            return cp;
        }
    }

    protected override void WndProc(ref Message m)
    {
        if (m.Msg == WM_MOUSEACTIVATE)
        {
            m.Result = (IntPtr)MA_NOACTIVATE;
            return;
        }
        base.WndProc(ref m);
    }

    protected override void OnHandleCreated(EventArgs e)
    {
        base.OnHandleCreated(e);
        Size = PhysicalSize(DeviceDpi);
        ApplyRegion(DeviceDpi);
        PlaceFromStore();
    }

    protected override void OnDpiChanged(DpiChangedEventArgs e)
    {
        base.OnDpiChanged(e);
        var center = new Point(Left + Width / 2, Top + Height / 2);
        Size = PhysicalSize(e.DeviceDpiNew);
        Location = new Point(center.X - Width / 2, center.Y - Height / 2);
        ApplyRegion(e.DeviceDpiNew);
        ClampToScreen();
    }

    protected override async void OnLoad(EventArgs e)
    {
        base.OnLoad(e);
        await InitWebAsync();
    }

    public void ShowHud()
    {
        _micWanted = true;
        ApplyMic();

        if (!Visible) Show();
        if (!IsHandleCreated) return;

        SetWindowPos(Handle, HWND_TOPMOST, 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
        ClampToScreen();
    }

    public void HideHud()
    {
        _micWanted = false;
        ApplyMic();
        SavePlacement();
        if (Visible) Hide();
    }

    // ------------------------------------------------------------- web host

    private async Task InitWebAsync()
    {
        try
        {
            await _web.EnsureCoreWebView2Async();
        }
        catch (Exception ex)
        {
            MessageBox.Show("WebView2 runtime unavailable: " + ex.Message,
                "JARVIS HUD", MessageBoxButtons.OK, MessageBoxIcon.Error);
            Hide();
            return;
        }

        var wv = _web.CoreWebView2!;
        _web.DefaultBackgroundColor = Color.FromArgb(0x0b, 0x11, 0x19);
        wv.Settings.AreDefaultContextMenusEnabled = false;
        wv.Settings.AreDevToolsEnabled = false;
        wv.Settings.IsStatusBarEnabled = false;
        wv.Settings.IsZoomControlEnabled = false;
        wv.Settings.IsSwipeNavigationEnabled = false;

        wv.PermissionRequested += (_, ev) =>
        {
            if (ev.PermissionKind == CoreWebView2PermissionKind.Microphone)
                ev.State = CoreWebView2PermissionState.Allow;
        };
        wv.WebMessageReceived += OnWebMessage;
        wv.NavigationCompleted += (_, _) =>
        {
            _ready = true;
            ApplyMic();
            PostBridge("shell_connected");
            _heartbeat.Start();
        };

        try
        {
            wv.Navigate($"http://127.0.0.1:{ShellHost.Port}/?mode=hud");
        }
        catch
        {
            // WebView2 not ready yet; the HUD page retries on next show.
        }
    }

    private void ApplyMic()
    {
        if (!_ready) return;
        var core = _web.CoreWebView2;
        if (core is null) return;

        _ = core.ExecuteScriptAsync(
            $"window.jarvisHud&&window.jarvisHud.setMicEnabled({(_micWanted ? "true" : "false")})");
    }

    // -------------------------------------------------------- page messages

    private void PostBridge(string op, string? extraKey = null, string? extraValue = null)
    {
        if (!_ready) return;
        PostToPage(JsonSerializer.Serialize(new Dictionary<string, string?>
        {
            ["op"] = op,
            ["source"] = "shell",  // page scripts cannot arm the overlay gate
            [extraKey ?? ""] = extraValue,
        }));
    }

    /// <summary>Send a JSON op to the page (used by OrbAppContext for stops).</summary>
    public void PostToPage(string json)
    {
        if (!_ready) return;
        var core = _web.CoreWebView2;
        if (core is null) return;
        // postWebMessageJson sends the op to the page, which relays it to /ws.
        _ = core.ExecuteScriptAsync("window.jarvisHud&&window.jarvisHud.bridge&&"
            + "window.jarvisHud.bridge(" + json + ")");
    }

    private void OnWebMessage(object? sender, CoreWebView2WebMessageReceivedEventArgs e)
    {
        try
        {
            using var doc = JsonDocument.Parse(e.WebMessageAsJson);
            var root = doc.RootElement;
            if (root.ValueKind != JsonValueKind.Object) return;
            if (!root.TryGetProperty("op", out var opEl)
                || opEl.ValueKind != JsonValueKind.String) return;
            string op = opEl.GetString() ?? "";

            // The page forwards backend overlay ops back to the native shell.
            // Only the documented cursor ops may reach the cursor window;
            // heartbeat/stop claims from a page must not drive the overlay.
            if (op.StartsWith("desktop_cursor_", StringComparison.Ordinal)
                || op == "desktop_confirm" || op == "desktop_result")
                CursorCommandReceived?.Invoke(this, e.WebMessageAsJson);

            switch (op)
            {
                case "dragStart": BeginDrag(); break;
                case "dragEnd": EndDrag(); break;
                case "hide-hud": HideRequested?.Invoke(); break;
                case "open-full": OpenFullRequested?.Invoke(); break;
                case "stop-desktop":
                    // HUD Stop: cancel the pending action through the backend.
                    PostBridge("desktop_hud_action", "reason", "hud stop");
                    break;
            }
        }
        catch (JsonException)
        {
            // Ignore malformed messages from the page.
        }
    }

    // ------------------------------------------------------------ dragging

    private void BeginDrag()
    {
        if (!GetCursorPos(out _dragCursorStart)) return;
        _dragWindowStart = Location;
        _dragging = true;
        _dragTimer.Start();
    }

    private void DragTick()
    {
        if (!_dragging)
        {
            _dragTimer.Stop();
            return;
        }
        if ((Control.MouseButtons & MouseButtons.Left) == 0)
        {
            EndDrag();
            return;
        }
        if (!GetCursorPos(out var p)) return;

        SetWindowPos(Handle, IntPtr.Zero,
            _dragWindowStart.X + (p.X - _dragCursorStart.X),
            _dragWindowStart.Y + (p.Y - _dragCursorStart.Y),
            0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE);
    }

    private void EndDrag()
    {
        if (!_dragging) return;
        _dragging = false;
        _dragTimer.Stop();
        ClampToScreen();
        SavePlacement();
    }

    // ----------------------------------------------------------- placement

    private void PlaceFromStore()
    {
        var saved = LoadPlacement();
        if (saved is null)
        {
            var wa = PrimaryScreen().WorkingArea;
            int m = (int)Math.Round(24 * DeviceDpi / 96.0);
            Location = new Point(wa.Right - Width - m, wa.Bottom - Height - m);
            return;
        }

        var screen = Screen.AllScreens.FirstOrDefault(s => s.DeviceName == saved.Monitor)
                     ?? PrimaryScreen();
        var area = screen.WorkingArea;
        int x = area.Left + (int)Math.Round(saved.Nx * Math.Max(1, area.Width - Width));
        int y = area.Top + (int)Math.Round(saved.Ny * Math.Max(1, area.Height - Height));
        Location = new Point(x, y);
        ClampToScreen();
    }

    private void ClampToScreen()
    {
        var area = Screen.FromRectangle(Bounds).WorkingArea;
        int x = Math.Max(area.Left, Math.Min(Left, area.Right - Width));
        int y = Math.Max(area.Top, Math.Min(Top, area.Bottom - Height));
        Location = new Point(x, y);
    }

    private void OnDisplaySettingsChanged(object? sender, EventArgs e)
    {
        if (IsDisposed || !IsHandleCreated) return;
        try
        {
            BeginInvoke(new Action(() =>
            {
                if (IsDisposed) return;
                Size = PhysicalSize(DeviceDpi);
                ApplyRegion(DeviceDpi);
                ClampToScreen();
            }));
        }
        catch
        {
            // Window is going away; nothing to re-place.
        }
    }

    private void SavePlacement()
    {
        try
        {
            var screen = Screen.FromRectangle(Bounds);
            var area = screen.WorkingArea;
            var saved = new OverlayPlacement
            {
                Monitor = screen.DeviceName,
                Nx = area.Width > Width ? (double)(Left - area.Left) / (area.Width - Width) : 0,
                Ny = area.Height > Height ? (double)(Top - area.Top) / (area.Height - Height) : 0,
            };

            var dir = Path.GetDirectoryName(StorePath);
            if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);
            File.WriteAllText(StorePath, JsonSerializer.Serialize(saved));
        }
        catch
        {
            // Placement is best-effort; never fail a hide over it.
        }
    }

    private static OverlayPlacement? LoadPlacement()
    {
        try
        {
            return File.Exists(StorePath)
                ? JsonSerializer.Deserialize<OverlayPlacement>(File.ReadAllText(StorePath))
                : null;
        }
        catch
        {
            return null;
        }
    }

    private static string StorePath => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "JARVIS", "overlay.json");

    private static Screen PrimaryScreen() => Screen.PrimaryScreen ?? Screen.AllScreens[0];

    private Size PhysicalSize(int dpi) => new(
        (int)Math.Round(WidthDip * dpi / 96.0),
        (int)Math.Round(HeightDip * dpi / 96.0));

    // --------------------------------------------------------------- chrome

    private void ApplyRegion(int dpi)
    {
        if (!IsHandleCreated) return;

        int radius = (int)Math.Round(30 * dpi / 96.0);
        IntPtr rgn = CreateRoundRectRgn(0, 0, Width + 1, Height + 1, radius, radius);
        if (rgn == IntPtr.Zero) return;

        // On success the system owns the region handle.
        if (SetWindowRgn(Handle, rgn, true) == 0) DeleteObject(rgn);
    }

    protected override void OnFormClosing(FormClosingEventArgs e)
    {
        SavePlacement();
        base.OnFormClosing(e);
    }

    protected override void OnFormClosed(FormClosedEventArgs e)
    {
        SystemEvents.DisplaySettingsChanged -= OnDisplaySettingsChanged;
        _dragTimer.Stop();
        _dragTimer.Dispose();
        _heartbeat.Stop();
        _heartbeat.Dispose();
        base.OnFormClosed(e);
    }

    // -------------------------------------------------------------- native

    [StructLayout(LayoutKind.Sequential)]
    private struct POINT
    {
        public int X;
        public int Y;
    }

    [DllImport("user32.dll")]
    private static extern bool GetCursorPos(out POINT p);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter,
        int x, int y, int cx, int cy, uint flags);

    [DllImport("user32.dll")]
    private static extern int SetWindowRgn(IntPtr hWnd, IntPtr hRgn, bool redraw);

    [DllImport("gdi32.dll")]
    private static extern IntPtr CreateRoundRectRgn(int left, int top, int right, int bottom,
        int width, int height);

    [DllImport("gdi32.dll")]
    private static extern bool DeleteObject(IntPtr obj);
}

internal sealed class OverlayPlacement
{
    public string? Monitor { get; set; }
    public double Nx { get; set; }
    public double Ny { get; set; }
}
