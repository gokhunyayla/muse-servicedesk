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
$Version = "1.5.0"
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

function Get-Software {
  # installed programs (both registry views) plus portable runtimes the desk knows how to patch
  $keys = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*", "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*"
  $list = @(Get-ItemProperty $keys -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -and $_.DisplayVersion -and -not $_.SystemComponent } |
    ForEach-Object { @{ name = [string]$_.DisplayName; version = [string]$_.DisplayVersion; publisher = [string]$_.Publisher } })
  foreach ($root in @("C:\php", "C:\tools\php")) {
    $exe = Join-Path $root "php.exe"
    if (Test-Path $exe) {
      $v = (& $exe -r "echo PHP_VERSION;" 2>$null)
      if ($v) { $list += @{ name = "PHP"; version = [string]$v; publisher = "The PHP Group"; path = $root } }
    }
  }
  return @($list | Select-Object -First 300)
}

function Get-Threats {
  # Microsoft Defender detections of the last 7 days, with what Defender did about them
  try {
    $names = @{}
    Get-MpThreat -ErrorAction Stop | ForEach-Object { $names[[string]$_.ThreatID] = $_ }
    $status = @{ 0 = "unknown"; 1 = "detected"; 2 = "cleaned"; 3 = "quarantined"; 4 = "removed"; 5 = "allowed"; 6 = "blocked"; 102 = "quarantine failed"; 103 = "remove failed"; 104 = "allow failed"; 105 = "abandoned"; 107 = "blocked failed" }
    $since = (Get-Date).AddDays(-7)
    return @(Get-MpThreatDetection -ErrorAction Stop | Where-Object { $_.InitialDetectionTime -ge $since } |
      Sort-Object InitialDetectionTime -Descending | Select-Object -First 20 | ForEach-Object {
        $t = $names[[string]$_.ThreatID]
        @{ id = [string]$_.ThreatID; name = $(if ($t) { [string]$t.ThreatName } else { "" }); severity = $(if ($t) { [int]$t.SeverityID } else { 0 })
           detected = $_.InitialDetectionTime.ToUniversalTime().ToString("s") + "Z"; action_success = [bool]$_.ActionSuccess
           status = $(if ($status.ContainsKey([int]$_.ThreatStatusID)) { $status[[int]$_.ThreatStatusID] } else { [string]$_.ThreatStatusID })
           active = [bool]($t -and $t.IsActive); resources = @($_.Resources | Select-Object -First 3 | ForEach-Object { [string]$_ }) } })
  } catch { return @() }
}

$BugChecks = @{
  "0x0000000a" = @("IRQL_NOT_LESS_OR_EQUAL", "driver"); "0x000000d1" = @("DRIVER_IRQL_NOT_LESS_OR_EQUAL", "driver")
  "0x00000050" = @("PAGE_FAULT_IN_NONPAGED_AREA", "driver or memory"); "0x0000007e" = @("SYSTEM_THREAD_EXCEPTION_NOT_HANDLED", "driver")
  "0x0000003b" = @("SYSTEM_SERVICE_EXCEPTION", "driver or system files"); "0x0000001a" = @("MEMORY_MANAGEMENT", "memory")
  "0x000000ef" = @("CRITICAL_PROCESS_DIED", "system files"); "0x00000133" = @("DPC_WATCHDOG_VIOLATION", "driver or firmware")
  "0x0000007a" = @("KERNEL_DATA_INPAGE_ERROR", "disk"); "0x00000077" = @("KERNEL_STACK_INPAGE_ERROR", "disk")
  "0x00000124" = @("WHEA_UNCORRECTABLE_ERROR", "hardware"); "0x0000009f" = @("DRIVER_POWER_STATE_FAILURE", "driver")
  "0x00000019" = @("BAD_POOL_HEADER", "driver or memory"); "0x000000c2" = @("BAD_POOL_CALLER", "driver")
  "0x000000e2" = @("MANUALLY_INITIATED_CRASH", "manual"); "0x00000139" = @("KERNEL_SECURITY_CHECK_FAILURE", "driver or memory")
}

