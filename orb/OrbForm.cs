using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace JARVIS.Orb;

/// <summary>
/// The dedicated v1 window. The backend is owned by OrbAppContext; this form
/// only hosts the page and can hand the microphone over to the HUD.
/// </summary>
public sealed class OrbForm : Form
{
    private readonly WebView2 _web;
    private bool _micEnabled = true;

    public event EventHandler? StartupFailed;
    /// <summary>Backend overlay op relayed by the page to the native shell.</summary>
    public event EventHandler<string>? CursorCommandReceived;

    public OrbForm()
    {
        Text = "JARVIS — Holo-Flux Orb";
        StartPosition = FormStartPosition.CenterScreen;
        WindowState = FormWindowState.Maximized;
        BackColor = Color.FromArgb(0x0a, 0x0e, 0x14);

        _web = new WebView2 { Dock = DockStyle.Fill };
        Controls.Add(_web);
        Load += (_, _) => LoadWebView();
    }

    /// <summary>
    /// The HUD owns the mic while it is visible; the window must stop its own
    /// capture so the wake word is only ever scored once.
    /// </summary>
    public void SetMicEnabled(bool on)
    {
        _micEnabled = on;
        ApplyMicState();
    }

    private void ApplyMicState()
    {
        var core = _web.CoreWebView2;
        if (core is null) return;

        _ = core.ExecuteScriptAsync(
            $"window.jarvisHud&&window.jarvisHud.setMicEnabled({(_micEnabled ? "true" : "false")})");
    }

    /// <summary>Relay a shell op to the page, which forwards it over /ws.</summary>
    public void PostToPage(string json)
    {
        var core = _web.CoreWebView2;
        if (core is null) return;
        _ = core.ExecuteScriptAsync("window.jarvisHud&&window.jarvisHud.bridge&&"
            + "window.jarvisHud.bridge(" + json + ")");
    }

    private async void LoadWebView()
    {
        try
        {
            await _web.EnsureCoreWebView2Async();
        }
        catch (Exception ex)
        {
            MessageBox.Show("WebView2 runtime unavailable: " + ex.Message,
                            "JARVIS orb", MessageBoxButtons.OK, MessageBoxIcon.Error);
            StartupFailed?.Invoke(this, EventArgs.Empty);
            return;
        }

        var wv = _web.CoreWebView2!;
        wv.Settings.AreDevToolsEnabled = true;
        wv.Settings.IsStatusBarEnabled = false;
        wv.Settings.IsZoomControlEnabled = false;
        wv.Settings.IsSwipeNavigationEnabled = false;

        // The mic drives the orb; grant microphone capture without a prompt.
        wv.PermissionRequested += (_, ev) =>
        {
            if (ev.PermissionKind == CoreWebView2PermissionKind.Microphone)
                ev.State = CoreWebView2PermissionState.Allow;
        };

        // The page may load while the HUD is already visible; apply the
        // deferred mic state once it is safe to talk to the page.
        wv.NavigationCompleted += (_, _) =>
        {
            ApplyMicState();
            OrbAppContext.SendNativeHeartbeat();
        };
        wv.WebMessageReceived += (_, ev) =>
        {
            // The main window relays HUD Stop to the backend as well.
            if (ev.WebMessageAsJson.Contains("stop-desktop", StringComparison.Ordinal))
                PostToPage("{\"op\":\"desktop_hud_action\",\"reason\":\"hud stop\"}");
            // Backend overlay ops are echoed by forwardOverlayOp(). The app
            // context verifies the backend HMAC before any cursor op is used.
            if (OrbAppContext.IsTrustedRelay(ev.WebMessageAsJson))
                CursorCommandReceived?.Invoke(this, ev.WebMessageAsJson);
        };

        try
        {
            wv.Navigate("http://127.0.0.1:" + ShellHost.Port + "/");
        }
        catch (Exception)
        {
            // WebView2 not ready yet; the page is retried on next focus.
        }
    }
}
