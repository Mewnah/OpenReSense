// OpenReSense.exe: OpenReSense in its own window. It starts the bundled Python with the app ("python\python.exe -m
// server") hidden in the background and shows the app in a WebView2 control (the Microsoft Edge engine that comes with
// Windows). Closing the window stops the app. Starting it again while it runs brings its window to the front.
//   --browser     show the app in the default web browser instead (also the fallback when WebView2 is missing)
//   --no-browser  only the server, no window (scripts and tests); the exit code is the server's
//   --port N      the port to use (default 8765; with a window, the next free one if it is taken)
//   --devtools    allow the developer tools (F12) in the window
// Other arguments go to the server. The server's output goes to cache\server.log.
// Built by scripts\build_release.ps1 with the C# 5 compiler that comes with Windows (.NET Framework 4.8), against the
// WebView2 SDK, whose DLLs (Microsoft.Web.WebView2.Core.dll, ...WinForms.dll, WebView2Loader.dll) sit next to this exe.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

static class Launcher
{
    internal static readonly string Root = AppDomain.CurrentDomain.BaseDirectory;
    // the window's browser data (cache, remembered layout): short, always writable, outside the app folder's path limit
    internal static readonly string DataDir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "OpenReSense");
    internal static readonly Color Background = Color.FromArgb(11, 14, 20);  // the app's --bg
    internal const string WebView2Download = "https://go.microsoft.com/fwlink/p/?LinkId=2124703";
    internal static Shell App;

    [STAThread]
    static int Main(string[] args)
    {
        bool browser = false, headless = false, devtools = false;
        int port = 8765;
        var rest = new List<string>();
        for (int i = 0; i < args.Length; i++)
        {
            string a = args[i];
            if (a == "--browser") browser = true;
            else if (a == "--no-browser") headless = true;
            else if (a == "--devtools") devtools = true;
            else if ((a == "--port" && i + 1 < args.Length) || a.StartsWith("--port="))
            {
                string v = a == "--port" ? args[++i] : a.Substring(7);
                if (!int.TryParse(v, out port) || port < 1 || port > 65535)
                    return Fail(headless, "--port needs a number from 1 to 65535, not \"" + v + "\".");
            }
            else rest.Add(a);
        }
        string python = Path.Combine(Root, "python", "python.exe");
        if (!File.Exists(python))
            return Fail(headless, "Cannot find " + python + ".\n\nKeep OpenReSense.exe in its folder: unzip or install the whole folder.");
        if (headless)
        {
            using (var server = new Server(python))
                return server.Run(rest, port);
        }

        SetDpiAware();
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        string note = null;
        if (!browser)
        {
            try
            {
                if (CoreWebView2Environment.GetAvailableBrowserVersionString() == null) note = "missing";
            }
            catch (WebView2RuntimeNotFoundException) { note = "missing"; }
            catch (Exception ex) { note = "The app window cannot start (" + ex.Message + "), so OpenReSense runs in your web browser."; }
            if (note == "missing")
                note = "The Microsoft Edge WebView2 Runtime is not installed, so OpenReSense runs in your web browser. Install it to get OpenReSense in its own window:";
            browser = note != null;
        }

        string id = "Local\\OpenReSense-" + Hash(Root);  // one running copy per app folder
        bool first;
        using (var single = new Mutex(true, id, out first))
        using (var wake = new EventWaitHandle(false, EventResetMode.AutoReset, id + "-show"))
        {
            if (!first)  // already running: bring it to the front
            {
                AllowSetForegroundWindow(-1);
                wake.Set();
                return 0;
            }
            using (var server = new Server(python))
            {
                App = new Shell();
                App.MainForm = browser ? (Form)new StatusForm(server, rest, port, note) : new AppForm(server, rest, port, devtools);
                var waker = new Thread(() =>
                {
                    try
                    {
                        while (true)
                        {
                            wake.WaitOne();
                            var f = App.MainForm as AppWindow;
                            try { if (f != null) f.BeginInvoke(new Action(f.Wake)); }
                            catch (InvalidOperationException) { }  // not shown yet, or closing
                        }
                    }
                    catch (ObjectDisposedException) { }  // the app is ending
                });
                waker.IsBackground = true;
                waker.Start();
                Application.Run(App);
            }
        }
        return 0;
    }

    static int Fail(bool quiet, string message)
    {
        if (!quiet) MessageBox.Show(message, "OpenReSense", MessageBoxButtons.OK, MessageBoxIcon.Error);
        return 1;
    }

    internal static void OpenExternal(string uri)  // web links open in the user's browser, never in the app window
    {
        Uri u;
        if (!Uri.TryCreate(uri, UriKind.Absolute, out u) || (u.Scheme != Uri.UriSchemeHttp && u.Scheme != Uri.UriSchemeHttps && u.Scheme != Uri.UriSchemeMailto))
            return;
        try { Process.Start(new ProcessStartInfo(u.AbsoluteUri) { UseShellExecute = true }); }
        catch (Exception) { }  // no browser set up: nothing we can open it with
    }

    internal static string LastLines(string s, int n)
    {
        string[] l = s.Split('\n');
        return l.Length <= n ? s : "…\n" + string.Join("\n", l, l.Length - n, n);
    }

    internal static Icon AppIcon()  // every size of the .ico (the exe's own icon resource gives only one)
    {
        Stream s = Assembly.GetExecutingAssembly().GetManifestResourceStream("OpenReSense.ico");
        return s != null ? new Icon(s) : Icon.ExtractAssociatedIcon(Application.ExecutablePath);
    }

    internal static string Page(string title, string detail, bool spinner)  // the window's own messages
    {
        return "<!doctype html><html><head><meta charset='utf-8'><title>OpenReSense</title><style>@keyframes s{to{transform:rotate(360deg)}}</style></head>" +
            "<body style=\"margin:0;height:100vh;display:grid;place-items:center;background:#0b0e14;color:#8a94a8;font:15px system-ui,'Segoe UI',sans-serif\">" +
            "<div style='max-width:780px;padding:24px;text-align:center'><div style='font-size:26px;color:#e6eaf2;margin-bottom:12px'>Open<b style='color:#5aa9ff'>ReSense</b></div>" +
            "<div>" + WebUtility.HtmlEncode(title) + "</div>" +
            (spinner ? "<div style='width:22px;height:22px;margin:16px auto 0;border:3px solid #2a3448;border-top-color:#5aa9ff;border-radius:50%;animation:s .8s linear infinite'></div>" : "") +
            (detail == null ? "" : "<pre style='text-align:left;white-space:pre-wrap;color:#e6eaf2;background:#121722;border:1px solid #2a3448;border-radius:8px;padding:12px;margin-top:16px;max-height:60vh;overflow:auto;font:12.5px Consolas,monospace'>" +
                WebUtility.HtmlEncode(detail) + "</pre>") +
            "</div></body></html>";
    }

    // Window size and place, kept across starts.
    static string BoundsFile { get { return Path.Combine(DataDir, "window.txt"); } }
    internal static void LoadBounds(Form f)
    {
        Rectangle wa = Screen.PrimaryScreen.WorkingArea;
        var r = new Rectangle(wa.X + wa.Width / 14, wa.Y + wa.Height / 14, wa.Width * 6 / 7, wa.Height * 6 / 7);
        bool max = false;
        try
        {
            string[] v = File.ReadAllText(BoundsFile).Trim().Split(',');
            var saved = new Rectangle(int.Parse(v[0]), int.Parse(v[1]), int.Parse(v[2]), int.Parse(v[3]));
            var grip = new Rectangle(saved.X + 40, saved.Y, 200, 30);  // part of the title bar must be on a screen
            if (saved.Width >= 400 && saved.Height >= 300 && Screen.AllScreens.Any(s => s.WorkingArea.Contains(grip)))
            {
                r = saved;
                max = v.Length > 4 && v[4] == "max";
            }
        }
        catch (Exception) { }  // first start, or a file we cannot read: the default
        f.StartPosition = FormStartPosition.Manual;
        f.Bounds = r;
        if (max) f.WindowState = FormWindowState.Maximized;
    }
    internal static void SaveBounds(Form f)
    {
        try
        {
            Rectangle r = f.WindowState == FormWindowState.Normal ? f.Bounds : f.RestoreBounds;
            Directory.CreateDirectory(DataDir);
            File.WriteAllText(BoundsFile, string.Join(",", r.X, r.Y, r.Width, r.Height) + (f.WindowState == FormWindowState.Maximized ? ",max" : ""));
        }
        catch (Exception) { }  // only a convenience
    }

    // This folder as the server names it in /api/instance (server/app.py): only a server of this folder is reused.
    internal static readonly string RootId = Hash(Path.GetFullPath(Root).TrimEnd('\\'));

    internal static string Hash(string s)  // SHA-1 of the lower-cased text: 16 lower-case hex digits, as Python's hexdigest
    {
        using (var sha = SHA1.Create())
            return BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(s.ToLowerInvariant()))).Replace("-", "").ToLowerInvariant().Substring(0, 16);
    }

    static void SetDpiAware()  // sharp text on scaled displays; WebView2 follows each monitor's scale itself
    {
        try { if (SetProcessDpiAwarenessContext(new IntPtr(-4))) return; }  // per monitor v2 (Windows 10 1703+)
        catch (EntryPointNotFoundException) { }
        SetProcessDPIAware();
    }

    [DllImport("user32.dll")] static extern bool SetProcessDpiAwarenessContext(IntPtr value);
    [DllImport("user32.dll")] static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] static extern bool AllowSetForegroundWindow(int processId);
}