function Get-Crashes($days = 7) {
  # blue screens Windows recorded after the restart (WER event 1001 in the System log)
  try {
    $since = (Get-Date).AddDays(-$days)
    return @(Get-WinEvent -FilterHashtable @{ LogName = "System"; Id = 1001; ProviderName = "Microsoft-Windows-WER-SystemErrorReporting"; StartTime = $since } -MaxEvents 10 -ErrorAction Stop |
      ForEach-Object {
        $m = [string]$_.Message
        $code = if ($m -match "0x[0-9a-fA-F]{8}") { $Matches[0].ToLower() } else { "" }
        $info = if ($BugChecks.ContainsKey($code)) { $BugChecks[$code] } else { @("", "unknown") }
        @{ id = [string]$_.RecordId; at = $_.TimeCreated.ToUniversalTime().ToString("s") + "Z"; code = $code; name = $info[0]; hint = $info[1]
           dump = $(if ($m -match "([A-Za-z]:\\\S+?\.dmp)") { $Matches[1] } else { "" }) } })
  } catch { return @() }
}

function Get-DiskHealth {
  try { return @(Get-PhysicalDisk -ErrorAction Stop | ForEach-Object { @{ name = [string]$_.FriendlyName; health = [string]$_.HealthStatus; status = [string]($_.OperationalStatus -join ",") } }) }
  catch { return @() }
}

function Get-Defender {
  try {
    $s = Get-MpComputerStatus -ErrorAction Stop
    return @{ realtime = [bool]$s.RealTimeProtectionEnabled; signature_age_days = [int]$s.AntivirusSignatureAge }
  } catch { return @{ realtime = $false; signature_age_days = -1 } }
}

function Get-Metrics {
  $os = Get-CimInstance Win32_OperatingSystem
  $disk = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='C:'"
  $cpu = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
  $dnsOk = $true
  try { Resolve-DnsName -Name "www.microsoft.com" -QuickTimeout -ErrorAction Stop | Out-Null } catch { $dnsOk = $false }
  $crit = 0
  try { $crit = @(Get-WinEvent -FilterHashtable @{ LogName = 'System'; Level = 1, 2; StartTime = (Get-Date).AddDays(-1) } -ErrorAction Stop).Count } catch {}
  $run = Join-Path $Home_ "updates-result.txt"
  $updatesRun = if (Test-Path $run) { (Get-Content $run -Raw).Trim() } else { "" }
  if ($updatesRun.Length -gt 1500) { $updatesRun = $updatesRun.Substring(0, 1500) }
  $software = Get-Software
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
    updates_run        = $updatesRun
    software           = $software
    threats            = @(Get-Threats)
    defender           = Get-Defender
    crashes            = @(Get-Crashes)
    disk_health        = @(Get-DiskHealth)
    repair_run         = $(if (Test-Path (Join-Path $Home_ "repair-result.txt")) { (Get-Content (Join-Path $Home_ "repair-result.txt") -Raw).Trim() } else { "" })
  }
}

# ---- the allow-listed scripts ---------------------------------------------------------------------

