using System.Drawing.Drawing2D;
using System.Runtime.InteropServices;
using System.Text.Json;

namespace JARVIS.Orb;

/// <summary>
/// Transparent, click-through, always-on-top cursor layer for confirmed desktop
/// actions. The window paints a ring/crosshair, a short path pulse, the action
/// label and a Stop button. It is a WS_EX_NOACTIVATE tool window, so it never
/// steals focus from the application the operator is using.
///
/// The Python orb sends ops over /ws; OverlayForm forwards them here through
/// <see cref="ApplyDesktopCursorCommand"/>. No input is injected by this class:
/// it draws the synchronized marker and reports Stop back to the backend.
/// </summary>
public sealed class CursorOverlayForm : Form
{
    private const int WS_EX_LAYERED = 0x00080000;
    private const int WS_EX_TRANSPARENT = 0x00000020;
    private const int WS_EX_TOOLWINDOW = 0x00000080;
    private const int WS_EX_NOACTIVATE = 0x08000000;
    private const int WM_MOUSEACTIVATE = 0x0021;
    private const int MA_NOACTIVATE = 3;

    private const uint SWP_NOSIZE = 0x0001;
    private const uint SWP_NOMOVE = 0x0002;
    private const uint SWP_NOACTIVATE = 0x0010;
    private const uint SWP_SHOWWINDOW = 0x0040;
    private static readonly IntPtr HWND_TOPMOST = new(-1);

    private const int RingRadius = 26;
    private const int LabelHeight = 30;
    private const int StopWidth = 62;

    public event Action? StopRequested;

    private readonly System.Windows.Forms.Timer _pulse;
    private readonly System.Windows.Forms.Timer _hideTimer;
    private float _phase;
    private bool _visible;
    private string _state = "scanning";
    private string _label = "JARVIS: Ready";
    private PointF _target;
    private PointF _origin;
    private RectangleF _stopHit;
    private bool _pointerInStop;

    public CursorOverlayForm()
    {
        FormBorderStyle = FormBorderStyle.None;
        ShowInTaskbar = false;
        TopMost = true;
        StartPosition = FormStartPosition.Manual;
        AutoScaleMode = AutoScaleMode.None;
        BackColor = Color.FromArgb(0, 0, 0);
        DoubleBuffered = true;
        Bounds = VirtualDesktopBounds();

        _pulse = new System.Windows.Forms.Timer { Interval = 30 };
        _pulse.Tick += (_, _) => PulseTick();
        _hideTimer = new System.Windows.Forms.Timer { Interval = 1200 };
        _hideTimer.Tick += (_, _) => { _hideTimer.Stop(); HideCursor(); };
    }

    protected override bool ShowWithoutActivation => true;

