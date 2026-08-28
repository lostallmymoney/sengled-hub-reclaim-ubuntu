#requires -Version 5.1
<#
.SYNOPSIS
    One-stop Sengled Element Hub reclaim controller (Windows).

.DESCRIPTION
    Opens the stock TCP/8686 service, enables telnet, converts the onboard
    EM357 to the proven EmberZNet 6.4.1 / EZSP v7 NCP when required, dumps the
    complete RTL8196E flash, builds a per-device reclaimed Bank2 image from the
    hub's OWN firmware, writes only Bank2, verifies byte-for-byte, reboots, and
    performs a health check.

    Bank1 is never written by this tool.
#>

[CmdletBinding()]
param(
    [string]$Hub,
    [ValidateRange(1024,65535)][int]$TftpPort = 6969,
    [switch]$SkipCoordinator,
    [switch]$ForceCoordinator,
    [switch]$NoReboot,
    [switch]$DryRun,
    [switch]$BackupOnly,
    [switch]$BootTestOnly,
    [switch]$KeepWork
)

$ErrorActionPreference = 'Stop'
$Version = '0.2-rc4'
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Payload = Join-Path $ScriptRoot 'payload'
$Lib = Join-Path $ScriptRoot 'lib'
$Cache = Join-Path $ScriptRoot 'cache'
New-Item -ItemType Directory -Force -Path $Cache | Out-Null

function Write-Step([string]$Text) {
    Write-Host ''
    Write-Host ('=== {0} ===' -f $Text) -ForegroundColor Cyan
}
function Write-Ok([string]$Text) { Write-Host ('[OK] {0}' -f $Text) -ForegroundColor Green }
function Write-Warn([string]$Text) { Write-Host ('[!] {0}' -f $Text) -ForegroundColor Yellow }

function Test-Administrator {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p = New-Object -TypeName Security.Principal.WindowsPrincipal -ArgumentList $id
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Test-TcpPort([string]$ComputerName, [int]$Port, [int]$TimeoutMs = 700) {
    $client = New-Object Net.Sockets.TcpClient
    try {
        $iar = $client.BeginConnect($ComputerName, $Port, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne($TimeoutMs, $false)) { return $false }
        $client.EndConnect($iar)
        return $client.Connected
    } catch { return $false }
    finally { $client.Dispose() }
}

function Wait-TcpPort([string]$ComputerName, [int]$Port, [int]$TimeoutSeconds, [string]$Label) {
    $until = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $until) {
        if (Test-TcpPort $ComputerName $Port 700) { Write-Ok "$Label is online at ${ComputerName}:$Port"; return }
        Start-Sleep -Seconds 2
    }
    throw "Timed out waiting for $Label at ${ComputerName}:$Port"
}

function Resolve-HubAddress {
    param([string]$Requested)

    # Advanced/scripted use can still supply -Hub. Normal launcher use prompts.
    if ($Requested) {
        $entered = $Requested.Trim()
        $parsed = $null
        if (-not [Net.IPAddress]::TryParse($entered, [ref]$parsed) -or
            $parsed.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) {
            throw "Invalid hub IPv4 address: $entered"
        }

        $address = $parsed.ToString()
        if ($BootTestOnly) { return $address }
        Write-Host "Checking hub at $address..." -ForegroundColor DarkGray
        if (-not (Test-TcpPort $address 8686 1200) -and -not (Test-TcpPort $address 23 1200)) {
            throw "Hub $address is not reachable on TCP/8686 or TCP/23"
        }
        return $address
    }

    Write-Host ''
    Write-Host 'Hub connection' -ForegroundColor Cyan
    while ($true) {
        $entered = Read-Host 'Enter Sengled hub IPv4 address (blank cancels)'
        if (-not $entered) { throw 'Cancelled: no hub IP supplied' }
        $entered = $entered.Trim()

        $parsed = $null
        if (-not [Net.IPAddress]::TryParse($entered, [ref]$parsed) -or
            $parsed.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) {
            Write-Warn "Invalid IPv4 address: $entered"
            continue
        }

        $address = $parsed.ToString()
        if ($BootTestOnly) { return $address }
        Write-Host "Checking hub at $address..." -ForegroundColor DarkGray
        if ((Test-TcpPort $address 8686 1200) -or (Test-TcpPort $address 23 1200)) {
            return $address
        }

        Write-Warn "Hub $address is not reachable on TCP/8686 or TCP/23. Check the address/power and try again."
    }
}

function Invoke-SengledAT([string]$ComputerName, [string[]]$Commands) {
    $client = New-Object Net.Sockets.TcpClient
    $stream = $null
    try {
        $client.ReceiveTimeout = 500
        $client.SendTimeout = 3000
        $client.Connect($ComputerName, 8686)
        $stream = $client.GetStream()
        foreach ($command in $Commands) {
            Write-Host "AT backdoor: $command" -ForegroundColor DarkGray
            $bytes = [Text.Encoding]::ASCII.GetBytes($command + "`r`n")
            $stream.Write($bytes,0,$bytes.Length); $stream.Flush()
            $reply = New-Object Text.StringBuilder
            $deadline = [DateTime]::UtcNow.AddSeconds(4)
            $buffer = New-Object byte[] 4096
            while ([DateTime]::UtcNow -lt $deadline) {
                while ($stream.DataAvailable) {
                    $n = $stream.Read($buffer,0,$buffer.Length)
                    if ($n -le 0) { break }
                    [void]$reply.Append([Text.Encoding]::ASCII.GetString($buffer,0,$n))
                }
                $text = $reply.ToString()
                if ($text -match ':OK' -or $text -match ':FAIL') { break }
                Start-Sleep -Milliseconds 80
            }
            $text = $reply.ToString().Trim()
            if ($text -match ':FAIL') { throw "Hub rejected $command : $text" }
            if ($text -notmatch ':OK') { throw "Unexpected/no response to $command : $text" }
        }
    } finally {
        if ($stream) { $stream.Dispose() }
        $client.Dispose()
    }
}

