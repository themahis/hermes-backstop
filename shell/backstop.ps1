# Backstop shell hook for Windows PowerShell 5.1 and PowerShell 7+.
#
# Wraps an opt-in list of build/test commands. The command runs exactly once, its output streams
# to your console as usual and is also kept in memory; when it exits with a real failure the tail
# of that output, the command line, the exit code and the directory are written to one JSON file
# under <HERMES_HOME>\plugin-data\backstop\spool. Nothing is re-run, nothing is sent anywhere by
# this script. The Hermes gateway picks the file up (see the plugin README).
#
# Trade-off: a wrapped command writes to a pipe, not a console, so it prints without colours and
# cannot prompt. Interactive commands are passed through untouched (see Test-BackstopInteractive).
# Turn it off for one shell with  $env:BACKSTOP_DISABLE = '1'.

$global:BackstopVersion = '0.1.0'

function global:Get-BackstopHome {
    if ($env:HERMES_HOME) { return $env:HERMES_HOME }
    if ($env:LOCALAPPDATA) { return (Join-Path $env:LOCALAPPDATA 'hermes') }
    return (Join-Path $HOME '.hermes')
}

$global:BackstopSpool = Join-Path (Get-BackstopHome) 'plugin-data\backstop\spool'
$global:BackstopOpen = Join-Path (Get-BackstopHome) 'plugin-data\backstop\open'
$global:BackstopDefault = @('npm', 'npx', 'pnpm', 'yarn', 'bun', 'deno', 'tsc', 'node', 'python', 'py',
    'pytest', 'cargo', 'go', 'dotnet', 'make', 'gradle', 'mvn', 'eslint', 'jest', 'vitest', 'ruff', 'mypy')
$global:BackstopCommands = if ($env:BACKSTOP_COMMANDS) {
    $env:BACKSTOP_COMMANDS -split '[,; ]+' | Where-Object { $_ }
} else { $global:BackstopDefault }
$global:BackstopTargets = @{}

# Exit codes that are not failures: 130 / 0xC000013A are Ctrl-C.
$global:BackstopIgnoredExit = @(0, 130, -1073741510, 3221225786)

function global:Test-BackstopInteractive {
    param([string]$Name, [object[]]$ArgList)
    $first = if ($ArgList.Count -gt 0) { "$($ArgList[0])" } else { '' }
    if ($ArgList.Count -eq 0 -and $Name -in @('node', 'python', 'py', 'deno', 'bun')) { return $true }  # REPL
    if ($first -in @('-i', '--interactive', 'repl', 'login', 'adduser', 'init', 'create', 'publish', 'shell')) { return $true }
    if ($Name -in @('npm', 'pnpm', 'yarn', 'bun') -and $first -in @('run', 'start') -and
        ($ArgList -match '^(dev|start|serve|watch)$')) { return $true }                                 # dev servers
    if ($ArgList -match '^(--watch|-w|watch)$') { return $true }
    return $false
}