// Lets the main window change (app window -> browser status window) without ending the app.
sealed class Shell : ApplicationContext
{
    public void Switch(Form next)
    {
        Form old = MainForm;
        MainForm = next;
        next.Show();
        if (old != null) old.Close();
    }
}

class AppWindow : Form
{
    FormWindowState shown = FormWindowState.Normal;
    public AppWindow()
    {
        Text = "OpenReSense";
        Icon = Launcher.AppIcon();
        Resize += (s, e) => { if (WindowState != FormWindowState.Minimized) shown = WindowState; };
    }
    public virtual void Wake()  // started again: come to the front
    {
        if (WindowState == FormWindowState.Minimized) WindowState = shown;
        Activate();
    }
}

// The app in its own window.
sealed class AppForm : AppWindow
{
    readonly Server server;
    readonly List<string> args;
    readonly int port;
    readonly bool devtools;
    readonly WebView2 web = new WebView2();
    string appUrl;  // set once the server answers
    bool ownPage;   // the next data:/about: navigation is one of the window's own messages
    int crashes;

    public AppForm(Server server, List<string> args, int port, bool devtools)
    {
        this.server = server; this.args = args; this.port = port; this.devtools = devtools;
        BackColor = Launcher.Background;
        MinimumSize = new Size(640, 420);
        web.Dock = DockStyle.Fill;
        web.DefaultBackgroundColor = Launcher.Background;  // no white flash while it loads
        Controls.Add(web);
        Launcher.LoadBounds(this);
        Shown += OnShown;
        FormClosing += (s, e) => Launcher.SaveBounds(this);
    }

