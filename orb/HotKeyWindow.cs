using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Windows.Forms;

namespace JARVIS.Orb;

/// <summary>
/// Message-only sink for WM_HOTKEY. Not a visible form, so it cannot steal
/// focus or appear in Alt-Tab.
/// </summary>
internal sealed class HotKeyWindow : NativeWindow, IDisposable
{
    private const int WM_HOTKEY = 0x0312;
    private const int MOD_ALT = 0x0001;
    private const int MOD_CONTROL = 0x0002;
    private const int MOD_SHIFT = 0x0004;
    private const int MOD_NOREPEAT = 0x4000;
    private const int HotKeyId = 0x4A52;  // 'JR'
    private const int FallbackHotKeyId = 0x4A53;  // 'JS'
    private int _registeredId;

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool RegisterHotKey(IntPtr hWnd, int id, int fsModifiers, int vk);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool UnregisterHotKey(IntPtr hWnd, int id);

    public event Action? Pressed;

    public string ShortcutText { get; private set; } = "Ctrl+Alt+J";
    public int RegistrationError { get; private set; }
    public bool IsRegistered => _registeredId != 0;

    public HotKeyWindow()
    {
        CreateHandle(new CreateParams());
        if (RegisterHotKey(Handle, HotKeyId, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, (int)Keys.J))
        {
            _registeredId = HotKeyId;
            return;
        }

        RegistrationError = Marshal.GetLastWin32Error();
        // If another application owns Ctrl+Alt+J, keep the global toggle
        // available on a clearly advertised backup chord.
        if (RegisterHotKey(Handle, FallbackHotKeyId, MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, (int)Keys.J))
        {
            _registeredId = FallbackHotKeyId;
            ShortcutText = "Ctrl+Shift+J";
        }
        else
        {
            RegistrationError = Marshal.GetLastWin32Error();
            Debug.WriteLine($"JARVIS global hotkey registration failed: {RegistrationError}");
        }
    }

    protected override void WndProc(ref Message m)
    {
        if (m.Msg == WM_HOTKEY && _registeredId != 0 && m.WParam.ToInt64() == _registeredId)
            Pressed?.Invoke();
        base.WndProc(ref m);
    }

    public void Dispose()
    {
        if (_registeredId != 0) UnregisterHotKey(Handle, _registeredId);
        DestroyHandle();
    }
}
