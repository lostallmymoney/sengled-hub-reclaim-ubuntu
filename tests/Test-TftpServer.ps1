#requires -Version 5.1
<#
.SYNOPSIS
    Tests or runs the built-in Sengled Reclaim TFTP server.

.DESCRIPTION
    Loads SengledReclaim.TftpServer from lib\ReclaimSupport.cs. Test mode uses
    an in-process TFTP client and real UDP packets, so it does not require the
    optional Windows tftp.exe client.

.EXAMPLE
    .\tests\Test-TftpServer.ps1

.EXAMPLE
    .\tests\Test-TftpServer.ps1 -Mode Serve -Root C:\TftpRoot -Port 6969

.EXAMPLE
    .\tests\Test-TftpServer.ps1 -Mode TestAndServe -Root .\tftp-root
#>

[CmdletBinding()]
param(
    [ValidateSet('Test', 'Serve', 'TestAndServe')]
    [string]$Mode = 'Test',

    [string]$Root,

    [ValidateRange(1, 65535)]
    [int]$Port = 6969,

    [ValidateRange(1, 60)]
    [int]$TimeoutSeconds = 5,

    [switch]$KeepWork
)

$ErrorActionPreference = 'Stop'
$script:TftpBlockSize = 512

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Test-DirectoryWritable {
    param([string]$Path)
    $probe = Join-Path $Path ('.permission-test-' + [Guid]::NewGuid().ToString('N'))
    try {
        [IO.File]::WriteAllText($probe, 'Sengled reclaim permission test')
    } finally {
        if (Test-Path -LiteralPath $probe) { Remove-Item -LiteralPath $probe -Force }
    }
}

function Assert-TftpFirewallRule {
    param(
        [string]$DisplayName,
        [int]$Port
    )

    $rules = @(Get-NetFirewallRule -DisplayName $DisplayName -ErrorAction SilentlyContinue)
    if ($rules.Count -ne 1) {
        throw "Expected exactly one Windows Firewall rule named '$DisplayName'; found $($rules.Count)"
    }
    $rule = $rules[0]
    if ([string]$rule.Enabled -ne 'True' -or
        [string]$rule.Direction -ne 'Inbound' -or
        [string]$rule.Action -ne 'Allow') {
        throw "Firewall rule '$DisplayName' is not an enabled inbound allow rule"
    }

    $filters = @($rule | Get-NetFirewallPortFilter)
    if ($filters.Count -ne 1 -or
        [string]$filters[0].Protocol -ne 'UDP' -or
        [string]$filters[0].LocalPort -ne [string]$Port) {
        throw "Firewall rule '$DisplayName' does not allow UDP/$Port"
    }
}

function Assert-TftpFirewallRuleRemoved {
    param([string]$DisplayName)
    $remaining = @(Get-NetFirewallRule -DisplayName $DisplayName -ErrorAction SilentlyContinue)
    if ($remaining.Count -ne 0) {
        throw "Temporary Windows Firewall rule '$DisplayName' remains after cleanup"
    }
}

function Get-U16 {
    param([byte[]]$Packet, [int]$Offset)
    return ([int]$Packet[$Offset] -shl 8) -bor [int]$Packet[$Offset + 1]
}

function New-TftpPacket {
    param(
        [ValidateRange(0, 65535)][int]$Opcode,
        [ValidateRange(0, 65535)][int]$Block,
        [byte[]]$Data = @()
    )

    $packet = [byte[]]::new(4 + $Data.Length)
    $packet[0] = [byte](($Opcode -shr 8) -band 0xff)
    $packet[1] = [byte]($Opcode -band 0xff)
    $packet[2] = [byte](($Block -shr 8) -band 0xff)
    $packet[3] = [byte]($Block -band 0xff)
    if ($Data.Length) { [Array]::Copy($Data, 0, $packet, 4, $Data.Length) }
    return $packet
}

function New-TftpRequest {
    param(
        [ValidateSet(1, 2)][int]$Opcode,
        [string]$FileName
    )

    $name = [Text.Encoding]::ASCII.GetBytes($FileName)
    $mode = [Text.Encoding]::ASCII.GetBytes('octet')
    $packet = [byte[]]::new(2 + $name.Length + 1 + $mode.Length + 1)
    $packet[1] = [byte]$Opcode
    [Array]::Copy($name, 0, $packet, 2, $name.Length)
    [Array]::Copy($mode, 0, $packet, 3 + $name.Length, $mode.Length)
    return $packet
}