function Get-LocalIPv4ForRemote([string]$Remote) {
    $u = New-Object Net.Sockets.UdpClient
    try {
        $u.Connect($Remote, 9)
        return ([Net.IPEndPoint]$u.Client.LocalEndPoint).Address.ToString()
    } finally { $u.Dispose() }
}

function Invoke-HubCommand {
    param(
        [Parameter(Mandatory)][SengledReclaim.TelnetShell]$Shell,
        [Parameter(Mandatory)][string]$Command,
        [int]$TimeoutSeconds = 30,
        [switch]$AllowFailure,
        [switch]$Quiet
    )
    $r = $Shell.Run($Command, $TimeoutSeconds * 1000)
    if (-not $Quiet -and $r.Output) { Write-Host $r.Output -ForegroundColor DarkGray }
    if (-not $AllowFailure -and $r.ExitCode -ne 0) {
        throw "Hub command failed (rc=$($r.ExitCode)): $Command`n$($r.Output)"
    }
    return $r
}

function Connect-HubShell {
    param(
        [Parameter(Mandatory)][string]$ComputerName,
        [ValidateRange(1,60)][int]$Attempts = 1,
        [ValidateRange(0,10)][int]$DelaySeconds = 2,
        [switch]$ReturnNull,
        [switch]$SuppressWarnings
    )

    $lastError = $null

    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        $candidate = $null
        try {
            if ($Attempts -gt 1 -and $attempt -gt 1) {
                Write-Host "Retrying telnet shell connection (attempt $attempt of $Attempts)..." -ForegroundColor DarkGray
            }

            # Important: use the REAL TelnetShell connection as the readiness check.
            # Do not probe TCP/23 with a throwaway TcpClient first; this hub's telnetd
            # can be sensitive to rapid connect/disconnect cycles.
            $candidate = New-Object -TypeName SengledReclaim.TelnetShell -ArgumentList @($ComputerName,23)
            $candidate.Connect(5000)

            # Give BusyBox a moment to attach the shell, then synchronize it.
            Start-Sleep -Milliseconds 750
            $candidate.SendRawLine('')
            Start-Sleep -Milliseconds 350

            # An open TCP/23 listener is not enough. Require a command to complete.
            $probe = $candidate.Run('echo __SENGLED_SHELL_READY__', 7000)
            if ($probe.ExitCode -ne 0 -or $probe.Output -notmatch '__SENGLED_SHELL_READY__') {
                throw "Telnet readiness probe failed (rc=$($probe.ExitCode)): $($probe.Output)"
            }

            Write-Ok "telnet command shell is responsive at ${ComputerName}:23"
            return $candidate
        }
        catch {
            $lastError = $_.Exception
            if ($candidate) {
                try { $candidate.Dispose() } catch { }
            }

            if (-not $SuppressWarnings) {
                Write-Warn "Telnet shell connection attempt $attempt failed: $($lastError.Message)"
            }

            if ($attempt -lt $Attempts -and $DelaySeconds -gt 0) {
                Start-Sleep -Seconds $DelaySeconds
            }
        }
    }

    if ($ReturnNull) { return $null }

    $detail = if ($lastError) { $lastError.Message } else { 'unknown error' }
    throw "Hub shell at ${ComputerName}:23 did not become command-responsive after $Attempts attempt(s). Last error: $detail"
}

function Copy-ToTftpRoot([string]$Source, [string]$TftpRoot, [string]$Name) {
    $dest = Join-Path $TftpRoot $Name
    Copy-Item -Force -LiteralPath $Source -Destination $dest
    return $dest
}

function Send-FileToHub {
    param($Shell,[string]$LocalPath,[string]$HubPath,[string]$RemoteName,[string]$TftpRoot,[string]$PcIp,[int]$Port)
    Copy-ToTftpRoot $LocalPath $TftpRoot $RemoteName | Out-Null
    $cmd = "tftp -g -r $RemoteName -l $HubPath $PcIp $Port"
    Invoke-HubCommand $Shell $cmd 180 -Quiet | Out-Null
    $expected = (Get-Item -LiteralPath $LocalPath).Length
    $r = Invoke-HubCommand $Shell "wc -c $HubPath" 20 -Quiet
    if ($r.Output -notmatch [Regex]::Escape([string]$expected)) { throw "Hub file size check failed for $HubPath (expected $expected): $($r.Output)" }
    Write-Ok "Transferred $RemoteName ($expected bytes)"
}

function Receive-FileFromHub {
    param($Shell,[string]$HubPath,[string]$RemoteName,[string]$TftpRoot,[string]$Destination,[long]$Expected,[string]$PcIp,[int]$Port)
    $serverFile = Join-Path $TftpRoot $RemoteName
    Remove-Item -Force -ErrorAction SilentlyContinue $serverFile,($serverFile+'.part')
    $cmd = "tftp -p -l $HubPath -r $RemoteName $PcIp $Port"
    Invoke-HubCommand $Shell $cmd 300 -Quiet | Out-Null
    # The hub exits after receiving the final ACK. The server sends that ACK
    # immediately before it flushes and atomically renames .part, so allow the
    # local worker a brief, bounded interval to commit the completed upload.
    $commitDeadline = [DateTime]::UtcNow.AddSeconds(5)
    while (-not (Test-Path -LiteralPath $serverFile) -and [DateTime]::UtcNow -lt $commitDeadline) {
        Start-Sleep -Milliseconds 20
    }
    if (-not (Test-Path -LiteralPath $serverFile)) {
        $partState = if (Test-Path -LiteralPath ($serverFile+'.part')) { ' (partial file remains)' } else { '' }
        throw "TFTP upload did not commit $RemoteName within 5 seconds$partState"
    }
    $len = (Get-Item -LiteralPath $serverFile).Length
    if ($len -ne $Expected) { throw "Backup $RemoteName has $len bytes; expected $Expected" }
    Copy-Item -Force -LiteralPath $serverFile -Destination $Destination
    Write-Ok "Backed up $RemoteName ($len bytes)"
}

