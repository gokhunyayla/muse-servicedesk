<#
  Muse ServiceDesk endpoint agent (Windows PowerShell 5.1+).

  Only calls out to the desk over HTTPS: a heartbeat with health metrics every minute and a long-poll for jobs.
  Runs nothing but the allow-listed scripts below; anything else the desk might ask for is refused here too.

  Install (elevated PowerShell; the console shows this line when an admin enrols the device):
    powershell -ExecutionPolicy Bypass -File muse-agent.ps1 -Install -Desk https://desk.example.com -Token mda_...
  Uninstall:
    powershell -ExecutionPolicy Bypass -File muse-agent.ps1 -Uninstall
#>
param(
  [switch]$Install,
  [switch]$Uninstall,
  [switch]$Run,
  [string]$Desk = "",
  [string]$Token = ""
)

$ErrorActionPreference = "Stop"
$Version = "1.1.0"
$Home_ = Join-Path $env:ProgramData "MuseAgent"
$ConfigFile = Join-Path $Home_ "config.json"
$LogFile = Join-Path $Home_ "agent.log"
$TaskName = "MuseServiceDeskAgent"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Log($msg) {
  try {
    $line = "{0:u} {1}" -f (Get-Date), $msg
    Add-Content -Path $LogFile -Value $line
    if ((Get-Item $LogFile).Length -gt 2MB) { Move-Item $LogFile "$LogFile.1" -Force }
  } catch {}
}

# ---- install / uninstall ------------------------------------------------------------------------

