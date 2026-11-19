using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Runtime.InteropServices;
using System.Windows.Forms;

namespace JARVIS.Orb;

/// <summary>
/// Owns the single backend process and both UI surfaces: the dedicated v1
/// window and the Phase 5 floating HUD. Exactly one surface captures the
/// microphone at a time, so the wake word never runs twice.
/// </summary>
public sealed class OrbAppContext : ApplicationContext
{
    private readonly Process? _backend;
    private readonly NotifyIcon _tray;
    private readonly HotKeyWindow _hotkey;
    private readonly ToolStripMenuItem _hudItem;
    private readonly IntPtr _trayIconHandle;

    private OrbForm? _window;
    private OverlayForm? _overlay;
    private bool _hudVisible;
    private bool _exiting;

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool DestroyIcon(IntPtr hIcon);

    public OrbAppContext(bool startInHud)
    {
        (_backend, _) = ShellHost.Launch();

        _hudItem = new ToolStripMenuItem("Floating HUD\tCtrl+Alt+J", null, (_, _) => ToggleHud());

        var menu = new ContextMenuStrip();
        menu.Items.Add(new ToolStripMenuItem("Show orb window", null, (_, _) => ShowWindow()));
        menu.Items.Add(_hudItem);
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add(new ToolStripMenuItem("Exit", null, (_, _) => Shutdown()));

        _tray = new NotifyIcon
        {
            Icon = BuildTrayIcon(out _trayIconHandle),
            Text = "JARVIS — Ctrl+Alt+J toggles the floating HUD",
            ContextMenuStrip = menu,
            Visible = true,
        };
        _tray.DoubleClick += (_, _) => ShowWindow();

        _hotkey = new HotKeyWindow();
        _hotkey.Pressed += ToggleHud;
        _hudItem.Text = _hotkey.IsRegistered
            ? $"Floating HUD\t{_hotkey.ShortcutText}"
            : "Floating HUD (use tray menu)";
        _tray.Text = _hotkey.IsRegistered
            ? $"JARVIS — {_hotkey.ShortcutText} toggles the floating HUD"
            : "JARVIS — shortcut unavailable; use the tray menu";

        if (startInHud) SetHud(true);
        else ShowWindow();
    }

    // ------------------------------------------------------------- surfaces

    private void ShowWindow()
    {
        if (_exiting) return;

        // The HUD and the window must not both hold the mic.
        SetHud(false);

        if (_window is null || _window.IsDisposed)
        {
            _window = new OrbForm();
            _window.SetMicEnabled(true);
            _window.StartupFailed += (_, _) => Shutdown();
            _window.FormClosing += OnWindowClosing;
            _window.FormClosed += OnWindowClosed;
        }

        _window.Show();
        // Reclaim the mic: the HUD was just hidden, and this window may have
        // been parked earlier with capture disabled.
        _window.SetMicEnabled(true);
        if (_window.WindowState == FormWindowState.Minimized)
            _window.WindowState = FormWindowState.Maximized;
        _window.Activate();
    }

    private void OnWindowClosing(object? sender, FormClosingEventArgs e)
    {
        if (_exiting) return;
        if (_hudVisible)
        {
            // The HUD is the live surface; closing the chat window parks it.
            e.Cancel = true;
            _window?.Hide();
        }
    }

    private void OnWindowClosed(object? sender, FormClosedEventArgs e)
    {
        // Closing the only visible surface exits, matching v1 expectations.
        if (!_exiting && !_hudVisible) Shutdown();
    }

    private void ToggleHud() => SetHud(!_hudVisible);

    private void SetHud(bool on)
    {
        if (_exiting || _hudVisible == on) return;
        _hudVisible = on;
        _hudItem.Checked = on;

        if (on)
        {
            _window?.SetMicEnabled(false);
            EnsureOverlay().ShowHud();
        }
        else
        {
            _overlay?.HideHud();
            if (_window is { Visible: true }) _window.SetMicEnabled(true);
        }
    }

    private OverlayForm EnsureOverlay()
    {
        if (_overlay is null || _overlay.IsDisposed)
        {
            var overlay = new OverlayForm();
            overlay.HideRequested += () => SetHud(false);
            overlay.OpenFullRequested += () =>
            {
                SetHud(false);
                ShowWindow();
            };
            overlay.FormClosed += (_, _) => _overlay = null;
            _overlay = overlay;
        }
        return _overlay;
    }

    // -------------------------------------------------------------- shutdown

    private void Shutdown()
    {
        if (_exiting) return;
        _exiting = true;

        _hotkey.Dispose();

        _tray.Visible = false;
        _tray.Dispose();

        if (_overlay is { IsDisposed: false } overlay)
        {
            try { overlay.Close(); } catch { }
        }
        if (_window is { IsDisposed: false, Visible: true } window)
        {
            try { window.Close(); } catch { }
        }

        if (_backend is not null)
        {
            try
            {
                ShellHost.StopBackendAsync(_backend).GetAwaiter().GetResult();
            }
            catch
            {
                try
                {
                    if (!_backend.HasExited) _backend.Kill(entireProcessTree: true);
                }
                catch { }
            }
            finally
            {
                _backend.Dispose();
            }
        }

        if (_trayIconHandle != IntPtr.Zero) DestroyIcon(_trayIconHandle);

        ExitThread();
    }

    // ------------------------------------------------------------------ tray

    private static Icon BuildTrayIcon(out IntPtr handle)
    {
        using var bmp = new Bitmap(32, 32);
        using (var g = Graphics.FromImage(bmp))
        {
            g.SmoothingMode = SmoothingMode.AntiAlias;
            g.Clear(Color.Transparent);
            using var glow = new SolidBrush(Color.FromArgb(26, 90, 200));
            g.FillEllipse(glow, 1, 1, 30, 30);
            using var ring = new Pen(Color.FromArgb(110, 225, 255), 2.4f);
            g.DrawEllipse(ring, 6.5f, 6.5f, 19f, 19f);
            using var core = new SolidBrush(Color.FromArgb(220, 245, 255));
            g.FillEllipse(core, 13, 13, 6, 6);
        }
        handle = bmp.GetHicon();
        return Icon.FromHandle(handle);
    }
}