function Install-HubExecutable {
    param($Shell,[string]$LocalPath,[string]$Name,[string]$TftpRoot,[string]$PcIp,[int]$Port)
    $dat = "/tmp/$Name.dat"
    $exe = "/tmp/$Name"
    Send-FileToHub $Shell $LocalPath $dat $Name $TftpRoot $PcIp $Port
    $r = Invoke-HubCommand $Shell "cp /bin/busybox $exe && cat $dat > $exe && rm -f $dat && ls -l $exe" 30 -Quiet
    if ($r.Output -notmatch 'rwx') { throw "Executable-carrier bootstrap failed for $Name : $($r.Output)" }
    Write-Ok "Installed executable /tmp/$Name"
    return $exe
}

function Get-GitBlobSha1([string]$Path) {
    [byte[]]$data = [IO.File]::ReadAllBytes($Path)
    [byte[]]$header = [Text.Encoding]::ASCII.GetBytes("blob $($data.Length)`0")
    $ms = New-Object IO.MemoryStream
    try {
        $ms.Write($header,0,$header.Length); $ms.Write($data,0,$data.Length); $ms.Position=0
        $sha = [Security.Cryptography.SHA1]::Create()
        try { $h=$sha.ComputeHash($ms) } finally { $sha.Dispose() }
    } finally { $ms.Dispose() }
    return (-join ($h | ForEach-Object { $_.ToString('x2') }))
}

function Get-CoordinatorFirmware([string]$Destination) {
    $url = 'https://raw.githubusercontent.com/walthowd/husbzb-firmware/master/em357-v641-ncp-uart-sw.ebl'
    $expectedLength = 146816
    $expectedGitSha = '361738c5116a97e7d755df46d6bcc31e167038fd'
    Write-Host 'Downloading public EM357 EmberZNet 6.4.1 / EZSP v7 firmware...' -ForegroundColor DarkGray
    Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $Destination
    $len = (Get-Item -LiteralPath $Destination).Length
    if ($len -ne $expectedLength) { throw "Coordinator firmware length $len != $expectedLength" }
    $git = Get-GitBlobSha1 $Destination
    if ($git -ne $expectedGitSha) { throw "Coordinator firmware Git blob SHA-1 mismatch: $git" }
    Write-Ok "Coordinator firmware verified: $len bytes, Git blob $git"
}

function Start-SquashProcess {
    param([string]$Exe,[string[]]$ProcessArguments,[string]$StdIn,[string]$StdOut,[string]$StdErr)
    if (-not $ProcessArguments -or $ProcessArguments.Count -eq 0) {
        throw "No arguments supplied for $([IO.Path]::GetFileName($Exe))"
    }
    $argLine = ($ProcessArguments | ForEach-Object {
        if ($_ -match '[\s"]') { '"' + ($_ -replace '"','\"') + '"' } else { $_ }
    }) -join ' '
    $sp = @{ FilePath=$Exe; ArgumentList=$argLine; Wait=$true; PassThru=$true; NoNewWindow=$true }
    if ($StdIn) { $sp.RedirectStandardInput = $StdIn }
    if ($StdOut) { $sp.RedirectStandardOutput = $StdOut }
    if ($StdErr) { $sp.RedirectStandardError = $StdErr }
    $p = Start-Process @sp
    if ($p.ExitCode -ne 0) {
        $err = if ($StdErr -and (Test-Path $StdErr)) { Get-Content -Raw $StdErr } else { '' }
        throw "$([IO.Path]::GetFileName($Exe)) failed rc=$($p.ExitCode)`n$err"
    }
}

function Ensure-SquashTools {
    $toolDir = Join-Path $Cache 'squashfs-tools-ng-1.3.2-mingw64'
    $sqfs2tar = Get-ChildItem -Path $toolDir -Recurse -Filter sqfs2tar.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    $tar2sqfs = Get-ChildItem -Path $toolDir -Recurse -Filter tar2sqfs.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $sqfs2tar -or -not $tar2sqfs) {
        Write-Step 'Downloading Windows SquashFS tools'
        $zip = Join-Path $Cache 'squashfs-tools-ng-1.3.2-mingw64.zip'
        $url = 'https://infraroot.at/pub/squashfs/windows/squashfs-tools-ng-1.3.2-mingw64.zip'
        Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $zip
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $toolDir
        New-Item -ItemType Directory -Force -Path $toolDir | Out-Null
        Expand-Archive -Force -LiteralPath $zip -DestinationPath $toolDir
        $sqfs2tar = Get-ChildItem -Path $toolDir -Recurse -Filter sqfs2tar.exe | Select-Object -First 1
        $tar2sqfs = Get-ChildItem -Path $toolDir -Recurse -Filter tar2sqfs.exe | Select-Object -First 1
    }
    if (-not $sqfs2tar -or -not $tar2sqfs) { throw 'Could not locate sqfs2tar.exe/tar2sqfs.exe after tool extraction' }
    Write-Ok 'SquashFS Windows tools ready'
    return [pscustomobject]@{ Sqfs2Tar=$sqfs2tar.FullName; Tar2Sqfs=$tar2sqfs.FullName }
}