    protected override CreateParams CreateParams
    {
        get
        {
            var cp = base.CreateParams;
            // Layered + transparent makes every pixel click-through; NOACTIVATE
            // and TOOLWINDOW keep the marker out of Alt-Tab and away from focus.
            cp.ExStyle |= WS_EX_LAYERED | WS_EX_TRANSPARENT
                          | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE;
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

    /// <summary>
    /// Entry point for the orb's overlay ops. Accepts the documented op names
    /// and ignores anything else; malformed JSON is dropped rather than shown.
    /// </summary>
    public void ApplyDesktopCursorCommand(string json)
    {
        if (string.IsNullOrWhiteSpace(json)) return;
        try
        {
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;
            if (root.ValueKind != JsonValueKind.Object
                || !root.TryGetProperty("op", out var opEl)
                || opEl.ValueKind != JsonValueKind.String) return;
            string op = opEl.GetString() ?? "";
            if (IsInvisibleInputOp(op)) return;  // never act on hidden-input claims

            if (InvokeRequired && IsHandleCreated && !IsDisposed)
                BeginInvoke(new Action(() => ApplyCursorOp(root.Clone(), op)));
            else
                ApplyCursorOp(root, op);
        }
        catch (JsonException)
        {
            // Invalid overlay payload: fail closed by leaving the last state.
        }
        catch (ObjectDisposedException)
        {
            // Shell is shutting down.
        }
    }

    private static bool IsInvisibleInputOp(string op) => op switch
    {
        "desktop_cursor_show" or "desktop_cursor_move" or "desktop_cursor_state"
            or "desktop_cursor_hide" or "desktop_hud_action" or "desktop_result"
            or "desktop_confirm" => false,
        _ => true,
    };

    private void ApplyCursorOp(JsonElement root, string op)
    {
        switch (op)
        {
            case "desktop_cursor_show":
                _label = "JARVIS: " + JsonText(root, "kind", "Ready");
                SetState(JsonText(root, "state", "scanning"));
                if (TryPoint(root, "x", "y", out var shown)) _target = shown;
                ShowCursor();
                break;
            case "desktop_cursor_move":
                if (TryPoint(root, "x", "y", out var moved))
                {
                    if (!_visible) _origin = moved;
                    _target = moved;
                }
                ShowCursor();
                break;
            case "desktop_cursor_state":
                SetState(JsonText(root, "state", "scanning"));
                ShowCursor();
                break;
            case "desktop_hud_action":
                if (JsonText(root, "action", "") == "stop")
                {
                    SetState("stopped");
                    StopRequested?.Invoke();
                    ScheduleHide();
                }
                break;
            case "desktop_result":
                string status = JsonText(root, "status", "");
                if (status == "cancelled" || status == "failed" || status == "blocked")
                    SetState("stopped");
                else if (status == "pending" || status == "ok") SetState("acting");
                if (status != "pending") ScheduleHide();
                break;
            case "desktop_confirm":
                // The card is rendered by the orb; the shell only marks the
                // target while the operator reads it.
                SetState("scanning");
                if (TryPoint(root, "x", "y", out var target)) _target = target;
                ShowCursor();
                break;
        }
    }

    private void SetState(string state)
    {
        _state = state switch
        {
            "scanning" or "queued" or "acting" or "done" or "stopped" => state,
            _ => "scanning",
        };
        _label = _state switch
        {
            "stopped" => "JARVIS: Stopped",
            "done" => "JARVIS: Done",
            "acting" => "JARVIS: Acting",
            "queued" => "JARVIS: Queued",
            _ => "JARVIS: Scanning",
        };
    }

    private void ShowCursor()
    {
        if (!IsHandleCreated) CreateHandle();
        if (!_visible)
        {
            _visible = true;
            Bounds = VirtualDesktopBounds();
            if (!GetCursorPos(out var p)) p = new POINT { X = (int)_target.X, Y = (int)_target.Y };
            _origin = new PointF(p.X, p.Y);
        }
        if (!Visible) Show();
        SetWindowPos(Handle, HWND_TOPMOST, 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW);
        _hideTimer.Stop();
        _pulse.Start();
    }

    private void HideCursor()
    {
        _pulse.Stop();
        _visible = false;
        if (Visible) Hide();
    }

    private void ScheduleHide()
    {
        if (!_visible) { HideCursor(); return; }
        _hideTimer.Stop();
        _hideTimer.Start();
    }

    private void PulseTick()
    {
        _phase = (_phase + 0.055f) % 1f;
        Invalidate();
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        base.OnPaint(e);
        if (!_visible) return;
        var g = e.Graphics;
        g.SmoothingMode = SmoothingMode.AntiAlias;
        g.Clear(Color.Transparent);

        Color color = _state switch
        {
            "stopped" => Color.FromArgb(255, 255, 82, 82),
            "done" => Color.FromArgb(255, 74, 222, 128),
            "acting" => Color.FromArgb(255, 255, 190, 80),
            "queued" => Color.FromArgb(255, 120, 180, 255),
            _ => Color.FromArgb(255, 102, 231, 255),
        };
        using var ring = new Pen(color, 2.5f);
        using var faint = new Pen(Color.FromArgb(150, color.R, color.G, color.B), 1.5f);
        using var glow = new SolidBrush(Color.FromArgb(30, color.R, color.G, color.B));

        // Path pulse from the OS cursor to the target, with a moving highlight.
        g.DrawLine(faint, _origin, _target);
        float px = _origin.X + (_target.X - _origin.X) * _phase;
        float py = _origin.Y + (_target.Y - _origin.Y) * _phase;
        g.FillEllipse(glow, px - 7, py - 7, 14, 14);

        // Ring + crosshair at the exact action coordinate.
        float x = _target.X, y = _target.Y;
        g.FillEllipse(glow, x - RingRadius - 8, y - RingRadius - 8,
            2 * (RingRadius + 8), 2 * (RingRadius + 8));
        g.DrawEllipse(ring, x - RingRadius, y - RingRadius,
            2 * RingRadius, 2 * RingRadius);
        g.DrawLine(ring, x - 10, y, x + 10, y);
        g.DrawLine(ring, x, y - 10, x, y + 10);

        // Label below the ring; monospace keeps the HUD cyber aesthetic.
        using var font = new Font("Consolas", 10f, FontStyle.Bold);
        SizeF size = g.MeasureString(_label, font);
        float lx = x - size.Width / 2;
        float ly = y + RingRadius + 8;
        RectangleF label = new(lx - 6, ly - 3, size.Width + 12, LabelHeight);
        using var panel = new SolidBrush(Color.FromArgb(225, 4, 12, 22));
        g.FillRectangle(panel, label);
        g.DrawRectangle(ring, Rectangle.Round(label));
        g.DrawString(_label, font, Brushes.White, lx, ly);

        // Stop control sits above the marker and is drawn on the click-through
        // layer as a visual target. Actual stop comes from the HUD button or
        // Ctrl+Alt+J through the existing hotkey window.
        _stopHit = new RectangleF(x - StopWidth / 2, y - RingRadius - LabelHeight - 14,
            StopWidth, LabelHeight);
        using var stopBrush = new SolidBrush(_pointerInStop
            ? Color.FromArgb(210, 255, 82, 82) : Color.FromArgb(190, 20, 26, 38));
        g.FillRectangle(stopBrush, _stopHit);
        g.DrawRectangle(ring, Rectangle.Round(_stopHit));
        using var stopFont = new Font("Consolas", 10f, FontStyle.Bold);
        g.DrawString("STOP", stopFont, Brushes.White,
            _stopHit.X + 12, _stopHit.Y + 6);
    }

    // The full-screen layer is intentionally click-through, so this only
    // changes the painted hover colour; it cannot swallow a click.
    protected override void OnMouseMove(MouseEventArgs e)
    {
        base.OnMouseMove(e);
        bool hit = _stopHit.Contains(e.Location);
        if (hit != _pointerInStop) { _pointerInStop = hit; Invalidate(); }
    }

    private static bool TryPoint(JsonElement root, string xKey, string yKey,
                                 out PointF point)
    {
        point = default;
        if (!root.TryGetProperty(xKey, out var x) || !root.TryGetProperty(yKey, out var y))
            return false;
        if (!x.TryGetSingle(out float px) || !y.TryGetSingle(out float py))
            return false;
        point = new PointF(px, py);
        return true;
    }

    private static string JsonText(JsonElement root, string key, string fallback) =>
        root.TryGetProperty(key, out var value) && value.ValueKind == JsonValueKind.String
            ? value.GetString() ?? fallback : fallback;

    private static Rectangle VirtualDesktopBounds()
    {
        int left = Screen.AllScreens.Min(s => s.Bounds.Left);
        int top = Screen.AllScreens.Min(s => s.Bounds.Top);
        int right = Screen.AllScreens.Max(s => s.Bounds.Right);
        int bottom = Screen.AllScreens.Max(s => s.Bounds.Bottom);
        return Rectangle.FromLTRB(left, top, right, bottom);
    }

    protected override void OnFormClosed(FormClosedEventArgs e)
    {
        _pulse.Dispose();
        _hideTimer.Dispose();
        base.OnFormClosed(e);
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct POINT { public int X; public int Y; }

    [DllImport("user32.dll")]
    private static extern bool GetCursorPos(out POINT p);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter,
        int x, int y, int cx, int cy, uint flags);
}
