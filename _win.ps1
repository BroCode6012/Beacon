# Signal UI verification helper: focus the window, optionally click/scroll, screenshot.
# Usage:
#   powershell -File _win.ps1 -Out shot.png                     # focus + capture
#   powershell -File _win.ps1 -ClickX 0.5 -ClickY 0.3 -Out s.png  # click (fractional of window) then capture
#   powershell -File _win.ps1 -Scroll -400 -Out s.png           # wheel scroll then capture
param(
  [string]$Title = "Signal*Niche Finder*",
  [int]$RootProcessId = 0,
  [string]$Out = "shot.png",
  [double]$ClickX = -1,
  [double]$ClickY = -1,
  [int]$Scroll = 0,
  [string]$Type = "",
  [switch]$Enter,
  [int]$Wait = 700
)

Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Win {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
  [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint a, uint b, bool f);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint dx, uint dy, int data, UIntPtr extra);
  [DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint f, UIntPtr extra);
  [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr h);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  public struct RECT { public int Left, Top, Right, Bottom; }
}
"@

# Make THIS process DPI-aware so GetWindowRect / SetCursorPos / the screenshot all
# share one physical-pixel coordinate space (otherwise clicks land off under scaling).
[Win]::SetProcessDPIAware() | Out-Null

# Pick the intended app window. For isolated verification, RootProcessId may be
# the launcher/stub process; include all descendants and select their largest
# real window. Without it, preserve the original title-based behavior.
if ($RootProcessId -gt 0) {
  $all = @(Get-CimInstance Win32_Process)
  $wanted = @($RootProcessId)
  do {
    $new = @($all | Where-Object {
      $wanted -contains [int]$_.ParentProcessId -and
      $wanted -notcontains [int]$_.ProcessId
    } | ForEach-Object { [int]$_.ProcessId })
    if ($new) { $wanted += $new }
  } while ($new)
  $cands = @(Get-Process -Id ($wanted | Select-Object -Unique) -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowHandle -ne 0 })
} else {
  $cands = @(Get-Process | Where-Object {
    $_.MainWindowTitle -like $Title -and $_.MainWindowHandle -ne 0
  })
}
if (-not $cands) { Write-Output "NO_WINDOW"; exit 1 }
$h = [IntPtr]::Zero; $bestArea = -1; $bestPid = 0
foreach ($c in $cands) {
  $rr = New-Object Win+RECT
  [Win]::GetWindowRect($c.MainWindowHandle, [ref]$rr) | Out-Null
  $area = ($rr.Right - $rr.Left) * ($rr.Bottom - $rr.Top)
  if ($area -gt $bestArea) {
    $bestArea = $area
    $h = $c.MainWindowHandle
    $bestPid = $c.Id
  }
}

# Restore (un-minimize) + raise. Retry until it's actually on-screen — never
# minimize first (a fast restore after minimize can be ignored, stranding it).
for ($i = 0; $i -lt 6; $i++) {
  [Win]::ShowWindow($h, 9) | Out-Null       # SW_RESTORE
  Start-Sleep -Milliseconds 250
  $rc = New-Object Win+RECT
  [Win]::GetWindowRect($h, [ref]$rc) | Out-Null
  if ($rc.Left -gt -10000 -and ($rc.Right - $rc.Left) -gt 300) { break }
}
# ALT-tap first: Windows only lets you steal foreground right after synthetic
# input, so a benign Alt down/up unlocks SetForegroundWindow past an actively
# foregrounded app (e.g. a playing video). Then AttachThreadInput as a backstop.
[Win]::keybd_event(0x12, 0, 0, [UIntPtr]::Zero)          # ALT down
[Win]::keybd_event(0x12, 0, 2, [UIntPtr]::Zero)          # ALT up
$fg = [Win]::GetForegroundWindow()
$t1 = [Win]::GetWindowThreadProcessId($fg, [ref]([uint32]0))
$t2 = [Win]::GetCurrentThreadId()
[Win]::AttachThreadInput($t1, $t2, $true) | Out-Null
[Win]::BringWindowToTop($h) | Out-Null
[Win]::SetForegroundWindow($h) | Out-Null
[Win]::ShowWindow($h, 9) | Out-Null
[Win]::AttachThreadInput($t1, $t2, $false) | Out-Null
Start-Sleep -Milliseconds 500

$r = New-Object Win+RECT
[Win]::GetWindowRect($h, [ref]$r) | Out-Null
$w = $r.Right - $r.Left
$hh = $r.Bottom - $r.Top

if ($ClickX -ge 0 -and $ClickY -ge 0) {
  $cx = [int]($r.Left + $w * $ClickX)
  $cy = [int]($r.Top + $hh * $ClickY)
  [Win]::SetCursorPos($cx, $cy) | Out-Null
  Start-Sleep -Milliseconds 120
  [Win]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)  # left down
  [Win]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)  # left up
  Start-Sleep -Milliseconds $Wait
}

if ($Type -ne "") {
  Add-Type -AssemblyName System.Windows.Forms
  Start-Sleep -Milliseconds 200
  [System.Windows.Forms.SendKeys]::SendWait($Type)   # note: avoid + ^ % ~ ( ) { } [ ] in text
  if ($Enter) { Start-Sleep -Milliseconds 200; [System.Windows.Forms.SendKeys]::SendWait("{ENTER}") }
  Start-Sleep -Milliseconds $Wait
}

if ($Scroll -ne 0) {
  $cx = [int]($r.Left + $w * 0.5)
  $cy = [int]($r.Top + $hh * 0.5)
  [Win]::SetCursorPos($cx, $cy) | Out-Null
  Start-Sleep -Milliseconds 120
  [Win]::mouse_event(0x0800, 0, 0, [int]$Scroll, [UIntPtr]::Zero)  # wheel (signed: -down/+up)
  Start-Sleep -Milliseconds $Wait
}

# full-screen capture (dialogs/overlays render above the window, so grab the screen)
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$b = [System.Windows.Forms.SystemInformation]::VirtualScreen
$bmp = New-Object System.Drawing.Bitmap($b.Width, $b.Height)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($b.Left, $b.Top, 0, 0, $bmp.Size)
$bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
Write-Output "OK pid=$bestPid $w x $hh -> $Out"