function Build-ReclaimedImages {
    param([string]$BackupDir,[string]$BuildDir)
    Write-Step 'Preparing per-device reclaimed Bank2 images'
    $tools = Ensure-SquashTools
    New-Item -ItemType Directory -Force -Path $BuildDir | Out-Null
    $stockTar = Join-Path $BuildDir 'rootfs-stock.tar'
    $patchedTar = Join-Path $BuildDir 'rootfs-reclaimed.tar'
    $rawSqfs = Join-Path $BuildDir 'rootfs-reclaimed.raw.sqfs'
    $sqErr = Join-Path $BuildDir 'sqfs2tar.log'
    $mkOut = Join-Path $BuildDir 'tar2sqfs-output.log'
    $mkErr = Join-Path $BuildDir 'tar2sqfs-error.log'
    $bank1Root = Join-Path $BackupDir 'mtd1-bank1-rootfs.bin'
    $bank1Kernel = Join-Path $BackupDir 'mtd0-bank1-kernel.bin'
    $bank2Kernel = Join-Path $BackupDir 'mtd2-bank2-kernel.bin'
    $outRoot = Join-Path $BuildDir 'mtd3-bank2-rootfs-reclaimed.bin'
    $outKernel = Join-Path $BuildDir 'mtd2-bank2-kernel-reclaimed.bin'

    Write-Host '[build] SquashFS -> metadata-preserving tar (no Windows extraction)' -ForegroundColor DarkGray
    Start-SquashProcess $tools.Sqfs2Tar @('-r','.','-X',$bank1Root) $null $stockTar $sqErr
    if ((Get-Item $stockTar).Length -lt 1024) { throw 'sqfs2tar produced an unexpectedly small archive' }

    $buildTime = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    $buildText = "Sengled reclaimed rootfs v2`nBuilt: $buildTime`nGateway: EZSP TCP/6638 -> /dev/ttyS1 @ 57600`n"
    $statusText = @'
#!/bin/sh
echo "=== Sengled reclaimed hub ==="
cat /etc/reclaim-build.txt 2>/dev/null
echo "--- boot bank ---"
cat /proc/bootbank 2>/dev/null
echo "--- cmdline ---"
cat /proc/cmdline
echo "--- gateway process ---"
ps | grep '[e]zsp_gateway'
echo "--- Sengled processes (should be none) ---"
ps | grep '[s]engled'
echo "--- listeners ---"
if grep ':19EE ' /proc/net/tcp 2>/dev/null; then
    echo "TCP/6638 listener: present"
else
    echo "TCP/6638 listener: absent"
fi
'@
    [SengledReclaim.TarPatcher]::Patch(
        $stockTar,
        $patchedTar,
        (Join-Path $Payload 'ezsp_gateway-v3'),
        (Join-Path $Payload 'hub-chmodx-v1'),
        (Join-Path $Payload 'ezsp_start.sh'),
        $buildText,
        $statusText
    )
    Write-Ok 'Patched rootfs archive without extracting device nodes onto Windows'

    Write-Host '[build] tar -> SquashFS 4.0 / LZMA / 128K' -ForegroundColor DarkGray
    Write-Host '[WAIT] Rebuilding SquashFS can take several minutes; detailed output is being saved in the build folder.' -ForegroundColor Yellow
    Start-SquashProcess $tools.Tar2Sqfs @('-c','lzma','-b','131072','-e','-x','-f',$rawSqfs) $patchedTar $mkOut $mkErr
    if (-not (Test-Path $rawSqfs)) { throw 'tar2sqfs did not create rootfs image' }

    Write-Host ([SengledReclaim.ImageBuilder]::WrapRootfs($rawSqfs,$outRoot)) -ForegroundColor DarkGray
    Write-Host ([SengledReclaim.ImageBuilder]::BuildKernel($bank1Kernel,$bank2Kernel,$outKernel)) -ForegroundColor DarkGray
    Write-Host ([SengledReclaim.ImageBuilder]::Verify($outKernel,$outRoot)) -ForegroundColor Green

    $hashFile = Join-Path $BuildDir 'RECLAIM-SHA256.txt'
    $lines = @(
        ((Get-FileHash -Algorithm SHA256 $outKernel).Hash.ToLowerInvariant() + '  mtd2-bank2-kernel-reclaimed.bin'),
        ((Get-FileHash -Algorithm SHA256 $outRoot).Hash.ToLowerInvariant() + '  mtd3-bank2-rootfs-reclaimed.bin')
    )
    $lines | Set-Content -Encoding ASCII $hashFile
    Write-Ok 'Reclaimed images built and verified'
    return [pscustomobject]@{ Kernel=$outKernel; Rootfs=$outRoot; Hashes=$hashFile }
}

function Save-HubMetadata($Shell,[string]$Dir) {
    $items = @{
        'proc-mtd.txt'='cat /proc/mtd';
        'cmdline.txt'='cat /proc/cmdline';
        'bootbank.txt'='cat /proc/bootbank';
        'dualbank.txt'='flash get DUALBANK_ENABLED';
        'mounts.txt'='mount';
        'flash-all.txt'='flash all'
    }
    foreach ($name in $items.Keys) {
        $r = Invoke-HubCommand $Shell $items[$name] 45 -AllowFailure -Quiet
        $r.Output | Set-Content -Encoding ASCII (Join-Path $Dir $name)
    }
}

function Backup-RtlFlash($Shell,[string]$BackupDir,[string]$TftpRoot,[string]$PcIp,[int]$Port) {
    Write-Step 'Dumping complete RTL8196E flash'
    New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
    Save-HubMetadata $Shell $BackupDir
    $parts = @(
        @{ Hub='/dev/mtdblock0'; Name='mtd0-bank1-kernel.bin'; Size=0x130000 },
        @{ Hub='/dev/mtdblock1'; Name='mtd1-bank1-rootfs.bin'; Size=0x2D0000 },
        @{ Hub='/dev/mtdblock2'; Name='mtd2-bank2-kernel.bin'; Size=0x130000 },
        @{ Hub='/dev/mtdblock3'; Name='mtd3-bank2-rootfs.bin'; Size=0x2D0000 }
    )
    foreach ($p in $parts) {
        Receive-FileFromHub $Shell $p.Hub $p.Name $TftpRoot (Join-Path $BackupDir $p.Name) $p.Size $PcIp $Port
    }
    $full = Join-Path $BackupDir 'fullflash-8mb.bin'
    $out = [IO.File]::Open($full,[IO.FileMode]::Create,[IO.FileAccess]::Write)
    try {
        foreach ($p in $parts) {
            $src = [IO.File]::OpenRead((Join-Path $BackupDir $p.Name))
            try { $src.CopyTo($out) } finally { $src.Dispose() }
        }
    } finally { $out.Dispose() }
    if ((Get-Item $full).Length -ne 0x800000) { throw 'Assembled fullflash backup is not exactly 8 MiB' }
    $hashes = Get-ChildItem $BackupDir -Filter '*.bin' | ForEach-Object {
        (Get-FileHash -Algorithm SHA256 $_.FullName).Hash.ToLowerInvariant() + '  ' + $_.Name
    }
    $hashes | Set-Content -Encoding ASCII (Join-Path $BackupDir 'BACKUP-SHA256.txt')
    Write-Ok "Complete flash backup saved to $BackupDir"
}