function global:Invoke-BackstopCommand {
    param([string]$Name, [string]$Exe, [object[]]$ArgList)
    if (-not $ArgList) { $ArgList = @() }
    if ($env:BACKSTOP_DISABLE -eq '1' -or (Test-BackstopInteractive $Name $ArgList)) {
        & $Exe @ArgList
        return
    }
    $ErrorActionPreference = 'Continue'
    if (Test-Path variable:PSNativeCommandUseErrorActionPreference) { $PSNativeCommandUseErrorActionPreference = $false }
    $buffer = New-Object System.Collections.Generic.List[string]
    $watch = [Diagnostics.Stopwatch]::StartNew()
    & $Exe @ArgList 2>&1 | ForEach-Object {
        $line = if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.Exception.Message } else { "$_" }
        $buffer.Add($line)
        if ($buffer.Count -gt 2000) { $buffer.RemoveAt(0) }
        $line
    }
    # Not reached on Ctrl-C: the pipeline is stopped before this line, so an interrupt is never recorded.
    $code = $LASTEXITCODE
    $watch.Stop()
    $global:LASTEXITCODE = $code
    $cwd = (Get-Location).ProviderPath
    $command = (@($Name) + ($ArgList | ForEach-Object { "$_" })) -join ' '
    $marker = Join-Path $global:BackstopOpen ((Get-BackstopKey $cwd $command) + '.txt')
    try {
        if ($code -eq 0) {
            # Same command, same directory, now passing: close the open incident (if any).
            if (Test-Path -LiteralPath $marker) {
                $failed = (Get-Content -LiteralPath $marker -Raw).Trim()
                if ($failed) {
                    $null = Write-BackstopRecord ([ordered]@{ kind = 'resolved'; resolves = $failed; cwd = $cwd; command = $command
                        exit = 0; duration_ms = $watch.ElapsedMilliseconds })
                    [IO.File]::WriteAllText($marker, '')
                    Remove-Item -LiteralPath $marker -Force -ErrorAction SilentlyContinue
                    Write-Host "[backstop] passes now - incident closed" -ForegroundColor DarkGreen
                }
            }
            return
        }
        if ($code -in $global:BackstopIgnoredExit) { return }
        $output = $buffer -join "`n"
        if ($output.Length -gt 64000) { $output = $output.Substring($output.Length - 64000) }
        $id = Write-BackstopRecord ([ordered]@{ cwd = $cwd; command = $command; exit = $code
                duration_ms = $watch.ElapsedMilliseconds; output = $output })
        if (-not (Test-Path -LiteralPath $global:BackstopOpen)) { New-Item -ItemType Directory -Force -Path $global:BackstopOpen | Out-Null }
        [IO.File]::WriteAllText($marker, $id)
        Write-Host "[backstop] exit $code - sent to Hermes for a read-only diagnosis" -ForegroundColor DarkCyan
    } catch {
        Write-Host "[backstop] could not record this run: $($_.Exception.Message)" -ForegroundColor DarkYellow
    }
}

function global:Get-BackstopKey {
    param([string]$Cwd, [string]$Command)
    $sha = [Security.Cryptography.SHA1]::Create()
    $bytes = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Cwd.ToLowerInvariant() + '|' + $Command))
    -join ($bytes[0..7] | ForEach-Object { $_.ToString('x2') })
}

function global:Write-BackstopRecord {
    param([System.Collections.Specialized.OrderedDictionary]$Fields)
    $id = (Get-Date -Format 'yyyyMMddHHmmssfff') + '-' + ([guid]::NewGuid().ToString('N').Substring(0, 8))
    $record = [ordered]@{ v = 1; id = $id; ts = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000
        shell = 'powershell ' + $PSVersionTable.PSVersion.ToString() }
    foreach ($k in $Fields.Keys) { $record[$k] = $Fields[$k] }
    if (-not (Test-Path -LiteralPath $global:BackstopSpool)) { New-Item -ItemType Directory -Force -Path $global:BackstopSpool | Out-Null }
    # A Windows-exclusive handle keeps the watcher from reading or deleting a partial record.
    # Publication needs only file creation/write access, without a directory rename permission.
    $path = Join-Path $global:BackstopSpool "$id.json"
    $bytes = (New-Object System.Text.UTF8Encoding($false)).GetBytes(($record | ConvertTo-Json -Depth 3 -Compress))
    $stream = [IO.File]::Open($path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try {
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush()
    } finally {
        $stream.Dispose()
    }
    return $id
}

foreach ($name in $global:BackstopCommands) {
    $app = Get-Command -Name $name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $app) { continue }
    $global:BackstopTargets[$name] = $app.Source
    $exe = $app.Source.Replace("'", "''")
    $body = @"
if (`$MyInvocation.ExpectingInput -or `$MyInvocation.PipelineLength -gt 1) { `$input | & '$exe' @args; return }
Invoke-BackstopCommand -Name '$name' -Exe '$exe' -ArgList `$args
"@
    Set-Item -Path "function:global:$name" -Value ([scriptblock]::Create($body))
}

function global:Get-BackstopStatus {
    [pscustomobject]@{
        Version  = $global:BackstopVersion
        Disabled = ($env:BACKSTOP_DISABLE -eq '1')
        Spool    = $global:BackstopSpool
        Wrapped  = ($global:BackstopTargets.Keys | Sort-Object) -join ', '
    }
}

function global:Disable-Backstop {
    foreach ($name in @($global:BackstopTargets.Keys)) {
        Remove-Item -Path "function:global:$name" -ErrorAction SilentlyContinue
    }
    $global:BackstopTargets.Clear()
}
