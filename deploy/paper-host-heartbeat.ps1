param(
    [Parameter(Mandatory=$true)][string]$OutputPath,
    [Parameter(Mandatory=$true)][ValidateSet(3600,86400)][int]$Seconds
)
$ErrorActionPreference = 'Stop'
# Host evidence only. Does not change Windows power policy or start trading services.
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
    Move-Item -Force -LiteralPath $temp -Destination $OutputPath
    Add-Content -LiteralPath "$OutputPath.jsonl" -Value $value -Encoding UTF8
    $lastWall = $now
    $lastTick = $tick
    Start-Sleep -Seconds 5
}
