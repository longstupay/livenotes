using System;
using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;
using System.Text;

// 和 shot_window.ps1 等价，但编译后启动只要几十毫秒：
// 自动化验收按打点截图时，PowerShell 每次 Add-Type 的几秒延迟会让截图错过状态。
public class WinShot
{
    public struct RECT { public int Left, Top, Right, Bottom; }
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc cb, IntPtr lParam);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int max);
    [DllImport("user32.dll")] public static extern int GetWindowTextLength(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr after, int x, int y, int cx, int cy, uint flags);
    [DllImport("user32.dll")] public static extern int GetSystemMetrics(int index);
    [DllImport("dwmapi.dll")] public static extern int DwmGetWindowAttribute(IntPtr hWnd, int attr, out RECT value, int size);

    static IntPtr FindWindow(int pid, string titleLike)
    {
        IntPtr best = IntPtr.Zero;
        long bestArea = 0;
        EnumWindows(delegate(IntPtr h, IntPtr l) {
            uint p;
            GetWindowThreadProcessId(h, out p);
            if (p != (uint)pid || !IsWindowVisible(h)) return true;
            int len = GetWindowTextLength(h);
            if (len <= 0) return true;
            var sb = new StringBuilder(len + 2);
            GetWindowText(h, sb, sb.Capacity);
            string title = sb.ToString();
            if (!String.IsNullOrEmpty(titleLike) &&
                title.IndexOf(titleLike, StringComparison.OrdinalIgnoreCase) < 0) return true;
            RECT r;
            if (!GetWindowRect(h, out r)) return true;
            long area = (long)(r.Right - r.Left) * (r.Bottom - r.Top);
            if (area > bestArea) { bestArea = area; best = h; }
            return true;
        }, IntPtr.Zero);
        return best;
    }

    public static int Main(string[] args)
    {
        if (args.Length < 2)
        {
            Console.Error.WriteLine("usage: win_shot.exe <pid> <out.png> [title]");
            return 2;
        }
        int pid = int.Parse(args[0]);
        string outPath = args[1];
        string titleLike = args.Length > 2 ? args[2] : "LiveNotes /";

        SetProcessDPIAware();
        IntPtr hwnd = FindWindow(pid, titleLike);
        if (hwnd == IntPtr.Zero && titleLike.Length > 0) hwnd = FindWindow(pid, "");
        if (hwnd == IntPtr.Zero)
        {
            Console.Error.WriteLine("no matching visible window");
            return 3;
        }
        if (IsIconic(hwnd))
        {
            ShowWindow(hwnd, 9);
            System.Threading.Thread.Sleep(400);
        }
        SetForegroundWindow(hwnd);
        BringWindowToTop(hwnd);
        System.Threading.Thread.Sleep(350);

        RECT rect;
        if (!GetWindowRect(hwnd, out rect)) return 4;
        int w = rect.Right - rect.Left, h = rect.Bottom - rect.Top;
        if (w <= 0 || h <= 0) return 5;
        int origLeft = rect.Left, origTop = rect.Top;

        // 截图时把窗口挪到左上角：系统通知横幅固定在屏幕右下角，
        // CopyFromScreen 会把压在窗口上的通知也拍进去。挪开再拍，拍完还原。
        int screenW = GetSystemMetrics(0), screenH = GetSystemMetrics(1);
        bool moved = false;
        if (w <= screenW && h <= screenH && (rect.Left != 0 || rect.Top != 0))
        {
            if (SetWindowPos(hwnd, IntPtr.Zero, 0, 0, 0, 0, 0x0001 | 0x0004 | 0x0010))
            {
                moved = true;
                System.Threading.Thread.Sleep(300);
                GetWindowRect(hwnd, out rect);
                w = rect.Right - rect.Left; h = rect.Bottom - rect.Top;
            }
        }

        // GetWindowRect 含不可见的缩放边框；DWM 的扩展边框才是用户看到的窗口。
        RECT vis;
        if (DwmGetWindowAttribute(hwnd, 9, out vis, Marshal.SizeOf(typeof(RECT))) == 0 &&
            vis.Right > vis.Left && vis.Bottom > vis.Top)
        {
            int vw = vis.Right - vis.Left, vh = vis.Bottom - vis.Top;
            if (vw <= w && vh <= h) { rect = vis; w = vw; h = vh; }
        }

        using (var bmp = new Bitmap(w, h))
        using (var g = Graphics.FromImage(bmp))
        {
            g.CopyFromScreen(rect.Left, rect.Top, 0, 0, bmp.Size);
            bmp.Save(outPath, ImageFormat.Png);
        }
        Console.WriteLine("{0} {1}x{2} at {3},{4}", outPath, w, h, rect.Left, rect.Top);
        if (moved)
        {
            SetWindowPos(hwnd, IntPtr.Zero, origLeft, origTop, 0, 0, 0x0001 | 0x0004 | 0x0010);
        }
        return 0;
    }
}
