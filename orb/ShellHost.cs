using System.Diagnostics;
using System.Net.Http;
using System.Net.Sockets;
using System.Text;

namespace JARVIS.Orb;

public static class ShellHost
{
    public const int Port = 8010;
    private static readonly HttpClient Http = new() { Timeout = TimeSpan.FromSeconds(3) };

    public static (Process? backend, bool attached) Launch(bool appendLog = false)
    {
        // Attach if a backend is already serving (second window, or service mode).
        if (PortOpen())
            return (null, true);

        var orbDir = FindOrbDir();
        var projectRoot = Path.GetFullPath(Path.Combine(orbDir, ".."));
        var serverPy = Path.Combine(orbDir, "server.py");
        if (!File.Exists(serverPy))
            throw new Exception($"server.py not found at {serverPy}");

        var (pythonw, srcRoot) = FindRuntime(projectRoot);

        // Two roots that must not be the same directory:
        //   OPENJARVIS_SRC  — the checkout we import openjarvis from (may be a repo)
        //   OPENJARVIS_HOME — OpenJarvis runtime state; core refuses to write this
        //                     inside the source tree, so it lives beside .env/.openjarvis
        var stateHome = Path.Combine(projectRoot, ".openjarvis");
        Directory.CreateDirectory(stateHome);

        var psi = new ProcessStartInfo
        {
            FileName = pythonw,
            Arguments = $"\"{serverPy}\"",
            WorkingDirectory = orbDir,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        psi.Environment["OPENJARVIS_HOME"] = stateHome;
        psi.Environment["OPENJARVIS_SRC"] = srcRoot;
        psi.Environment["JARVIS_ORB_MANAGED"] = "1";

        var proc = Process.Start(psi)!;
        // appendLog: a supervised restart must not erase the crash evidence
        // from the previous run. Open handles are closed when the streams
        // end (process exit), so a restart can open the same files again.
        var outFile = OpenLog(Path.Combine(orbDir, "backend.log"), appendLog);
        var errFile = OpenLog(Path.Combine(orbDir, "backend.err.log"), appendLog);
        _ = Task.Run(() => { try { proc.StandardOutput.BaseStream.CopyTo(outFile); } catch { } finally { try { outFile.Dispose(); } catch { } } });
        _ = Task.Run(() => { try { proc.StandardError.BaseStream.CopyTo(errFile); } catch { } finally { try { errFile.Dispose(); } catch { } } });
        // The HTTP listener binds only after the module imports finish, which on a
        // cold start includes torch/faster-whisper — allow well past 10s for that.
        for (int i = 0; i < 120 && !PortOpen(); i++)
        {
            if (proc.HasExited) break;
            Thread.Sleep(250);
        }
        if (!PortOpen())
        {
            proc.Kill(true);
            throw new Exception("Backend did not open port 8010 within 30s — see orb\\backend.err.log.");
        }
        return (proc, false);
    }

    private static FileStream OpenLog(string path, bool append)
    {
        if (!append) return File.Create(path);
        return new FileStream(path, FileMode.Append, FileAccess.Write, FileShare.Read);
    }

    /// <summary>
    /// Locate the OpenJarvis checkout holding both the venv and the package.
    /// Returns the pythonw.exe to run and the root to expose as OPENJARVIS_SRC.
    /// </summary>
    private static (string pythonw, string srcRoot) FindRuntime(string projectRoot)
    {
        var candidates = new[]
        {
            Environment.GetEnvironmentVariable("OPENJARVIS_SRC"),
            Environment.GetEnvironmentVariable("OPENJARVIS_HOME"),
            Path.Combine(projectRoot, "openjarvis_runtime"),
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                         "OpenJarvis", "src"),
        };

        foreach (var candidate in candidates)
        {
            if (string.IsNullOrWhiteSpace(candidate)) continue;
            var root = Path.GetFullPath(candidate);
            var pythonw = Path.Combine(root, ".venv", "Scripts", "pythonw.exe");
            if (File.Exists(pythonw) && Directory.Exists(Path.Combine(root, "src", "openjarvis")))
                return (pythonw, root);
        }

        throw new Exception(
            "OpenJarvis runtime not found (need <root>\\.venv\\Scripts\\pythonw.exe and <root>\\src\\openjarvis).\n" +
            $"Checked OPENJARVIS_SRC, OPENJARVIS_HOME, {Path.Combine(projectRoot, "openjarvis_runtime")} " +
            "and %LOCALAPPDATA%\\OpenJarvis\\src.\n" +
            "Run the Phase 1 installer, or set OPENJARVIS_SRC to the checkout.");
    }

    /// <summary>Resolve the project folder holding server.py (works from bin\... as well as source).</summary>
    public static string FindOrbDir()
    {
        var here = AppContext.BaseDirectory;
        var candidates = new[]
        {
            Environment.GetEnvironmentVariable("JARVIS_ORB_ROOT"),
            here,
            Path.GetFullPath(Path.Combine(here, "..", "..", "..")), // bin\{cfg}\net8.0-windows -> project dir
        };
        foreach (var c in candidates)
        {
            if (!string.IsNullOrEmpty(c) && File.Exists(Path.Combine(c, "server.py")))
                return Path.GetFullPath(c!);
        }
        throw new Exception("Cannot locate server.py next to the shell executable.");
    }

    public static async Task StopBackendAsync(Process? backend)
    {
        if (backend is null || backend.HasExited)
            return;

        try
        {
            using var content = new StringContent("{}", Encoding.UTF8, "application/json");
            using var response = await Http.PostAsync($"http://127.0.0.1:{Port}/api/shutdown", content);
            response.EnsureSuccessStatusCode();
            await backend.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(8));
        }
        catch (Exception) when (!backend.HasExited)
        {
            // Fallback for an unresponsive/stale backend. Do not leave the model
            // processes orphaned if graceful shutdown could not complete.
            backend.Kill(entireProcessTree: true);
            await backend.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(5));
        }
    }

    private static bool PortOpen()
    {
        try
        {
            using var c = new TcpClient();
            c.Connect("127.0.0.1", Port);
            return true;
        }
        catch
        {
            return false;
        }
    }
}