function Validate-StockHub($Shell) {
    Write-Step 'Validating supported hub layout'
    $boot = Invoke-HubCommand $Shell 'cat /proc/bootbank' 15 -Quiet
    if ($boot.Output -notmatch '(?m)^1\s*$') { throw "Public installer requires Bank1 active. /proc/bootbank output:`n$($boot.Output)" }
    $dual = Invoke-HubCommand $Shell 'flash get DUALBANK_ENABLED' 15 -Quiet
    if ($dual.Output -notmatch 'DUALBANK_ENABLED=1') { throw "DUALBANK_ENABLED is not 1: $($dual.Output)" }
    $mtd = Invoke-HubCommand $Shell 'cat /proc/mtd' 15 -Quiet
    foreach ($needle in @('mtd0: 00130000','mtd1: 002d0000','mtd2: 00130000','mtd3: 002d0000')) {
        if ($mtd.Output.ToLowerInvariant() -notmatch [Regex]::Escape($needle)) { throw "Unexpected MTD layout; missing '$needle'" }
    }
    $dev = Invoke-HubCommand $Shell 'ls -l /dev/mtdblock0 /dev/mtdblock1 /dev/mtdblock2 /dev/mtdblock3 /dev/ttyS1 /proc/gpio_ctrl' 15 -Quiet
    if ($dev.ExitCode -ne 0) { throw 'Required MTD/UART/GPIO interfaces are missing' }
    Write-Ok 'Bank1 active, dual-bank enabled, expected RTL8196E layout present'
}

function Stop-StockGateway($Shell) {
    # Some stock gateway builds ignore SIGTERM. Try a graceful stop first, then
    # use SIGKILL only when a known Sengled UART owner is still present.
    $stop = 'killall sengled_startup 2>/dev/null; killall sengled_gateway_app 2>/dev/null; killall sengled_start.sh 2>/dev/null; sleep 2; if ps | grep ''[s]engled_gateway_app\|[s]engled_startup\|[s]engled_start\.sh'' >/dev/null; then echo __SENGLED_FORCE_KILL__; killall -9 sengled_startup 2>/dev/null; killall -9 sengled_gateway_app 2>/dev/null; killall -9 sengled_start.sh 2>/dev/null; sleep 1; fi'
    $result = Invoke-HubCommand $Shell $stop 20 -AllowFailure -Quiet
    if ($result.Output -match '__SENGLED_FORCE_KILL__') {
        Write-Warn 'Stock gateway ignored graceful termination; forced it to release /dev/ttyS1'
    }
    $check = Invoke-HubCommand $Shell "ps | grep '[s]engled'" 10 -AllowFailure -Quiet
    if ($check.Output -match 'sengled_gateway_app|sengled_startup|sengled_start\.sh') {
        throw "Stock Sengled process is still alive and may own /dev/ttyS1: $($check.Output)"
    }
}

function Probe-Coordinator($Shell,[string]$TftpRoot,[string]$PcIp,[int]$Port) {
    Stop-StockGateway $Shell
    $probe = Install-HubExecutable $Shell (Join-Path $Payload 'em357-v641-live-probe-v1') 'em357-v641-live-probe-v1' $TftpRoot $PcIp $Port
    $r = Invoke-HubCommand $Shell $probe 15 -AllowFailure -Quiet
    Write-Host $r.Output -ForegroundColor DarkGray
    if ($r.ExitCode -eq 0 -and $r.Output -match 'EZSP_V7_OK') { return $true }

    # A freshly booted/flashed ASH NCP is not required to accept DATA frames
    # until the host has initialized the link with CANCEL + RST.  The probe
    # configures ttyS1 to 57600 before its first attempt, so send the standard
    # ASH reset frame now, allow the NCP to emit RSTACK, then retry VERSION.
    # RST is a software reset only; it does not enter the bootloader or flash.
    Write-Host '[PROBE] No VERSION response; initializing ASH link and retrying once' -ForegroundColor DarkGray
    $reset = Invoke-HubCommand $Shell 'printf ''\032\300\070\274\176'' > /dev/ttyS1; sleep 1' 10 -AllowFailure -Quiet
    if ($reset.ExitCode -ne 0) {
        Write-Warn "Could not send ASH reset frame (rc=$($reset.ExitCode)): $($reset.Output)"
        return $false
    }

    $r = Invoke-HubCommand $Shell $probe 15 -AllowFailure -Quiet
    Write-Host $r.Output -ForegroundColor DarkGray
    return ($r.ExitCode -eq 0 -and $r.Output -match 'EZSP_V7_OK')
}