    async void OnShown(object sender, EventArgs e)
    {
        Task<string> ready = server.Ready(args, port);  // the server starts while the window's engine loads
        try
        {
            var env = await CoreWebView2Environment.CreateAsync(null, Path.Combine(Launcher.DataDir, "WebView2"));
            await web.EnsureCoreWebView2Async(env);
        }
        catch (Exception ex)
        {
            Launcher.App.Switch(new StatusForm(server, args, port, "The app window could not start (" + ex.Message + "), so OpenReSense runs in your web browser."));
            return;
        }
        CoreWebView2 core = web.CoreWebView2;
        core.Settings.AreDevToolsEnabled = devtools;
        core.Settings.IsStatusBarEnabled = false;
        core.Settings.IsGeneralAutofillEnabled = false;
        core.Settings.IsPasswordAutosaveEnabled = false;
        core.DocumentTitleChanged += (s, a) => { Text = string.IsNullOrEmpty(core.DocumentTitle) ? "OpenReSense" : core.DocumentTitle; };
        core.NewWindowRequested += (s, a) => { a.Handled = true; Launcher.OpenExternal(a.Uri); };  // target=_blank links
        core.NavigationStarting += (s, a) =>
        {
            string u = a.Uri ?? "";
            if ((appUrl != null && u.StartsWith(appUrl, StringComparison.OrdinalIgnoreCase)) || u.StartsWith("blob:")) return;
            if (ownPage && (u.StartsWith("data:") || u.StartsWith("about:"))) { ownPage = false; return; }
            a.Cancel = true;  // a web page (the user's browser shows it), or Back to a message page that is over
            Launcher.OpenExternal(u);
        };
        core.ProcessFailed += (s, a) =>
        {
            if (a.ProcessFailedKind == CoreWebView2ProcessFailedKind.RenderProcessExited && ++crashes <= 3)
                core.Reload();  // the page crashed: load it again
            else if (a.ProcessFailedKind == CoreWebView2ProcessFailedKind.BrowserProcessExited)
            {
                MessageBox.Show(this, "The window's browser engine (WebView2) stopped. Start OpenReSense again.", "OpenReSense", MessageBoxButtons.OK, MessageBoxIcon.Error);
                Close();
            }
        };
        Message("Starting OpenReSense…", null, true);
        string why = await ready;
        if (IsDisposed) return;
        if (why != null)
        {
            Message("OpenReSense could not start.", why, false);
            return;
        }
        appUrl = server.Url;
        Action stopped = () => Message("OpenReSense stopped unexpectedly. Close this window and start it again.", server.Tail() + "\n\nLog: " + server.LogPath, false);
        server.Stopped += () =>  // the server ended on its own (not because the window closed)
        {
            try { BeginInvoke(stopped); }
            catch (InvalidOperationException) { }  // the window is closing anyway
        };
        ownPage = false;
        core.Navigate(appUrl);
        if (server.HasStopped) stopped();  // it ended before we listened
    }

