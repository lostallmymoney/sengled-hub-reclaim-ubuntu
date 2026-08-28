# Generate-SHA256SUMS.ps1

$Root       = (Get-Location).Path
$OutputFile = Join-Path $Root 'SHA256SUMS.txt'
$ExcludeDir = Join-Path $Root 'output'
$ReleaseDir = Join-Path $Root 'release'
$GitDir     = Join-Path $Root '.git'
$CacheDir   = Join-Path $Root 'cache'
$CacheReadme = Join-Path $CacheDir 'README.txt'
$ScriptFile = $MyInvocation.MyCommand.Path

$lines = Get-ChildItem -Path $Root -File -Recurse |
    Where-Object {
        # Exclude SHA256SUMS.txt
        $_.FullName -ne $OutputFile -and

        # Exclude this script itself
        $_.FullName -ne $ScriptFile -and

        # Exclude the entire output folder
        -not $_.FullName.StartsWith(
            $ExcludeDir + '\',
            [System.StringComparison]::OrdinalIgnoreCase
        ) -and

        # Exclude locally assembled release folders and sibling release ZIPs.
        -not $_.FullName.StartsWith(
            $ReleaseDir + '\',
            [System.StringComparison]::OrdinalIgnoreCase
        ) -and
        -not $_.FullName.StartsWith(
            $GitDir + '\',
            [System.StringComparison]::OrdinalIgnoreCase
        ) -and
        $_.Name -notlike 'Sengled-Hub-Reclaim-*.zip' -and

        # Downloaded build tools are runtime cache, not packaged files. Keep
        # only the cache placeholder/readme in the distributable manifest.
        ($_.FullName -eq $CacheReadme -or -not $_.FullName.StartsWith(
            $CacheDir + '\',
            [System.StringComparison]::OrdinalIgnoreCase
        ))
    } |
    ForEach-Object {
        $relativePath = $_.FullName.Substring($Root.Length).TrimStart('\', '/')
        $relativePath = $relativePath -replace '/', '\'

        # Files inside folders come before root-level files
        $sortGroup = if ($relativePath.Contains('\')) { 0 } else { 1 }

        [PSCustomObject]@{
            SortGroup = $sortGroup
            Path      = $relativePath
            Hash      = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    } |
    Sort-Object SortGroup, Path |
    ForEach-Object {
        "$($_.Hash)  $($_.Path)"
    }

[System.IO.File]::WriteAllLines(
    $OutputFile,
    $lines,
    [System.Text.Encoding]::ASCII
)

Write-Host "Created $OutputFile"
Write-Host "$($lines.Count) files hashed."