function Flash-Coordinator($Shell,[string]$TftpRoot,[string]$PcIp,[int]$Port,[string]$WorkDir) {
    Write-Step 'Flashing onboard EM357 coordinator to EZSP v7'
    Stop-StockGateway $Shell
    $fw = Join-Path $WorkDir 'em357-v641-ncp-uart-sw.ebl'
    Get-CoordinatorFirmware $fw
    Send-FileToHub $Shell $fw '/tmp/em357-v641-ncp-uart-sw.ebl' 'em357-v641-ncp-uart-sw.ebl' $TftpRoot $PcIp $Port
    $flasher = Install-HubExecutable $Shell (Join-Path $Payload 'em357-flash-v641-public-v1') 'em357-flash-v641-public-v1' $TftpRoot $PcIp $Port
    Invoke-HubCommand $Shell 'echo YES > /tmp/FLASH_EM357_NOW' 10 -Quiet | Out-Null
    try {
        Write-Host ''
        Write-Host '[WAIT] Coordinator flash is now running on the hub.' -ForegroundColor Yellow
        Write-Host '[WAIT] This can take several minutes. Progress output is buffered and will appear when the flasher finishes.' -ForegroundColor Yellow
        Write-Host '[WAIT] Keep the hub powered and do not start another reclaim or telnet session.' -ForegroundColor Yellow
        $r = Invoke-HubCommand $Shell $flasher 360 -AllowFailure -Quiet
        Write-Host $r.Output -ForegroundColor DarkGray
        if ($r.ExitCode -ne 0 -or $r.Output -notmatch 'FLASH COMPLETE') {
            throw "EM357 flash failed (rc=$($r.ExitCode)). DO NOT POWER-CYCLE if the flasher reported that it left the bootloader active."
        }
    } finally {
        try { Invoke-HubCommand $Shell 'rm -f /tmp/FLASH_EM357_NOW' 10 -AllowFailure -Quiet | Out-Null } catch { }
    }
    Start-Sleep -Seconds 2
    if (-not (Probe-Coordinator $Shell $TftpRoot $PcIp $Port)) { throw 'EM357 flash completed, but post-flash EZSP v7 probe failed' }
    Write-Ok 'EM357 is running EZSP protocol v7'
}

function Flash-SystemBank2($Shell,$Images,[string]$TftpRoot,[string]$PcIp,[int]$Port) {
    Write-Step 'Flashing reclaimed system to inactive Bank2'
    $flasher = Install-HubExecutable $Shell (Join-Path $Payload 'bank2-safe-flash-v2-block') 'bank2-safe-flash-v2-block' $TftpRoot $PcIp $Port
    Invoke-HubCommand $Shell 'rm -f /tmp/FLASH_BANK2_ROOTFS_NOW /tmp/FLASH_BANK2_KERNEL_NOW' 10 -AllowFailure -Quiet | Out-Null
    $dry = Invoke-HubCommand $Shell $flasher 30 -AllowFailure -Quiet
    Write-Host $dry.Output -ForegroundColor DarkGray
    if ($dry.Output -notmatch 'ACTIVE BOOT BANK REPORTS: 1' -or $dry.Output -notmatch 'mtdblock2.*OK' -or $dry.Output -notmatch 'mtdblock3.*OK') {
        throw 'Bank2 flasher read-only probe did not match the proven layout'
    }

    Send-FileToHub $Shell $Images.Rootfs '/tmp/mtd3-bank2-rootfs-reclaimed.bin' 'mtd3-bank2-rootfs-reclaimed.bin' $TftpRoot $PcIp $Port
    Invoke-HubCommand $Shell 'echo 1 > /tmp/FLASH_BANK2_ROOTFS_NOW' 10 -Quiet | Out-Null
    Write-Host '[WAIT] Bank2 rootfs is being written and byte-verified. Keep the hub powered; output appears when this stage finishes.' -ForegroundColor Yellow
    try {
        $rr = Invoke-HubCommand $Shell $flasher 360 -AllowFailure -Quiet
        Write-Host $rr.Output -ForegroundColor DarkGray
        if ($rr.ExitCode -ne 0 -or $rr.Output -notmatch 'VERIFY: PASS') { throw 'Bank2 rootfs flash/verify failed' }
    } finally {
        Invoke-HubCommand $Shell 'rm -f /tmp/FLASH_BANK2_ROOTFS_NOW' 10 -AllowFailure -Quiet | Out-Null
    }
    Invoke-HubCommand $Shell 'rm -f /tmp/mtd3-bank2-rootfs-reclaimed.bin' 15 -AllowFailure -Quiet | Out-Null
    Write-Ok 'Bank2 rootfs written and byte-for-byte verified'

    Send-FileToHub $Shell $Images.Kernel '/tmp/mtd2-bank2-kernel-reclaimed.bin' 'mtd2-bank2-kernel-reclaimed.bin' $TftpRoot $PcIp $Port
    Invoke-HubCommand $Shell 'echo 1 > /tmp/FLASH_BANK2_KERNEL_NOW' 10 -Quiet | Out-Null
    Write-Host '[WAIT] Bank2 kernel is being written and byte-verified. Keep the hub powered; output appears when this stage finishes.' -ForegroundColor Yellow
    try {
        $kr = Invoke-HubCommand $Shell $flasher 300 -AllowFailure -Quiet
        Write-Host $kr.Output -ForegroundColor DarkGray
        if ($kr.ExitCode -ne 0 -or $kr.Output -notmatch 'VERIFY: PASS') { throw 'Bank2 kernel flash/verify failed' }
    } finally {
        Invoke-HubCommand $Shell 'rm -f /tmp/FLASH_BANK2_KERNEL_NOW' 10 -AllowFailure -Quiet | Out-Null
    }
    $bank = Invoke-HubCommand $Shell 'cat /proc/bootbank' 10 -Quiet
    if ($bank.Output -notmatch '(?m)^1\s*$') { throw 'Active bank changed before reboot; refusing to continue' }
    Write-Ok 'Bank2 kernel written and byte-for-byte verified; Bank1 is still running'
}

function Test-ReclaimedBank2Health($Shell,[string]$HubAddress) {
    $bank = Invoke-HubCommand $Shell 'cat /proc/bootbank' 15 -Quiet
    if ($bank.Output -notmatch '(?m)^2\s*$') {
        throw "Hub is running Bank $($bank.Output.Trim()), not reclaimed Bank2. If Bank2 was already written and verified, cold-power-cycle the hub and run TEST-BANK2-BOOT.cmd again."
    }

    $marker = Invoke-HubCommand $Shell 'test -f /etc/reclaim-build.txt && cat /etc/reclaim-build.txt' 15 -AllowFailure -Quiet
    if ($marker.ExitCode -ne 0 -or $marker.Output -notmatch 'Sengled reclaimed rootfs') {
        throw 'Bank2 is active, but the reclaimed build marker is missing'
    }

    $status = Invoke-HubCommand $Shell 'reclaim-status' 30 -AllowFailure -Quiet
    Write-Host $status.Output
    Wait-TcpPort $HubAddress 6638 30 'EZSP gateway'
    Write-Ok 'Bank2 reclaimed filesystem and TCP/6638 health checks passed'
}