    void Message(string title, string detail, bool spinner)  // the window's own page (not in Back/Forward)
    {
        ownPage = true;
        web.CoreWebView2.NavigateToString(Launcher.Page(title, detail, spinner));
    }
}

// The app in the default browser, with a small window that shows it runs (closing it stops the app).
sealed class StatusForm : AppWindow
{
    readonly Server server;
    readonly Label text = new Label();
    readonly Button open = new Button(), stop = new Button();

    public StatusForm(Server server, List<string> args, int port, string note)
    {
        this.server = server;
        AutoScaleMode = AutoScaleMode.Dpi;
        Font = SystemFonts.MessageBoxFont;
        FormBorderStyle = FormBorderStyle.FixedSingle;
        MaximizeBox = false;
        StartPosition = FormStartPosition.CenterScreen;
        ClientSize = new Size(520, 10);
        var panel = new FlowLayoutPanel { FlowDirection = FlowDirection.TopDown, WrapContents = false, Padding = new Padding(16), AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink };
        text.AutoSize = true;
        text.MaximumSize = new Size(480, 0);
        text.Text = "Starting OpenReSense…";
        panel.Controls.Add(text);
        if (note != null)
        {
            var tip = new LinkLabel { AutoSize = true, MaximumSize = new Size(480, 0), Margin = new Padding(0, 12, 0, 0) };
            tip.Text = note + (note.EndsWith(":") ? " " + Launcher.WebView2Download : "");
            if (note.EndsWith(":")) tip.LinkArea = new LinkArea(note.Length + 1, Launcher.WebView2Download.Length);
            else tip.LinkArea = new LinkArea(0, 0);
            tip.LinkClicked += (s, e) => Launcher.OpenExternal(Launcher.WebView2Download);
            panel.Controls.Add(tip);
        }
        var buttons = new FlowLayoutPanel { AutoSize = true, Margin = new Padding(0, 16, 0, 0) };
        open.Text = "Open in browser";
        open.AutoSize = true;
        open.Enabled = false;
        open.Click += (s, e) => Launcher.OpenExternal(server.Url);
        stop.Text = "Stop OpenReSense";
        stop.AutoSize = true;
        stop.Click += (s, e) => Close();
        buttons.Controls.Add(open);
        buttons.Controls.Add(stop);
        panel.Controls.Add(buttons);
        Controls.Add(panel);
        AutoSize = true;
        AutoSizeMode = AutoSizeMode.GrowAndShrink;
        Shown += async (s, e) =>
        {
            string why = await server.Ready(args, port);
            if (IsDisposed) return;
            if (why != null) { text.Text = "OpenReSense could not start:\n\n" + Launcher.LastLines(why, 12) + "\n\nLog: " + server.LogPath; return; }
            if (server.Owned)
            {
                text.Text = "OpenReSense is running at " + server.Url + " in your web browser.\nClose this window to stop it.";
                Action stopped = () =>
                {
                    text.Text = "OpenReSense stopped unexpectedly. Close this window and start it again.\n\nLog: " + server.LogPath;
                    open.Enabled = false;
                };
                server.Stopped += () => { try { BeginInvoke(stopped); } catch (InvalidOperationException) { } };
                if (server.HasStopped) { stopped(); return; }
            }
            else
            {
                text.Text = "OpenReSense is already running at " + server.Url + " and opens in your web browser.\nClosing this window leaves it running.";
                stop.Text = "Close";
            }
            open.Enabled = true;
            AcceptButton = open;
            Launcher.OpenExternal(server.Url);
        };
    }