function New-TftpClient {
    param([int]$TimeoutMilliseconds)
    $client = [Net.Sockets.UdpClient]::new(0)
    $client.Client.ReceiveTimeout = $TimeoutMilliseconds
    return $client
}

function Receive-TftpPacket {
    param([Net.Sockets.UdpClient]$Client)
    $remote = [Net.IPEndPoint]::new([Net.IPAddress]::Any, 0)
    try {
        $packet = $Client.Receive([ref]$remote)
    } catch [Net.Sockets.SocketException] {
        if ($_.Exception.SocketErrorCode -eq [Net.Sockets.SocketError]::TimedOut) {
            throw 'Timed out waiting for a TFTP packet'
        }
        throw
    }
    return [pscustomobject]@{ Packet = $packet; Remote = $remote }
}

function Invoke-TftpDownload {
    param(
        [string]$FileName,
        [Net.IPEndPoint]$Server,
        [int]$TimeoutMilliseconds
    )

    $client = New-TftpClient $TimeoutMilliseconds
    $output = [IO.MemoryStream]::new()
    try {
        $request = New-TftpRequest 1 $FileName
        [void]$client.Send($request, $request.Length, $Server)
        $expected = 1
        while ($true) {
            $received = Receive-TftpPacket $client
            $packet = $received.Packet
            $opcode = if ($packet.Length -ge 2) { Get-U16 $packet 0 } else { -1 }
            if ($opcode -eq 5) {
                $message = if ($packet.Length -gt 5) {
                    [Text.Encoding]::ASCII.GetString($packet, 4, $packet.Length - 5)
                } else { 'Unknown TFTP error' }
                throw "TFTP server error: $message"
            }
            if ($packet.Length -lt 4 -or $opcode -ne 3 -or (Get-U16 $packet 2) -ne $expected) {
                throw "Unexpected packet while waiting for DATA block $expected"
            }
            $count = $packet.Length - 4
            if ($count) { $output.Write($packet, 4, $count) }
            $ack = New-TftpPacket 4 $expected
            [void]$client.Send($ack, $ack.Length, $received.Remote)
            if ($count -lt $script:TftpBlockSize) { break }
            $expected = ($expected + 1) -band 0xffff
        }
        return $output.ToArray()
    } finally {
        $output.Dispose()
        $client.Dispose()
    }
}

function Invoke-TftpUpload {
    param(
        [string]$FileName,
        [byte[]]$Content,
        [Net.IPEndPoint]$Server,
        [int]$TimeoutMilliseconds
    )

    $client = New-TftpClient $TimeoutMilliseconds
    try {
        $request = New-TftpRequest 2 $FileName
        [void]$client.Send($request, $request.Length, $Server)
        $received = Receive-TftpPacket $client
        if ($received.Packet.Length -lt 4 -or (Get-U16 $received.Packet 0) -ne 4 -or
            (Get-U16 $received.Packet 2) -ne 0) {
            throw 'TFTP server did not acknowledge the write request'
        }

        $serverTransferEndpoint = $received.Remote
        $offset = 0
        $block = 1
        do {
            $count = [Math]::Min($script:TftpBlockSize, $Content.Length - $offset)
            $chunk = [byte[]]::new($count)
            if ($count) { [Array]::Copy($Content, $offset, $chunk, 0, $count) }
            $data = New-TftpPacket 3 $block $chunk
            [void]$client.Send($data, $data.Length, $serverTransferEndpoint)
            $received = Receive-TftpPacket $client
            if ($received.Packet.Length -lt 4 -or (Get-U16 $received.Packet 0) -ne 4 -or
                (Get-U16 $received.Packet 2) -ne $block) {
                throw "Unexpected packet while waiting for ACK block $block"
            }
            $offset += $count
            $block = ($block + 1) -band 0xffff
        } while ($count -eq $script:TftpBlockSize)
    } finally {
        $client.Dispose()
    }
}