function Confirm-DestructivePlan {
    param(
        [string]$HubAddress,
        [bool]$ForceCoordinatorRequested,
        [bool]$CoordinatorEnabled
    )

    Write-Host ''
    Write-Host 'This operation will:' -ForegroundColor White
    Write-Host '  * temporarily stop the stock Sengled gateway'
    if ($ForceCoordinatorRequested) {
        Write-Host '  * force-flash the onboard EM357 with EmberZNet 6.4.1 / EZSP v7 (debug mode)' -ForegroundColor Yellow
    } elseif ($CoordinatorEnabled) {
        Write-Host '  * replace the onboard EM357 application with EmberZNet 6.4.1 / EZSP v7 unless already v7'
    } else {
        Write-Host '  * skip the onboard EM357 coordinator stage'
    }
    Write-Host '  * dump all four RTL flash partitions to this PC'
    Write-Host '  * build reclaimed firmware from THIS HUB''S OWN Bank1/Bank2 dumps'
    Write-Host '  * write ONLY inactive Bank2 (mtd3 rootfs first, mtd2 kernel last)'
    Write-Host '  * verify both writes byte-for-byte before reboot'
    Write-Host ''
    Write-Host 'Bank1 is never written by this tool.' -ForegroundColor Green
    Write-Host 'Do not remove power during a coordinator or Bank2 flash.' -ForegroundColor Yellow

    if ($ForceCoordinatorRequested) {
        Write-Host 'DEBUG MODE: this programs the public EmberZNet 6.4.1 / EZSP v7 image.' -ForegroundColor Yellow
        Write-Host 'It does not restore Sengled''s original coordinator firmware.' -ForegroundColor Yellow
        $answer = Read-Host "Type FORCE-COORDINATOR to confirm the debug reflash and full reclaim of $HubAddress"
        if ($answer -cne 'FORCE-COORDINATOR') { throw 'Cancelled by user' }
        return $true
    }

    if ($CoordinatorEnabled) {
        Write-Host 'Type RECLAIM to continue. An existing EZSP v7 coordinator is preserved.' -ForegroundColor DarkGray
        $answer = Read-Host "Type RECLAIM to continue with $HubAddress"
        if ($answer -cne 'RECLAIM') { throw 'Cancelled by user' }
        return $false
    }

    $answer = Read-Host "Type RECLAIM to continue with $HubAddress"
    if ($answer -cne 'RECLAIM') { throw 'Cancelled by user' }
    return $false
}