    public override void Wake()  // started again: show the app again
    {
        base.Wake();
        if (open.Enabled) Launcher.OpenExternal(server.Url);
    }
}

// The Python server: started hidden, its output in cache\server.log, ended with this launcher (a job object kills it
// even if the launcher itself is killed).
sealed class Server : IDisposable
{
    readonly string python;
    readonly Queue<string> tail = new Queue<string>();  // last output lines, for error messages
    readonly object gate = new object();
    Process proc;
    IntPtr job = IntPtr.Zero;
    StreamWriter log;
    long logged;
    bool stopping;
    Task<string> ready;
    public int Port;
    public bool Owned;  // false when another copy of the app already serves the port
    public event Action Stopped;
    public string Url { get { return "http://127.0.0.1:" + Port + "/"; } }
    string logPath = Path.Combine(Launcher.Root, "cache", "server.log");
    public string LogPath { get { return logPath; } }

    public Server(string python) { this.python = python; }

    // Without a window: run the server like the console launcher did and return its exit code.
    public int Run(List<string> args, int port)
    {
        Port = port;
        try { Start(args); }
        catch (Exception) { return 1; }  // python.exe would not start
        proc.WaitForExit();
        return proc.ExitCode;
    }

    // Starts the server once (null when it answers, else why not) on the first port from 'first' on that is free, or
    // uses this folder's server where one already runs (then it is left running).
    public Task<string> Ready(List<string> args, int first)
    {
        lock (gate)
            return ready ?? (ready = Task.Run(() => StartAndWait(args, first)));
    }

    // The server ended on its own after it had answered.
    public bool HasStopped { get { return Owned && proc != null && proc.HasExited; } }