function Test-ByteArrayEqual {
    param([byte[]]$Expected, [byte[]]$Actual)
    if ($Expected.Length -ne $Actual.Length) { return $false }
    for ($i = 0; $i -lt $Expected.Length; $i++) {
        if ($Expected[$i] -ne $Actual[$i]) { return $false }
    }
    return $true
}

function Invoke-TftpSelfTest {
    param(
        [int]$TimeoutMilliseconds,
        [int]$Port,
        [string]$Root,
        [bool]$PreserveFiles
    )

    $testRoot = [IO.Path]::GetFullPath($Root)
    [void][IO.Directory]::CreateDirectory($testRoot)
    $server = [SengledReclaim.TftpServer]::new($testRoot, $Port)
    $endpoint = [Net.IPEndPoint]::new([Net.IPAddress]::Loopback, $Port)
    $createdFiles = [Collections.Generic.List[string]]::new()
    try {
        # 0x2D0000 is the largest partition and image transferred by the
        # production workflow. It is also an exact multiple of 512, requiring
        # the terminating zero-length TFTP DATA block.
        $productionTransferSize = 0x2D0000
        $downloadExpected = [byte[]]::new($productionTransferSize)
        [Random]::new(12345).NextBytes($downloadExpected)
        $downloadName = 'test-mtd3-bank2-rootfs-reclaimed.bin'
        $downloadPath = Join-Path $testRoot $downloadName
        [IO.File]::WriteAllBytes($downloadPath, $downloadExpected)
        $createdFiles.Add($downloadPath)

        try {
            $server.Start()
        } catch [Net.Sockets.SocketException] {
            throw "Could not bind the production TFTP endpoint UDP/$Port. Stop the process using that port or select another with -Port. $($_.Exception.Message)"
        }
        $downloadActual = Invoke-TftpDownload $downloadName $endpoint $TimeoutMilliseconds
        if (-not (Test-ByteArrayEqual $downloadExpected $downloadActual)) {
            throw 'RRQ test returned different bytes than the source file'
        }
        Write-Host "[PASS] RRQ PC-to-hub simulation ($productionTransferSize bytes, production rootfs size)" -ForegroundColor Green

        $uploadExpected = [byte[]]::new($productionTransferSize)
        [Random]::new(67890).NextBytes($uploadExpected)
        $uploadName = 'test-mtd3-bank2-rootfs.bin'
        $uploadPath = Join-Path $testRoot $uploadName
        $createdFiles.Add($uploadPath)
        $createdFiles.Add($uploadPath + '.part')
        Invoke-TftpUpload $uploadName $uploadExpected $endpoint $TimeoutMilliseconds
        $commitDeadline = [DateTime]::UtcNow.AddMilliseconds($TimeoutMilliseconds)
        while (-not [IO.File]::Exists($uploadPath) -and [DateTime]::UtcNow -lt $commitDeadline) {
            Start-Sleep -Milliseconds 20
        }
        if (-not [IO.File]::Exists($uploadPath)) {
            throw 'WRQ test was acknowledged but the uploaded file was not committed'
        }
        $uploadActual = [IO.File]::ReadAllBytes($uploadPath)
        if (-not (Test-ByteArrayEqual $uploadExpected $uploadActual)) {
            throw 'WRQ test wrote different bytes than the uploaded content'
        }
        Write-Host "[PASS] WRQ hub-to-PC simulation ($productionTransferSize bytes, commit verified)" -ForegroundColor Green

        try {
            [void](Invoke-TftpDownload 'missing.bin' $endpoint $TimeoutMilliseconds)
            throw 'Missing-file request unexpectedly succeeded'
        } catch {
            if ($_.Exception.Message -notlike 'TFTP server error: File not found*') { throw }
        }
        Write-Host '[PASS] Missing file returns a TFTP error' -ForegroundColor Green
        Write-Host 'All TFTP self-tests passed.' -ForegroundColor Cyan
    } finally {
        $server.Stop()
        if (-not $PreserveFiles) {
            foreach ($createdFile in $createdFiles) {
                if (Test-Path -LiteralPath $createdFile) {
                    Remove-Item -LiteralPath $createdFile -Force
                }
            }
        }
    }
}

