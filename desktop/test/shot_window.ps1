param(
    [Parameter(Mandatory = $true)][int]$AppPid,
    [Parameter(Mandatory = $true)][string]$Out,
    [string]$TitleLike = "LiveNotes /"
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
public struct LN_RECT { public int Left, Top, Right, Bottom; }
public class LN_Win32 {
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out LN_RECT rect);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc cb, IntPtr lParam);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int max);
    [DllImport("user32.dll")] public static extern int GetWindowTextLength(IntPtr hWnd);

    public static IntPtr FindByPidAndTitle(int pid, string titleLike) {
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
            LN_RECT r;
            if (!GetWindowRect(h, out r)) return true;
            long area = (long)(r.Right - r.Left) * (r.Bottom - r.Top);
            if (area > bestArea) { bestArea = area; best = h; }
            return true;
        }, IntPtr.Zero);
        return best;
    }
}
"@

[LN_Win32]::SetProcessDPIAware() | Out-Null
$proc = Get-Process -Id $AppPid
$hwnd = [IntPtr]::Zero
if ($TitleLike) {
    $hwnd = [LN_Win32]::FindByPidAndTitle($AppPid, $TitleLike)
}
if ($hwnd -eq [IntPtr]::Zero) {
    $hwnd = $proc.MainWindowHandle
}
if ($hwnd -eq [IntPtr]::Zero) { throw "process $AppPid has no matching window" }
if ([LN_Win32]::IsIconic($hwnd)) {
    [LN_Win32]::ShowWindow($hwnd, 9) | Out-Null
    Start-Sleep -Milliseconds 500
}
[LN_Win32]::SetForegroundWindow($hwnd) | Out-Null
[LN_Win32]::BringWindowToTop($hwnd) | Out-Null
Start-Sleep -Milliseconds 800

$rect = New-Object LN_RECT
[LN_Win32]::GetWindowRect($hwnd, [ref]$rect) | Out-Null
$w = $rect.Right - $rect.Left
$h = $rect.Bottom - $rect.Top
if ($w -le 0 -or $h -le 0) { throw "bad window rect ${w}x${h}" }

$bmp = New-Object System.Drawing.Bitmap $w, $h
$g = [System.Drawing.Graphics]::FromImage($bmp)
try {
    $g.CopyFromScreen($rect.Left, $rect.Top, 0, 0, $bmp.Size)
    $dir = Split-Path -Parent $Out
    if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
    $bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
} finally {
    $g.Dispose()
    $bmp.Dispose()
}
Write-Output ("{0} {1}x{2} at {3},{4}" -f $Out, $w, $h, $rect.Left, $rect.Top)