$transcriptStarted = $false
$tftp = $null
$shell = $null
$firewallRule = "Sengled Reclaim TFTP $TftpPort"
$runDir = $null
try {
    Write-Host ''
    Write-Host "Sengled Element Hub Reclaim $Version" -ForegroundColor White
    Write-Host 'One controller: backdoor -> coordinator -> backup -> build -> Bank2 flash -> health check' -ForegroundColor DarkGray

    if (-not [Environment]::Is64BitOperatingSystem) { throw 'This release requires 64-bit Windows' }
    if (-not $BootTestOnly -and -not (Test-Administrator)) { throw 'Run the matching .cmd launcher so the temporary Windows Firewall rule can be installed.' }
    if (@($DryRun,$BackupOnly,$BootTestOnly | Where-Object { $_ }).Count -gt 1) {
        throw '-DryRun, -BackupOnly, and -BootTestOnly cannot be combined.'
    }

    $Hub = Resolve-HubAddress $Hub
    Write-Ok "Hub selected: $Hub"

    $support = Join-Path $Lib 'ReclaimSupport.cs'
    if (-not (Test-Path $support)) { throw "Missing $support" }
    Add-Type -Path $support

    if (-not $DryRun -and -not $BackupOnly -and -not $BootTestOnly) {
        foreach ($required in @('bank2-safe-flash-v2-block','ezsp_gateway-v3','hub-chmodx-v1','ezsp_start.sh','em357-flash-v641-public-v1','em357-v641-live-probe-v1')) {
            if (-not (Test-Path (Join-Path $Payload $required))) { throw "Package is incomplete: missing payload/$required" }
        }
    }

    $stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMdd-HHmmss')
    $runDir = Join-Path $ScriptRoot ("output\{0}-{1}" -f ($Hub -replace '[^0-9A-Za-z.-]','_'),$stamp)
    $backupDir = Join-Path $runDir 'backup'
    $buildDir = Join-Path $runDir 'build'
    $tftpRoot = Join-Path $runDir 'tftp'
    New-Item -ItemType Directory -Force -Path $backupDir,$buildDir,$tftpRoot | Out-Null
    Start-Transcript -Path (Join-Path $runDir 'reclaim.log') -Force | Out-Null
    $transcriptStarted = $true

    Write-Step 'Opening stock hub through TCP/8686 backdoor'

    # First try to use an already-running telnet shell. This is a REAL shell
    # check, not just a TCP/23 port probe. If it works, do not touch telnetd.
    Write-Host 'Checking for an already-running telnet shell...' -ForegroundColor DarkGray
    $shell = Connect-HubShell $Hub -Attempts 1 -ReturnNull -SuppressWarnings

    if ($shell) {
        Write-Ok 'Existing telnet shell is usable; skipping AT+START_TELNETD=1'
    } else {
        # No usable shell exists. Start telnetd through the stock 8686 service
        # ONCE, then only retry the real shell connection. Never re-issue the AT
        # command inside the retry loop.
        Wait-TcpPort $Hub 8686 60 'stock debug service'
        Write-Host 'No usable telnet shell found; starting telnetd once...' -ForegroundColor DarkGray
        Invoke-SengledAT $Hub @('AT+START_TELNETD=1')
        Start-Sleep -Seconds 2

        $shell = Connect-HubShell $Hub -Attempts 5 -DelaySeconds 2
    }

    if ($BootTestOnly) {
        Write-Step 'Testing reclaimed Bank2 boot and gateway only'
        Test-ReclaimedBank2Health $shell $Hub
        Write-Host ''
        Write-Host '============================================================' -ForegroundColor Green
        Write-Host 'BANK2 BOOT TEST PASSED' -ForegroundColor Green
        Write-Host "Home Assistant ZHA socket: socket://${Hub}:6638" -ForegroundColor White
        Write-Host 'No coordinator or RTL flash write was performed.' -ForegroundColor White
        Write-Host '============================================================' -ForegroundColor Green
        return
    }

    Validate-StockHub $shell

    $pcIp = Get-LocalIPv4ForRemote $Hub
    Write-Ok "PC address visible to hub: $pcIp"
    & netsh advfirewall firewall delete rule name="$firewallRule" 2>$null | Out-Null
    & netsh advfirewall firewall add rule name="$firewallRule" dir=in action=allow protocol=UDP localport=$TftpPort | Out-Null
    $tftp = New-Object -TypeName SengledReclaim.TftpServer -ArgumentList @($tftpRoot,$TftpPort)
    $tftp.Start()

    if ($DryRun) {
        Write-Warn 'DRY RUN: stopping before any coordinator/system flash.'
        Save-HubMetadata $shell $backupDir
        Write-Ok "Dry-run metadata saved to $backupDir"
        return
    }

    if ($BackupOnly) {
        Write-Warn 'BACKUP ONLY: reading all four RTL flash partitions; no coordinator or flash-write stages will run.'
        Backup-RtlFlash $shell $backupDir $tftpRoot $pcIp $TftpPort
        Write-Host ''
        Write-Host '============================================================' -ForegroundColor Green
        Write-Host 'READ-ONLY FLASH BACKUP COMPLETE' -ForegroundColor Green
        Write-Host 'No coordinator firmware or RTL flash was modified.' -ForegroundColor White
        Write-Host "Backup artifacts: $backupDir" -ForegroundColor White
        Write-Host '============================================================' -ForegroundColor Green
        return
    }

    $ForceCoordinator = Confirm-DestructivePlan -HubAddress $Hub `
        -ForceCoordinatorRequested ([bool]$ForceCoordinator -and -not [bool]$SkipCoordinator) `
        -CoordinatorEnabled (-not [bool]$SkipCoordinator)

    if (-not $SkipCoordinator) {
        Write-Step 'Checking onboard coordinator'
        $alreadyV7 = Probe-Coordinator $shell $tftpRoot $pcIp $TftpPort
        if ($alreadyV7 -and -not $ForceCoordinator) {
            Write-Ok 'Coordinator already speaks EZSP v7; destructive coordinator reflash skipped'
        } else {
            Flash-Coordinator $shell $tftpRoot $pcIp $TftpPort $runDir
        }
    } else {
        Write-Warn 'Coordinator stage skipped by -SkipCoordinator'
    }

    Backup-RtlFlash $shell $backupDir $tftpRoot $pcIp $TftpPort
    $images = Build-ReclaimedImages $backupDir $buildDir
    Flash-SystemBank2 $shell $images $tftpRoot $pcIp $TftpPort

    if ($NoReboot) {
        Write-Warn 'All flashes verified. -NoReboot requested, so Bank1 remains active until you reboot manually.'
        return
    }

    Write-Step 'Rebooting into reclaimed Bank2'
    $shell.SendRawLine('reboot')
    $shell.Dispose(); $shell = $null
    Start-Sleep -Seconds 4

    # After reboot, wait for the reclaimed shell by attempting the real telnet
    # session instead of repeatedly opening and closing TCP/23 probe sockets.
    $shell = Connect-HubShell $Hub -Attempts 20 -DelaySeconds 3
    try {
        Test-ReclaimedBank2Health $shell $Hub
    } catch {
        if ($_.Exception.Message -notmatch 'not reclaimed Bank2') { throw }

        $shell.Dispose(); $shell = $null
        Write-Host ''
        Write-Warn 'The verified Bank2 image needs one cold boot on this hub. Nothing needs to be reflashed.'
        Write-Host '1. Unplug power from the hub.' -ForegroundColor White
        Write-Host '2. Wait 10 seconds.' -ForegroundColor White
        Write-Host '3. Reconnect power and wait for the hub to start.' -ForegroundColor White
        do {
            $powered = (Read-Host 'Type POWERED after reconnecting the hub').Trim()
        } until ($powered -ieq 'POWERED')

        Write-Host '[WAIT] Waiting for the hub to finish its cold boot...' -ForegroundColor Yellow
        $shell = Connect-HubShell $Hub -Attempts 30 -DelaySeconds 3
        Test-ReclaimedBank2Health $shell $Hub
    }

    Write-Host ''
    Write-Host '============================================================' -ForegroundColor Green
    Write-Host 'RECLAIM COMPLETE' -ForegroundColor Green
    Write-Host "Home Assistant ZHA socket: socket://${Hub}:6638" -ForegroundColor White
    Write-Host "Recovery Bank1 was not modified." -ForegroundColor White
    Write-Host "Backup + build artifacts: $runDir" -ForegroundColor White
    Write-Host '============================================================' -ForegroundColor Green
}
catch {
    Write-Host ''
    Write-Host 'RECLAIM STOPPED' -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    if ($runDir) { Write-Host "Logs/artifacts: $runDir" -ForegroundColor Yellow }
    Write-Host 'No automatic retry will be attempted. Read the last stage before doing anything else.' -ForegroundColor Yellow
    exit 1
}
finally {
    if ($shell) { try { $shell.Dispose() } catch { } }
    if ($tftp) { try { $tftp.Stop() } catch { } }
    try { & netsh advfirewall firewall delete rule name="$firewallRule" 2>$null | Out-Null } catch { }
    if ($transcriptStarted) { try { Stop-Transcript | Out-Null } catch { } }
    if (-not $KeepWork -and $runDir) {
        try { Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path $runDir 'tftp') } catch { }
    }
}