    string StartAndWait(List<string> args, int first)
    {
        try
        {
            int last = Math.Min(first + 19, 65535);
            for (Port = first; Port <= last; Port++)
            {
                if (Free(Port)) break;
                if (IsOurs(Port)) return null;  // this folder's server, e.g. one started with --no-browser
            }
            if (Port > last) return "No free port from " + first + " to " + last + ".";
            Start(args);
            DateTime until = DateTime.UtcNow.AddSeconds(120);  // the first start compiles the Python files
            while (DateTime.UtcNow < until)
            {
                if (proc.HasExited)
                {
                    proc.WaitForExit();  // the rest of its output
                    if (proc.ExitCode == 0 && IsOurs(Port)) { Owned = false; return null; }  // ours started there first
                    return "The server stopped (exit code " + proc.ExitCode + "):\n\n" + Tail();
                }
                if (Listening(Port, 300) && IsOurs(Port) && !proc.HasExited)
                {
                    proc.Exited += (s, e) =>
                    {
                        if (stopping) return;
                        try { proc.WaitForExit(); Action h = Stopped; if (h != null) h(); }
                        catch (Exception) { }  // ending anyway
                    };
                    proc.EnableRaisingEvents = true;
                    return null;
                }
                Thread.Sleep(200);
            }
            return "OpenReSense did not answer within 2 minutes.\n\n" + Tail();
        }
        catch (Exception ex)
        {
            return ex.Message;
        }
    }

    void Start(List<string> args)
    {
        var all = new List<string>(args);
        all.Add("--port"); all.Add(Port.ToString());
        all.Add("--no-browser");
        var psi = new ProcessStartInfo(python, "-m server " + string.Join(" ", all.Select(Quote)))
        {
            UseShellExecute = false, CreateNoWindow = true, WorkingDirectory = Launcher.Root,
            RedirectStandardOutput = true, RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8,
        };
        psi.EnvironmentVariables["PYTHONUTF8"] = "1";
        psi.EnvironmentVariables["PYTHONUNBUFFERED"] = "1";
        try
        {
            string dir = Path.Combine(Launcher.Root, "cache");
            Directory.CreateDirectory(dir);
            foreach (string name in new[] { "server.log", "server-" + Port + ".log" })  // another copy may be writing server.log
            {
                try
                {
                    string path = Path.Combine(dir, name);
                    log = new StreamWriter(new FileStream(path, FileMode.Create, FileAccess.Write, FileShare.Read), new UTF8Encoding(false)) { AutoFlush = true };
                    logPath = path;
                    break;
                }
                catch (IOException) { }
            }
        }
        catch (Exception) { }  // a folder we cannot write to: the server will say so itself
        proc = Process.Start(psi);
        Owned = true;
        EndWithUs(proc);
        proc.OutputDataReceived += (s, e) => Line(e.Data);
        proc.ErrorDataReceived += (s, e) => Line(e.Data);
        proc.BeginOutputReadLine();
        proc.BeginErrorReadLine();
    }

    void Line(string s)
    {
        if (s == null) return;
        lock (tail)
        {
            tail.Enqueue(s);
            while (tail.Count > 40) tail.Dequeue();
            if (log == null || logged > (8 << 20)) return;
            try
            {
                log.WriteLine(s);
                logged += s.Length + 2;
                if (logged > (8 << 20)) log.WriteLine("(the log stops here at 8 MB)");
            }
            catch (Exception) { log = null; }  // disk full or similar: keep running without the log
        }
    }

    public string Tail() { lock (tail) return string.Join("\n", tail); }

    // Something accepts connections there. A listener on this PC answers at once, while Windows takes about 2 s to
    // report a refused connection: a short wait tells them apart.
    static bool Listening(int port, int ms)
    {
        using (var c = new TcpClient())
        {
            try
            {
                IAsyncResult ar = c.BeginConnect(IPAddress.Loopback, port, null, null);
                if (!ar.AsyncWaitHandle.WaitOne(ms)) return false;
                c.EndConnect(ar);
                return true;
            }
            catch (SocketException) { return false; }
        }
    }