$scriptPath = $MyInvocation.MyCommand.Path
if ([string]::IsNullOrWhiteSpace($scriptPath)) {
    throw 'This test must be run as a .ps1 file.'
}
$testsRoot = Split-Path -Parent ([IO.Path]::GetFullPath($scriptPath))
$projectRoot = Split-Path -Parent $testsRoot
$supportSource = Join-Path $projectRoot 'lib\ReclaimSupport.cs'
if (-not (Test-Path -LiteralPath $supportSource)) {
    throw "Missing support library: $supportSource"
}
if (-not ('SengledReclaim.TftpServer' -as [type])) {
    Add-Type -Path $supportSource
}

$timeoutMilliseconds = $TimeoutSeconds * 1000
if ($Mode -in @('Test', 'TestAndServe')) {
    if (-not (Test-Administrator)) {
        throw 'Run tests\RUN-TFTP-TEST.cmd or start PowerShell as Administrator. The production setup requires a temporary Windows Firewall rule.'
    }

    $stamp = [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss')
    $runDir = Join-Path $projectRoot ("output\tftp-test-$stamp")
    $backupDir = Join-Path $runDir 'backup'
    $buildDir = Join-Path $runDir 'build'
    $tftpRoot = Join-Path $runDir 'tftp'
    $firewallRule = "Sengled Reclaim TFTP $Port"
    $transcriptStarted = $false
    try {
        # Match the production controller's directory creation and transcript
        # setup, then explicitly prove every stage directory is writable.
        New-Item -ItemType Directory -Force -Path $backupDir,$buildDir,$tftpRoot | Out-Null
        Start-Transcript -Path (Join-Path $runDir 'tftp-test.log') -Force | Out-Null
        $transcriptStarted = $true
        Test-DirectoryWritable $backupDir
        Test-DirectoryWritable $buildDir
        Test-DirectoryWritable $tftpRoot
        Write-Host '[PASS] Production output tree created and writable' -ForegroundColor Green

        # Use the same rule name, protocol, direction, action, and local port as
        # Reclaim-SengledHub.ps1. The rule is removed in finally on every path.
        & netsh advfirewall firewall delete rule name="$firewallRule" 2>$null | Out-Null
        Assert-TftpFirewallRuleRemoved $firewallRule
        & netsh advfirewall firewall add rule name="$firewallRule" dir=in action=allow protocol=UDP localport=$Port | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Could not install temporary inbound UDP/$Port Windows Firewall rule" }
        Assert-TftpFirewallRule $firewallRule $Port
        Write-Host "[PASS] Temporary inbound allow rule verified for UDP/$Port" -ForegroundColor Green

        Invoke-TftpSelfTest $timeoutMilliseconds $Port $tftpRoot $KeepWork.IsPresent
        Write-Host "[PASS] Production-style TFTP root: $tftpRoot" -ForegroundColor Green
        Write-Host "Test artifacts and transcript: $runDir" -ForegroundColor Cyan
    } finally {
        $firewallCleanupError = $null
        try {
            & netsh advfirewall firewall delete rule name="$firewallRule" 2>$null | Out-Null
            Assert-TftpFirewallRuleRemoved $firewallRule
            Write-Host '[PASS] Temporary Windows Firewall rule removed' -ForegroundColor Green
        } catch {
            $firewallCleanupError = $_.Exception
        }
        if ($transcriptStarted) { try { Stop-Transcript | Out-Null } catch { } }
        if (-not $KeepWork -and $runDir) {
            try { Remove-Item -LiteralPath $tftpRoot -Recurse -Force -ErrorAction SilentlyContinue } catch { }
        }
        if ($firewallCleanupError) { throw $firewallCleanupError }
    }
}

if ($Mode -in @('Serve', 'TestAndServe')) {
    if ([string]::IsNullOrWhiteSpace($Root)) {
        $Root = Join-Path $projectRoot 'tftp-root'
    }
    $resolvedRoot = [IO.Path]::GetFullPath($Root)
    [void][IO.Directory]::CreateDirectory($resolvedRoot)
    $server = [SengledReclaim.TftpServer]::new($resolvedRoot, $Port)
    try {
        $server.Start()
        Write-Host "Serving $resolvedRoot on UDP/$Port. Press Ctrl+C to stop." -ForegroundColor Cyan
        while ($true) { Start-Sleep -Seconds 1 }
    } finally {
        $server.Stop()
        Write-Host 'TFTP server stopped.'
    }
}
