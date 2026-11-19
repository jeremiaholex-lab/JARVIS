using System.Windows.Forms;
using JARVIS.Orb;

namespace JARVIS.Orb;

internal static class Program
{
    [STAThread]
    private static void Main(string[] args)
    {
        Application.SetHighDpiMode(HighDpiMode.PerMonitorV2);
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);

        bool startInHud = args.Any(a =>
            string.Equals(a, "--hud", StringComparison.OrdinalIgnoreCase) ||
            string.Equals(a, "--overlay", StringComparison.OrdinalIgnoreCase));

        // One shell owns the tray icon and the global hotkey. A second launch
        // would otherwise create a second overlay capturing the mic (double
        // wake words).
        using var single = new Mutex(true, @"Local\JARVIS.Orb.Shell", out bool first);
        if (!first)
        {
            MessageBox.Show(
                "JARVIS is already running.\n\nUse the tray icon or press Ctrl+Alt+J to toggle the floating HUD.",
                "JARVIS", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return;
        }

        try
        {
            Application.Run(new OrbAppContext(startInHud));
        }
        catch (Exception ex)
        {
            MessageBox.Show(ex.Message, "JARVIS orb", MessageBoxButtons.OK, MessageBoxIcon.Warning);
        }
        finally
        {
            single.ReleaseMutex();
        }
    }
}