if ($Install) {
  if (-not $Desk -or -not $Token) { throw "-Desk and -Token are required" }
  New-Item -ItemType Directory -Force -Path $Home_ | Out-Null
  Copy-Item -Path $PSCommandPath -Destination (Join-Path $Home_ "muse-agent.ps1") -Force
  @{ desk = $Desk.TrimEnd("/"); token = $Token } | ConvertTo-Json | Set-Content -Path $ConfigFile -Encoding UTF8
  # only SYSTEM and administrators may read the token
  icacls $Home_ /inheritance:r /grant:r "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F" | Out-Null
  $action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$(Join-Path $Home_ 'muse-agent.ps1')`" -Run"
  $trigger = New-ScheduledTaskTrigger -AtStartup
  $settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
  $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal | Out-Null
  Start-ScheduledTask -TaskName $TaskName
  Write-Output "Muse agent $Version installed on $env:COMPUTERNAME; reporting to $Desk"
  exit 0
}

if ($Uninstall) {
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
  Remove-Item -Recurse -Force $Home_ -ErrorAction SilentlyContinue
  Write-Output "Muse agent removed"
  exit 0
}

# ---- talking to the desk ------------------------------------------------------------------------

$Cfg = Get-Content $ConfigFile -Raw | ConvertFrom-Json
$Headers = @{ Authorization = "Bearer $($Cfg.token)" }

function Call($method, $path, $body = $null, $timeout = 40) {
  $args_ = @{ Method = $method; Uri = "$($Cfg.desk)$path"; Headers = $Headers; TimeoutSec = $timeout; UseBasicParsing = $true }
  if ($body -ne $null) { $args_.Body = ($body | ConvertTo-Json -Depth 6 -Compress); $args_.ContentType = "application/json; charset=utf-8" }
  return Invoke-RestMethod @args_
}

# ---- health ---------------------------------------------------------------------------------------

function Get-LastPatchDays {
  $hf = Get-HotFix -ErrorAction SilentlyContinue | Where-Object { $_.InstalledOn } | Sort-Object InstalledOn -Descending | Select-Object -First 1
  if ($hf) { return [int]((Get-Date) - [datetime]$hf.InstalledOn).TotalDays }
  return -1
}

function Get-PendingReboot {
  return (Test-Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending") -or
         (Test-Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired")
}

function Get-Metrics {
  $os = Get-CimInstance Win32_OperatingSystem
  $disk = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='C:'"
  $cpu = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
  $dnsOk = $true
  try { Resolve-DnsName -Name "www.microsoft.com" -QuickTimeout -ErrorAction Stop | Out-Null } catch { $dnsOk = $false }
  $crit = 0
  try { $crit = @(Get-WinEvent -FilterHashtable @{ LogName = 'System'; Level = 1, 2; StartTime = (Get-Date).AddDays(-1) } -ErrorAction Stop).Count } catch {}
  $ip = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254*" } | Select-Object -First 1).IPAddress
  return @{
    disk_used_pct      = [int](100 - ($disk.FreeSpace / $disk.Size * 100))
    disk_free_gb       = [math]::Round($disk.FreeSpace / 1GB, 1)
    cpu_pct            = [int]$cpu
    mem_used_pct       = [int](100 - ($os.FreePhysicalMemory / $os.TotalVisibleMemorySize * 100))
    uptime_hours       = [int]((Get-Date) - $os.LastBootUpTime).TotalHours
    last_patch_days    = Get-LastPatchDays
    pending_reboot     = [bool](Get-PendingReboot)
    spooler            = [string](Get-Service -Name Spooler -ErrorAction SilentlyContinue).Status
    critical_events_24h = $crit
    dns_ok             = $dnsOk
    ip                 = $ip
  }
}

# ---- the allow-listed scripts ---------------------------------------------------------------------

function Invoke-Allowed($script) {
  switch ($script) {
    "dns-reset" {
      Clear-DnsClientCache
      ipconfig /registerdns | Out-Null
      $r = try { (Resolve-DnsName www.microsoft.com -ErrorAction Stop | Select-Object -First 1).Name } catch { "FAILED: $($_.Exception.Message)" }
      return "DNS cache cleared and registration renewed. Test lookup www.microsoft.com -> $r"
    }
    "vpn-reset" {
      $svc = Get-Service | Where-Object { $_.Name -match "vpn|wireguard|openvpn|forti|anyconnect|globalprotect|tailscale" -and $_.Status -ne "Disabled" }
      Clear-DnsClientCache
      if (-not $svc) { return "No VPN client is installed on this device. DNS cache cleared; nothing else to reset." }
      $svc | Restart-Service -Force
      return "Restarted VPN service(s): $(($svc | ForEach-Object Name) -join ', '); DNS cache cleared."
    }
    "printer-fix" {
      $spool = Join-Path $env:WINDIR "System32\spool\PRINTERS"
      Stop-Service -Name Spooler -Force
      $n = @(Get-ChildItem $spool -File -ErrorAction SilentlyContinue).Count
      Get-ChildItem $spool -File -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
      Start-Service -Name Spooler
      return "Print spooler restarted ($((Get-Service Spooler).Status)); $n stuck job file(s) removed from the queue."
    }
    "perf-report" {
      $m = Get-Metrics
      $top = Get-Process | Sort-Object CPU -Descending | Select-Object -First 5 | ForEach-Object { "{0} ({1:N0}s CPU, {2:N0} MB)" -f $_.ProcessName, $_.CPU, ($_.WorkingSet64 / 1MB) }
      $startup = @(Get-CimInstance Win32_StartupCommand -ErrorAction SilentlyContinue).Count
      return ("CPU {0}%, memory {1}% used, disk C: {2}% used ({3} GB free), up {4} h, {5} start-up item(s). Top processes: {6}" -f `
        $m.cpu_pct, $m.mem_used_pct, $m.disk_used_pct, $m.disk_free_gb, $m.uptime_hours, $startup, ($top -join "; "))
    }
    "disk-cleanup" {
      $before = (Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='C:'").FreeSpace
      $paths = @("$env:WINDIR\Temp", "$env:WINDIR\SoftwareDistribution\Download") + (Get-ChildItem "C:\Users" -Directory -ErrorAction SilentlyContinue | ForEach-Object { Join-Path $_.FullName "AppData\Local\Temp" })
      foreach ($p in $paths) {
        Get-ChildItem $p -Recurse -Force -ErrorAction SilentlyContinue | Where-Object { -not $_.PSIsContainer -and $_.LastWriteTime -lt (Get-Date).AddHours(-1) } |
          Remove-Item -Force -ErrorAction SilentlyContinue
      }
      Clear-RecycleBin -Force -ErrorAction SilentlyContinue
      $after = (Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='C:'").FreeSpace
      return ("Temporary files, old update downloads and the recycle bin cleaned: {0:N0} MB freed; C: now has {1:N1} GB free." -f (($after - $before) / 1MB), ($after / 1GB))
    }
    "lock-screen" {
      $sessions = @(quser 2>$null | Select-Object -Skip 1 | ForEach-Object { ($_ -replace '\s{2,}', ',').Trim(',').Split(',') } |
        Where-Object { $_ -match 'Active' })
      if (-not $sessions) { return "No signed-in user session on this device; nothing to lock." }
      $ids = foreach ($s in $sessions) { $s | Where-Object { $_ -match '^\d+$' } | Select-Object -First 1 }
      foreach ($id in $ids) { tsdiscon $id 2>$null }
      return "Locked $(@($ids).Count) active session(s); the user must sign in again."
    }
    "patch-status" {
      $hf = Get-HotFix -ErrorAction SilentlyContinue | Where-Object { $_.InstalledOn } | Sort-Object InstalledOn -Descending | Select-Object -First 3
      $pending = "unknown"
      try {
        $searcher = (New-Object -ComObject Microsoft.Update.Session).CreateUpdateSearcher()
        $pending = $searcher.Search("IsInstalled=0 and IsHidden=0").Updates.Count
      } catch {}
      $last = ($hf | ForEach-Object { "{0} ({1:yyyy-MM-dd})" -f $_.HotFixID, $_.InstalledOn }) -join ", "
      $run = Join-Path $Home_ "updates-result.txt"
      $lastRun = if (Test-Path $run) { " Last install run: " + ((Get-Content $run -Raw) -replace "`r?`n", " | ") } else { "" }
      return "Last updates: $last. Pending updates: $pending. Reboot pending: $([bool](Get-PendingReboot)).$lastRun"
    }
    "install-updates" {
      # Windows Update can take many minutes: hand it to a one-off SYSTEM task so this agent keeps answering,
      # and let patch-status report the outcome afterwards.
      $searcher = (New-Object -ComObject Microsoft.Update.Session).CreateUpdateSearcher()
      $todo = $searcher.Search("IsInstalled=0 and IsHidden=0 and Type='Software'").Updates
      if ($todo.Count -eq 0) { return "No pending updates; the device is up to date." }
      $titles = @(); foreach ($u in $todo) { $titles += $u.Title }
      $worker = Join-Path $Home_ "install-updates.ps1"
      @'
$ErrorActionPreference = "Continue"
$out = Join-Path $env:ProgramData "MuseAgent\updates-result.txt"
"started $((Get-Date).ToUniversalTime().ToString('u'))" | Set-Content $out
try {
  $s = New-Object -ComObject Microsoft.Update.Session
  $found = $s.CreateUpdateSearcher().Search("IsInstalled=0 and IsHidden=0 and Type='Software'").Updates
  $coll = New-Object -ComObject Microsoft.Update.UpdateColl
  foreach ($u in $found) { if (-not $u.EulaAccepted) { $u.AcceptEula() }; [void]$coll.Add($u) }
  $d = $s.CreateUpdateDownloader(); $d.Updates = $coll; [void]$d.Download()
  $i = $s.CreateUpdateInstaller(); $i.Updates = $coll; $r = $i.Install()
  $lines = for ($k = 0; $k -lt $coll.Count; $k++) { "{0}: {1}" -f $coll.Item($k).Title, @('not started','in progress','succeeded','succeeded with errors','failed','aborted')[$r.GetUpdateResult($k).ResultCode] }
  "finished $((Get-Date).ToUniversalTime().ToString('u')); reboot required: $($r.RebootRequired)`n$($lines -join "`n")" | Set-Content $out
} catch { "failed $((Get-Date).ToUniversalTime().ToString('u')): $($_.Exception.Message)" | Set-Content $out }
'@ | Set-Content -Path $worker -Encoding UTF8
      $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$worker`""
      $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
      Unregister-ScheduledTask -TaskName "MuseAgentInstallUpdates" -Confirm:$false -ErrorAction SilentlyContinue
      Register-ScheduledTask -TaskName "MuseAgentInstallUpdates" -Action $action -Principal $principal | Out-Null
      Start-ScheduledTask -TaskName "MuseAgentInstallUpdates"
      return "Started installing $($todo.Count) update(s) in the background: $($titles -join '; '). This usually takes 10-30 minutes; patch-status shows the outcome and whether a restart is needed."
    }
    "driver-rollback" {
      throw "driver rollback is not enabled on this device (servers keep their certified drivers); hand off to L2/L4"
    }
    default { throw "script '$script' is not allow-listed on this device" }
  }
}

# ---- main loop ----------------------------------------------------------------------------------

if ($Run) {
  Log "agent $Version starting on $env:COMPUTERNAME"
  $lastBeat = [datetime]::MinValue
  while ($true) {
    try {
      if (((Get-Date) - $lastBeat).TotalSeconds -ge 60) {
        $os = (Get-CimInstance Win32_OperatingSystem).Caption
        $hb = Call "POST" "/agent/heartbeat" @{ version = $Version; os = $os; metrics = (Get-Metrics) }
        $lastBeat = Get-Date
        if ($hb.agent_version -and $hb.agent_version -ne $Version) {
          # the desk ships a newer agent: fetch it, check it is ours, swap, and restart into it
          $new = Join-Path $Home_ "muse-agent.new.ps1"
          Invoke-WebRequest -UseBasicParsing -Uri "$($Cfg.desk)/agent/muse-agent.ps1" -OutFile $new
          if ((Get-Content $new -Raw) -match 'function Invoke-Allowed' -and (Get-Content $new -Raw) -match [regex]::Escape("`$Version = `"$($hb.agent_version)`"")) {
            Move-Item $new (Join-Path $Home_ "muse-agent.ps1") -Force
            Log "updating $Version -> $($hb.agent_version)"
            Start-Process powershell.exe -ArgumentList "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$(Join-Path $Home_ 'muse-agent.ps1')`" -Run" -WindowStyle Hidden
            exit 0
          }
          Remove-Item $new -Force -ErrorAction SilentlyContinue
        }
      }
      $r = Call "GET" "/agent/jobs?wait=20" $null 35
      foreach ($job in @($r.jobs)) {
        if (-not $job) { continue }
        Log "job $($job.id): $($job.script)"
        try {
          $out = Invoke-Allowed $job.script
          Call "POST" "/agent/jobs/$($job.id)/result" @{ ok = $true; output = [string]$out } | Out-Null
        } catch {
          Call "POST" "/agent/jobs/$($job.id)/result" @{ ok = $false; output = [string]$_.Exception.Message } | Out-Null
        }
        $lastBeat = [datetime]::MinValue          # report fresh metrics right after a change
      }
    } catch {
      Log "error: $($_.Exception.Message)"
      Start-Sleep -Seconds 10
    }
  }
}
