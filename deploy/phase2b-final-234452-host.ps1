param(
    [Parameter(Mandatory=$true)][string]$OutputPath,
    [Parameter(Mandatory=$true)][ValidateSet(360,3600,86400)][int]$Seconds
)
$ErrorActionPreference = 'Stop'
# Host evidence only. Does not change Windows power policy or start trading services.

# C# helper for atomic file replacement on NTFS using MoveFileEx Win32 API
Add-Type -TypeDefinition @"
using System;
using System.IO;
using System.Runtime.InteropServices;
public static class AtomicFileReplace {
    [DllImport("kernel32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
    private static extern bool MoveFileEx(string lpExistingFileName, string lpNewFileName, uint dwFlags);

    // MOVEFILE_REPLACE_EXISTING = 0x1 (replace existing destination atomically on NTFS)
    public static void AtomicReplace(string tempPath, string destPath) {
        for (int attempt = 0; attempt < 10; attempt++) {
            try {
                bool result = MoveFileEx(tempPath, destPath, 0x1);
                if (result) return;
                int err = Marshal.GetLastWin32Error();
                throw new IOException("MoveFileEx failed: " + err);
            } catch (IOException) {
                if (attempt >= 9) throw;
                System.Threading.Thread.Sleep(50 * (attempt + 1));
            }
        }
    }
}
"@

$started = [DateTime]::UtcNow
$deadline = $started.AddSeconds($Seconds + 120)
$lastWall = $started
$lastTick = ([System.Diagnostics.Stopwatch]::GetTimestamp() * 1000.0 / [System.Diagnostics.Stopwatch]::Frequency)
$interrupted = $false
$session = [Guid]::NewGuid().ToString()
$boot = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString('o')
$sequence = 0
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $OutputPath) | Out-Null

while ([DateTime]::UtcNow -lt $deadline) {
    $now = [DateTime]::UtcNow
    $tick = ([System.Diagnostics.Stopwatch]::GetTimestamp() * 1000.0 / [System.Diagnostics.Stopwatch]::Frequency)
    $elapsed = ($now - $lastWall).TotalSeconds
    if ($elapsed -lt 0 -or $elapsed -gt 20 -or [Math]::Abs($elapsed - ($tick-$lastTick)/1000.0) -gt 2) {
        $interrupted = $true
    }
    $readable = $true
    $events = @()
    try {
        $log = Get-WinEvent -ListLog System -ErrorAction Stop
        if (-not $log.IsEnabled -or $log.RecordCount -lt 1) { throw 'System event log unavailable' }
        $events = @(Get-WinEvent -FilterHashtable @{
            LogName='System'; StartTime=$started.ToLocalTime()
            Id=1,12,13,42,107,506,507
        } -ErrorAction Stop | Where-Object {
            $_.ProviderName -in @('Microsoft-Windows-Kernel-Power','Microsoft-Windows-Power-Troubleshooter','Microsoft-Windows-Kernel-General')
        } | ForEach-Object { @{id=$_.Id;provider=$_.ProviderName;at=$_.TimeCreated.ToUniversalTime().ToString('o')} })
        if ($events.Count -gt 0) { $interrupted = $true }
    } catch {
        if ($_.FullyQualifiedErrorId -notlike 'NoMatchingEventsFound*') { $readable = $false }
    }
    $sequence++
    $value = @{
        session=$session; boot=$boot; sequence=$sequence
        wall=([DateTimeOffset]$now).ToUnixTimeMilliseconds()/1000.0
        started=$started.ToString('o'); interrupted=$interrupted
        power_events_readable=$readable; events=$events
        tick_ms=$tick
    } | ConvertTo-Json -Depth 5 -Compress
    $temp = "$OutputPath.tmp"
    [System.IO.File]::WriteAllText($temp, $value, [System.Text.UTF8Encoding]::new($false))
    if (-not (Test-Path -LiteralPath $OutputPath)) {
        [System.IO.File]::Move($temp, $OutputPath)  # initial create
    } else {
        [AtomicFileReplace]::AtomicReplace($temp, $OutputPath)  # atomic replace via MoveFileEx
        Start-Sleep -Milliseconds 5  # drvfs cache propagation for WSL /mnt/c reads
    }
    [System.IO.File]::AppendAllText("$OutputPath.jsonl", $value + "`n", [System.Text.UTF8Encoding]::new($false))  # no BOM
    $lastWall = $now
    $lastTick = $tick
    Start-Sleep -Seconds 5
}
