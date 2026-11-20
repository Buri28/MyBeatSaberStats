param([int]$Hwnd, [string]$Out)
Add-Type @"
using System; using System.Runtime.InteropServices;
public class W { [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
[DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint f);
public struct RECT { public int L,T,R,B; } }
"@
Add-Type -AssemblyName System.Drawing
$r = New-Object W+RECT; [W]::GetWindowRect([IntPtr]$Hwnd, [ref]$r) | Out-Null
$w = $r.R - $r.L; $h = $r.B - $r.T
$bmp = New-Object System.Drawing.Bitmap $w, $h
$g = [System.Drawing.Graphics]::FromImage($bmp); $hdc = $g.GetHdc()
[W]::PrintWindow([IntPtr]$Hwnd, $hdc, 2) | Out-Null
$g.ReleaseHdc($hdc); $g.Dispose(); $bmp.Save($Out); $bmp.Dispose()
"saved $Out ${w}x${h}"