function Invoke-Allowed($script, $jobArgs) {
  switch ($script) {
    "app-update" {
      # only apps this agent knows how to patch, only from the vendor's own release feed, checksum verified
      if ($jobArgs.app -ne "php") { throw "no updater for '$($jobArgs.app)' on this device" }
      $root = if ($jobArgs.path) { [string]$jobArgs.path } else { "C:\php" }
      if ($root -notin @("C:\php", "C:\tools\php")) { throw "PHP path not allowed: $root" }
      $exe = Join-Path $root "php.exe"
      if (-not (Test-Path $exe)) { throw "PHP not found at $root" }
      $before = (& $exe -r "echo PHP_VERSION;")
      $branch = ($before -split "\.")[0..1] -join "."
      $feed = Invoke-RestMethod -UseBasicParsing "https://windows.php.net/downloads/releases/releases.json" -TimeoutSec 60
      $rel = $feed.$branch
      if (-not $rel) { throw "no release line $branch in the PHP for Windows feed" }
      $flavor = if (Test-Path (Join-Path $root "php8ts.dll")) { "ts" } else { "nts" }
      $build = $rel.PSObject.Properties | Where-Object { $_.Name -like "$flavor-vs*-x64" } | Select-Object -First 1
      if (-not $build) { throw "no $flavor x64 build for PHP $branch" }
      $target = ([string]$rel.version -split "-")[0]          # the feed marks rebuilds as 8.3.36-1; PHP itself says 8.3.36
      if ($jobArgs.min_version -and ([version]$target -lt [version]$jobArgs.min_version)) { throw "the newest PHP $branch ($target) is still below $($jobArgs.min_version)" }
      if ([version]$target -le [version]$before) { return "PHP $before is already the newest $branch release ($target); nothing to do." }
      $zip = $build.Value.zip
      $tmp = Join-Path $env:TEMP "php-$target.zip"
      Invoke-WebRequest -UseBasicParsing "https://windows.php.net/downloads/releases/$($zip.path)" -OutFile $tmp -TimeoutSec 300
      $sha = (Get-FileHash $tmp -Algorithm SHA256).Hash.ToLower()
      if ($sha -ne ([string]$zip.sha256).ToLower()) { Remove-Item $tmp -Force; throw "checksum mismatch for $($zip.path); nothing changed" }
      $backups = Join-Path $Home_ "backups"; New-Item -ItemType Directory -Force -Path $backups | Out-Null
      $backupId = "php-$before-" + (Get-Date).ToUniversalTime().ToString("yyyyMMddHHmmss")
      Copy-Item $root (Join-Path $backups $backupId) -Recurse -Force
      $stage = Join-Path $env:TEMP "php-$target"; Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
      Expand-Archive $tmp -DestinationPath $stage -Force
      if (Test-Path (Join-Path $root "php.ini")) { Copy-Item (Join-Path $root "php.ini") $stage -Force }   # keep the configuration
      Get-ChildItem $root | Remove-Item -Recurse -Force
      Copy-Item (Join-Path $stage "*") $root -Recurse -Force
      $after = (& $exe -r "echo PHP_VERSION;")
      if ($after -ne $target) { throw "PHP reports $after after the update, expected $target; restore with app-rollback $backupId" }
      return "PHP updated $before -> $after from windows.php.net ($($zip.path), sha256 verified). Backup: $backupId"
    }
    "app-rollback" {
      if ($jobArgs.app -ne "php") { throw "no rollback for '$($jobArgs.app)' on this device" }
      $root = if ($jobArgs.path) { [string]$jobArgs.path } else { "C:\php" }
      if ($root -notin @("C:\php", "C:\tools\php")) { throw "PHP path not allowed: $root" }
      $backups = Join-Path $Home_ "backups"
      $pick = if ($jobArgs.backup -and $jobArgs.backup -ne "latest") { Get-Item (Join-Path $backups ([IO.Path]::GetFileName([string]$jobArgs.backup))) -ErrorAction SilentlyContinue }
              else { Get-ChildItem $backups -Directory -Filter "php-*" -ErrorAction SilentlyContinue | Sort-Object Name -Descending | Select-Object -First 1 }
      if (-not $pick) { throw "no PHP backup to restore" }
      $now = (& (Join-Path $root "php.exe") -r "echo PHP_VERSION;" 2>$null)
      Get-ChildItem $root | Remove-Item -Recurse -Force
      Copy-Item (Join-Path $pick.FullName "*") $root -Recurse -Force
      $back = (& (Join-Path $root "php.exe") -r "echo PHP_VERSION;")
      return "PHP rolled back $now -> $back from backup $($pick.Name)"
    }
    "malware-clean" {
      # Defender only: remove what it detected, refresh signatures, quick scan, then report what is left
      $found = @(Get-MpThreat -ErrorAction Stop)
      $active = @($found | Where-Object { $_.IsActive })
      Remove-MpThreat -ErrorAction SilentlyContinue
      try { Update-MpSignature -ErrorAction Stop } catch {}
      Start-MpScan -ScanType QuickScan -ErrorAction Stop
      $left = @(Get-MpThreat -ErrorAction SilentlyContinue | Where-Object { $_.IsActive })
      $names = (@($found | ForEach-Object { $_.ThreatName }) | Select-Object -Unique) -join ", "
      $verdict = if ($left.Count -eq 0) { "Result: clean" } else { "Result: threats remain: " + (($left | ForEach-Object { $_.ThreatName }) -join ", ") }
      return "Defender threats on record: $(if ($names) { $names } else { 'none' }); active before: $($active.Count); removed and quick scan finished. $verdict"
    }
    "defender-test" {
      # drill: the industry-standard EICAR test file (harmless text every antivirus must catch); built from parts so
      # this script itself is not flagged
      $p1 = 'X5O!P%@AP[4\PZX54(P^)7CC)7}$'; $p2 = 'EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*'
      $file = Join-Path $env:PUBLIC "muse-eicar-test.com"
      try { [IO.File]::WriteAllText($file, $p1 + $p2) } catch { return "Defender blocked the EICAR test file as it was written: protection works. ($($_.Exception.Message))" }
      Start-Sleep -Seconds 5
      if (Test-Path $file) { try { Start-MpScan -ScanType CustomScan -ScanPath $file -ErrorAction Stop } catch {} }   # scan it now rather than wait
      $still = Test-Path $file
      return "EICAR test file written to $file and scanned; it is $(if ($still) { 'still there: Defender did not catch it' } else { 'gone: Defender caught it' })."
    }
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
      return "Started installing $($todo.Count) update(s) in the background: $($titles -join '; '). This usually takes 10-30 minutes; the desk is told automatically when it finishes (result and whether a restart is needed)."
    }
    "crash-report" {
      # what an engineer looks at first: the stop codes, drivers that changed lately, disk health
      $crashes = @(Get-Crashes 30)
      $lines = @()
      if ($crashes.Count -eq 0) { $lines += "No blue screen recorded in the last 30 days." }
      foreach ($c in ($crashes | Select-Object -First 5)) { $lines += "Blue screen $($c.at): $($c.code) $($c.name) (likely cause: $($c.hint))$(if ($c.dump) { '; dump ' + $c.dump })" }
      $since = (Get-Date).AddDays(-14)
      $fresh = @(Get-ChildItem "$env:SystemRoot\System32\drivers" -Filter *.sys -ErrorAction SilentlyContinue | Where-Object { $_.CreationTime -ge $since -or $_.LastWriteTime -ge $since } |
        Sort-Object LastWriteTime -Descending | Select-Object -First 8 | ForEach-Object {
          $sig = Get-AuthenticodeSignature $_.FullName -ErrorAction SilentlyContinue
          $who = if ($sig -and $sig.SignerCertificate) { ($sig.SignerCertificate.Subject -replace '^CN=([^,]+).*', '$1') } else { "unsigned" }
          "$($_.Name) ($who, changed $($_.LastWriteTime.ToString('yyyy-MM-dd HH:mm')))" })
      $lines += "Drivers added or changed in the last 14 days: $(if ($fresh) { $fresh -join '; ' } else { 'none' })"
      $disks = @(Get-DiskHealth)
      $lines += "Disk health: $(if ($disks) { ($disks | ForEach-Object { "$($_.name) $($_.health)" }) -join '; ' } else { 'unknown' })"
      $bad = @($disks | Where-Object { $_.health -and $_.health -ne "Healthy" })
      $hw = @($crashes | Where-Object { $_.hint -in @("disk", "hardware", "memory") })
      $verdict = if ($bad -or $hw) { "Verdict: hardware suspected ($(@($bad | ForEach-Object { $_.name + ' ' + $_.health }) + @($hw | ForEach-Object { $_.name }) -join ', '))" }
                 elseif ($crashes) { "Verdict: software (driver or system files); repair is worth trying" } else { "Verdict: no crash to act on" }
      return ($lines + $verdict) -join "`n"
    }
    "system-repair" {
      # DISM then SFC take 10-40 minutes: a one-off SYSTEM task, the outcome reaches the desk with the heartbeat
      $worker = Join-Path $Home_ "system-repair.ps1"
      @'
$out = Join-Path $env:ProgramData "MuseAgent\repair-result.txt"
"started $((Get-Date).ToUniversalTime().ToString('u'))" | Set-Content $out
try {
  $dism = (& dism.exe /Online /Cleanup-Image /RestoreHealth 2>&1 | Out-String)
  $sfc = (& sfc.exe /scannow 2>&1 | Out-String) -replace "`0", ""
  $d = if ($dism -match "operation completed successfully") { "component store repaired or healthy" } else { "DISM did not finish cleanly" }
  $s = if ($sfc -match "did not find any integrity violations") { "no corrupt system files" }
       elseif ($sfc -match "successfully repaired") { "corrupt system files found and repaired" }
       elseif ($sfc -match "unable to fix") { "corrupt system files found that could not be repaired" } else { "SFC result unclear" }
  "finished $((Get-Date).ToUniversalTime().ToString('u')); DISM: $d; SFC: $s" | Set-Content $out
} catch { "failed $((Get-Date).ToUniversalTime().ToString('u')): $($_.Exception.Message)" | Set-Content $out }
'@ | Set-Content -Path $worker -Encoding UTF8
      $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$worker`""
      $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
      Unregister-ScheduledTask -TaskName "MuseAgentSystemRepair" -Confirm:$false -ErrorAction SilentlyContinue
      Register-ScheduledTask -TaskName "MuseAgentSystemRepair" -Action $action -Principal $principal | Out-Null
      Start-ScheduledTask -TaskName "MuseAgentSystemRepair"
      return "Started Windows system repair (DISM RestoreHealth, then SFC) in the background. It usually takes 10-40 minutes; the desk is told automatically when it finishes."
    }
    "crash-drill" {
      # admin drill: Microsoft Sysinternals NotMyFault makes a controlled blue screen; the machine restarts by itself
      $dir = Join-Path $Home_ "notmyfault"; New-Item -ItemType Directory -Force -Path $dir | Out-Null
      $zip = Join-Path $dir "NotMyFault.zip"
      Invoke-WebRequest -UseBasicParsing "https://download.sysinternals.com/files/NotMyFault.zip" -OutFile $zip -TimeoutSec 120
      Expand-Archive $zip -DestinationPath $dir -Force
      $exe = Join-Path $dir "notmyfaultc64.exe"
      if (-not (Test-Path $exe)) { $exe = Join-Path $dir "notmyfault64.exe" }
      $sig = Get-AuthenticodeSignature $exe
      if ($sig.Status -ne "Valid" -or $sig.SignerCertificate.Subject -notmatch "Microsoft Corporation") { throw "NotMyFault signature is not a valid Microsoft signature; nothing run" }
      $arg = if ($exe -like "*notmyfaultc64.exe") { "-accepteula crash 0x01" } else { "-accepteula /crash" }
      $action = New-ScheduledTaskAction -Execute $exe -Argument $arg
      $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
      $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddSeconds(20)
      Unregister-ScheduledTask -TaskName "MuseAgentCrashDrill" -Confirm:$false -ErrorAction SilentlyContinue
      Register-ScheduledTask -TaskName "MuseAgentCrashDrill" -Action $action -Principal $principal -Trigger $trigger | Out-Null
      return "Crash drill armed: Microsoft-signed NotMyFault ($([IO.Path]::GetFileName($exe))) will cause a blue screen in about 20 seconds; Windows restarts by itself."
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
          $out = Invoke-Allowed $job.script $job.args
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
