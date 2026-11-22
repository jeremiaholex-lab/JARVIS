using System.Net.Http;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
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
    private Process? _backend;
    private bool _ownsBackend;
    private readonly NotifyIcon _tray;
    private readonly HotKeyWindow _hotkey;
    private readonly ToolStripMenuItem _hudItem;
    private readonly IntPtr _trayIconHandle;

    private static bool _secretsReady;

    private static string _relaySecret = ShellHost.NativeRelaySecret;
    private static string _shellToken = ShellHost.NativeShellToken;

    /// <summary>
    /// Verify a backend relay signature with the same canonical field order as
    /// jarvis.desktop.actions.sign_native_relay. Static so tests can call it
    /// without starting a WinForms desktop session.
    /// </summary>
    public static bool VerifyRelaySignature(string json, string secret)
    {
        if (string.IsNullOrWhiteSpace(json)) return false;
        try
        {
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;
            if (root.ValueKind != JsonValueKind.Object
                || !root.TryGetProperty("op", out var opEl)
                || opEl.ValueKind != JsonValueKind.String) return false;
            string op = opEl.GetString() ?? "";
            if (!op.StartsWith("desktop_", StringComparison.Ordinal)) return false;
            if (!root.TryGetProperty("relay_sig", out var sigEl)
                || sigEl.ValueKind != JsonValueKind.String) return false;

            static string Field(JsonElement root, string name) =>
                root.TryGetProperty(name, out var el) && el.ValueKind != JsonValueKind.Null
                    && el.ValueKind != JsonValueKind.Object && el.ValueKind != JsonValueKind.Array
                    ? el.ToString()
                    : "";
            // Canonical field order shared with sign_native_relay: every value
            // used by the native cursor layer is covered by the MAC.
            string body = string.Join('|',
                op,
                Field(root, "action_id"), Field(root, "status"),
                Field(root, "action"), Field(root, "reason"),
                Field(root, "x"), Field(root, "y"),
                Field(root, "state"), Field(root, "kind"));
            using var hmac = new HMACSHA256(Encoding.UTF8.GetBytes(secret));
            string expected = Convert.ToHexString(
                hmac.ComputeHash(Encoding.UTF8.GetBytes(body))).ToLowerInvariant();
            return CryptographicOperations.FixedTimeEquals(
                Encoding.UTF8.GetBytes(sigEl.GetString() ?? ""),
                Encoding.UTF8.GetBytes(expected));
        }
        catch (JsonException)
        {
            return false;
        }
    }

    public static bool IsTrustedRelay(string json) =>
        _secretsReady && VerifyRelaySignature(json, _relaySecret);

    public static void RefreshNativeSecrets()
    {
        _relaySecret = ShellHost.NativeRelaySecret;
        _shellToken = ShellHost.NativeShellToken;
        _secretsReady = string.IsNullOrEmpty(ShellHost.ChannelError)
            && _relaySecret.Length == 64 && _shellToken.Length == 64;
    }

    /// <summary>
    /// Send a heartbeat over the native-only loopback channel. The token is
    /// used as an HTTP header and HMAC key; it is never serialized into a page
    /// bridge message or injected by ExecuteScriptAsync.
    /// </summary>
    public static void SendNativeHeartbeat()
    {
        if (!_secretsReady) return;
        var nonce = Convert.ToHexString(RandomNumberGenerator.GetBytes(16)).ToLowerInvariant();
        const string op = "heartbeat";
        var request = new HttpRequestMessage(HttpMethod.Post,
            $"http://127.0.0.1:{ShellHost.Port}/api/native/heartbeat")
        {
            Content = new StringContent(
                JsonSerializer.Serialize(new Dictionary<string, string>
                {
                    ["op"] = op,
                    ["nonce"] = nonce,
                }),
                Encoding.UTF8,
                "application/json"),
        };
        request.Headers.Add("x-jarvis-shell-token", _shellToken);
        request.Headers.Add("x-jarvis-shell-hmac",
            ShellHost.SignNativeMessage(op, nonce, _shellToken));
        try
        {
            using var response = ShellHost.NativeHttp.Send(request);
            response.EnsureSuccessStatusCode();
        }
        catch (HttpRequestException)
        {
            // A missed heartbeat is fail-closed on the backend; do not block
            // the UI thread with a retry loop or expose the token in logs.
        }
        catch (OperationCanceledException)
        {
            // Covers TaskCanceledException (HttpClient timeout). It must never
            // escape a WinForms timer or navigation handler as an unhandled
            // thread exception.
        }
    }

    // Backend supervision. When we launched the backend ourselves and it dies
    // (OOM on this box is the realistic case: whisper + kokoro + ollama push
    // past 15 GB), the orb is useless until the user restarts the shell.
    // A UI-thread watchdog polls the child every 5 s and relaunches it with a
    // crash-loop backoff, so a transient kill recovers silently while a truly
    // broken server does not spin the machine.
    private readonly System.Windows.Forms.Timer _backendWatchdog = new() { Interval = 5000 };
    private int _restarts;
    private DateTime _lastBackendStart = DateTime.MinValue;
    private bool _gaveUpNotified;

    private OrbForm? _window;
    private OverlayForm? _overlay;
    private CursorOverlayForm? _cursorOverlay;
    private bool _hudVisible;
    private bool _exiting;

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool DestroyIcon(IntPtr hIcon);

    public OrbAppContext(bool startInHud)
    {
        RefreshNativeSecrets();
        (_backend, _) = ShellHost.Launch();
        _ownsBackend = _backend is not null;
        _lastBackendStart = DateTime.Now;

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
        // Emergency stop: the same global chord cancels any pending desktop
        // action as well as toggling the HUD. Escape is deliberately not
        // intercepted globally (operator's explicit choice).
        _hotkey.Pressed += () => OnDesktopStop("global hotkey");
        _hudItem.Text = _hotkey.IsRegistered
            ? $"Floating HUD\t{_hotkey.ShortcutText}"
            : "Floating HUD (use tray menu)";
        _tray.Text = _hotkey.IsRegistered
            ? $"JARVIS — {_hotkey.ShortcutText} toggles the floating HUD"
            : "JARVIS — shortcut unavailable; use the tray menu";

        if (startInHud) SetHud(true);
        else ShowWindow();

        _backendWatchdog.Tick += (_, _) => SuperviseBackend();
        _backendWatchdog.Start();
    }

    /// <summary>
    /// Relaunch a backend we own that exited unexpectedly. Bounded: more
    /// than three exits inside their own first minute means the server is
    /// broken, not unlucky, and a restart loop only burns the machine the
    /// models need. An attach-mode shell (backend owned elsewhere) never
    /// supervises or kills anything.
    /// </summary>
    private void SuperviseBackend()
    {
        if (_exiting || !_ownsBackend) return;
        bool dead;
        try { dead = _backend is null || _backend.HasExited; }
        catch { dead = true; }
        if (!dead) return;

        // A backend that survived its first minute earns a fresh budget.
        if (DateTime.Now - _lastBackendStart > TimeSpan.FromSeconds(60))
            _restarts = 0;

        if (_restarts >= 3)
        {
            if (!_gaveUpNotified)
            {
                _gaveUpNotified = true;
                _tray.Text = "JARVIS — backend crashed repeatedly; restart the orb";
                _tray.BalloonTipText = "The JARVIS backend crashed 3 times in a row. "
                    + "Check orb\\backend.err.log, then restart the orb.";
                _tray.ShowBalloonTip(6000, "JARVIS backend stopped", _tray.BalloonTipText,
                    ToolTipIcon.Error);
            }
            return;
        }

        _restarts++;
        try
        {
            _backend?.Dispose();
        }
        catch { }
        _backend = null;
        RefreshNativeSecrets();

        try
        {
            var (proc, attached) = ShellHost.Launch(appendLog: true);
            _backend = proc;
            _ownsBackend = !attached;
            _lastBackendStart = DateTime.Now;
            _gaveUpNotified = false;
            if (!attached)
            {
                _tray.BalloonTipText = "The JARVIS backend restarted (crash recovery).";
                _tray.ShowBalloonTip(3000, "JARVIS", _tray.BalloonTipText, ToolTipIcon.Info);
            }
        }
        catch
        {
            // Launch failures keep _backend null; the next tick counts as
            // another crash against the same budget until it is exhausted.
        }
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

        _window.CursorCommandReceived += (_, json) => OnDesktopCursorCommand(json);
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

    /// <summary>Send a stop to the cursor layer and the backend (one-way).</summary>
    private void OnDesktopStop(string reason)
    {
        // The backend is the only source of cursor-layer commands. Post the
        // stop to /ws; the signed desktop_hud_action event it emits is the
        // verified round trip that reaches ApplyDesktopCursorCommand.
        PostOverlayBridge("desktop_hud_action", "reason", reason);
    }

    private void PostOverlayBridge(string op, string? extraKey = null,
                                   string? extraValue = null)
    {
        // The HUD page relays shell ops to /ws via window.jarvisHud.bridge.
        string json = extraKey is null
            ? "{\"op\":\"" + op + "\"}"
            : "{\"op\":\"" + op + "\",\"" + extraKey + "\":\"" + extraValue + "\"}";
        _overlay?.PostToPage(json);
        // The main orb window relays too, so Stop works with either surface.
        _window?.PostToPage(json);
    }

    /// <summary>Forward orb overlay ops to the transparent cursor window.</summary>
    private void OnDesktopCursorCommand(string json)
    {
        // Page-authored ops are unsigned and stop here. Only the backend's
        // relay signature can unlock the native cursor layer.
        if (!IsTrustedRelay(json)) return;
        (_cursorOverlay ??= new CursorOverlayForm()).ApplyDesktopCursorCommand(json);
    }

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
            overlay.CursorCommandReceived += (_, json) => OnDesktopCursorCommand(json);
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

        _backendWatchdog.Stop();
        _hotkey.Dispose();

        _tray.Visible = false;
        _tray.Dispose();

        if (_overlay is { IsDisposed: false } overlay)
        {
            try { overlay.Close(); } catch { }
        }
        if (_cursorOverlay is { IsDisposed: false } cursor)
        {
            try { cursor.Close(); } catch { }
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
