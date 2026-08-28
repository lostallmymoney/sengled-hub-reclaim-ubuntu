# Development workflow

This directory is the canonical source repository for Sengled Hub Reclaim from
version `0.2-rc4` forward. Do not make release changes in the older parent
folder and copy them back here.

## Before committing

1. Parse the controller and compile its embedded support library:

   ```powershell
   $errors = $null
   [void][Management.Automation.Language.Parser]::ParseFile(
       (Resolve-Path '.\Reclaim-SengledHub.ps1'),
       [ref]$null,
       [ref]$errors
   )
   if ($errors.Count) { $errors; exit 1 }
   Add-Type -Path '.\lib\ReclaimSupport.cs'
   ```

2. When image-building code changes, run `tests\Test-LocalBuild.ps1` against a
   known complete backup. It builds and verifies images locally without
   connecting to a hub.

3. Regenerate the package manifest from this repository root:

   ```powershell
   .\Generate-SHA256SUMS.ps1
   ```

4. Review `git status` and confirm that `output\`, downloaded `cache\` content,
   firmware downloads, and release ZIPs are not staged.

The distributable ZIP should contain this repository's tracked files without
the `.git` directory. Bank1 backups and other per-device runtime artifacts must
never be included.
