// OpenReSense.exe: starts the bundled Python with the app ("python\python.exe -m server") in this console window.
// Built by scripts\build_release.ps1 with the C# compiler that ships with Windows (.NET Framework 4).
using System;
using System.Diagnostics;
using System.IO;
using System.Text;

static class Launcher
{
    static int Main(string[] args)
    {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        string python = Path.Combine(root, "python", "python.exe");
        if (!File.Exists(python))
            return Fail("Cannot find " + python + ".\nKeep OpenReSense.exe in its folder: unzip or install the whole folder.", 1);
        Console.Title = "OpenReSense - close this window to stop the app";
        Console.CancelKeyPress += (s, e) => e.Cancel = true;  // Ctrl+C: let the app shut down, then exit with it
        var line = new StringBuilder("-m server");
        foreach (string a in args)
            line.Append(' ').Append(Quote(a));
        var info = new ProcessStartInfo(python, line.ToString()) { UseShellExecute = false, WorkingDirectory = root };
        using (Process p = Process.Start(info))
        {
            p.WaitForExit();
            return p.ExitCode == 0 ? 0 : Fail(null, p.ExitCode);
        }
    }

    static string Quote(string a)  // Windows command-line quoting, as the C runtime splits it again
    {
        if (a.Length > 0 && a.IndexOfAny(new[] { ' ', '\t', '"' }) < 0)
            return a;
        int tail = 0;  // backslashes before the closing quote must be doubled ("C:\dir with space\")
        while (tail < a.Length && a[a.Length - 1 - tail] == '\\')
            tail++;
        return "\"" + a.Replace("\\\"", "\\\\\"").Replace("\"", "\\\"") + new string('\\', tail) + "\"";
    }

    static int Fail(string message, int code)  // keep the window open so the message can be read
    {
        if (message != null)
            Console.Error.WriteLine(message);
        Console.Error.WriteLine("\nPress any key to close this window.");
        try { Console.ReadKey(true); } catch (InvalidOperationException) { }  // no console to read from
        return code;
    }
}