    static bool Free(int port)  // nothing listens there, and we may bind it (Windows reserves some port ranges)
    {
        if (Listening(port, 300)) return false;
        try
        {
            var l = new TcpListener(IPAddress.Loopback, port) { ExclusiveAddressUse = true };
            l.Start();
            l.Stop();
            return true;
        }
        catch (SocketException) { return false; }
    }

    static bool IsOurs(int port)  // this folder's OpenReSense answers there (not another copy's, or another user's)
    {
        try
        {
            var req = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:" + port + "/api/instance");
            req.Proxy = null;
            req.Timeout = req.ReadWriteTimeout = 3000;
            using (WebResponse resp = req.GetResponse())
            using (Stream s = resp.GetResponseStream())
            {
                var buf = new byte[1024];
                int n = 0, r;
                while (n < buf.Length && (r = s.Read(buf, n, buf.Length - n)) > 0) n += r;
                return Encoding.UTF8.GetString(buf, 0, n).Contains("\"" + Launcher.RootId + "\"");
            }
        }
        catch (WebException) { return false; }
        catch (IOException) { return false; }
    }

    static string Quote(string a)  // Windows command-line quoting, as the C runtime splits it again
    {
        if (a.Length > 0 && a.IndexOfAny(new[] { ' ', '\t', '"' }) < 0)
            return a;
        var sb = new StringBuilder("\"");
        int slashes = 0;
        foreach (char ch in a)
        {
            if (ch == '\\') { slashes++; continue; }
            sb.Append('\\', ch == '"' ? slashes * 2 + 1 : slashes);  // backslashes before a quote are doubled, plus one
            slashes = 0;
            sb.Append(ch);
        }
        return sb.Append('\\', slashes * 2).Append('"').ToString();  // and before the closing quote
    }

    void EndWithUs(Process p)
    {
        job = CreateJobObject(IntPtr.Zero, null);
        var info = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
        info.BasicLimitInformation.LimitFlags = 0x2000;  // JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if (job != IntPtr.Zero && SetInformationJobObject(job, 9, ref info, (uint)Marshal.SizeOf(info)))
            AssignProcessToJobObject(job, p.Handle);  // best effort: without it the window still stops the server
    }

    public void Dispose()
    {
        stopping = true;
        if (Owned && proc != null)
        {
            try { if (!proc.HasExited) proc.Kill(); }
            catch (Exception) { }  // already gone
        }
        if (job != IntPtr.Zero) { CloseHandle(job); job = IntPtr.Zero; }
        lock (tail) { if (log != null) { try { log.Dispose(); } catch (Exception) { } log = null; } }
    }

    [StructLayout(LayoutKind.Sequential)]
    struct JOBOBJECT_BASIC_LIMIT_INFORMATION
    {
        public long PerProcessUserTimeLimit, PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize, MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass, SchedulingClass;
    }
    [StructLayout(LayoutKind.Sequential)]
    struct IO_COUNTERS { public ulong ReadOps, WriteOps, OtherOps, ReadBytes, WriteBytes, OtherBytes; }
    [StructLayout(LayoutKind.Sequential)]
    struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    {
        public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
        public IO_COUNTERS IoInfo;
        public UIntPtr ProcessMemoryLimit, JobMemoryLimit, PeakProcessMemoryUsed, PeakJobMemoryUsed;
    }
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] static extern IntPtr CreateJobObject(IntPtr attributes, string name);
    [DllImport("kernel32.dll")] static extern bool SetInformationJobObject(IntPtr job, int infoClass, ref JOBOBJECT_EXTENDED_LIMIT_INFORMATION info, uint size);
    [DllImport("kernel32.dll")] static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
}
