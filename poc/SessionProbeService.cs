using System;
using System.Diagnostics;
using System.IO;
using System.ServiceProcess;
using System.Threading;

// One-shot LocalService harness for the phase-0 probe.
internal sealed class SessionProbeService : ServiceBase
{
    private readonly string folder = AppDomain.CurrentDomain.BaseDirectory;

    public static void Main()
    {
        var service = new SessionProbeService();
        service.ServiceName = Path.GetFileNameWithoutExtension(typeof(SessionProbeService).Assembly.Location);
        ServiceBase.Run(service);
    }

    protected override void OnStart(string[] args)
    {
        if (args.Length != 1 || !Directory.Exists(args[0]))
            throw new ArgumentException("Expected the RGBMoCap installation directory.");
        var root = Path.GetFullPath(args[0]).TrimEnd(Path.DirectorySeparatorChar);
        new Thread(() => Probe(root)).Start();
    }

    private void Probe(string root)
    {
        var output = Path.Combine(folder, "result");
        try
        {
            var start = new ProcessStartInfo
            {
                FileName = Path.Combine(root, "runtime", "python.exe"),
                WorkingDirectory = folder,
                Arguments = "-I -X utf8 -B session_probe.py --expect-service --out result --root \"" + root + "\"",
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true
            };
            using (var child = Process.Start(start))
            {
                var stdout = child.StandardOutput.ReadToEndAsync();
                var stderr = child.StandardError.ReadToEndAsync();
                child.WaitForExit();
                File.WriteAllText(Path.Combine(output, "service.log"), stdout.Result + stderr.Result);
                ExitCode = child.ExitCode;
            }
        }
        catch (Exception error)
        {
            ExitCode = 1;
            File.WriteAllText(Path.Combine(output, "service.log"), error.ToString());
        }
        finally
        {
            Stop();
        }
    }
}
