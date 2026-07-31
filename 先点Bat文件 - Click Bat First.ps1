param(
    [switch]$CheckOnly,
    [switch]$Json
)

$ErrorActionPreference = 'Stop'

$scriptRoot = $PSScriptRoot
if (-not $scriptRoot) {
    $scriptRoot = Split-Path -Path $MyInvocation.MyCommand.Path -Parent
}
$scriptRoot = [System.IO.Path]::GetFullPath($scriptRoot)
$bootstrapPath = Join-Path $scriptRoot 'codex_python_runtime_bootstrap.py'
$localAppDataRoot = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
if (-not $localAppDataRoot) { $localAppDataRoot = [string]$env:LOCALAPPDATA }
if (-not $localAppDataRoot) { $localAppDataRoot = [System.IO.Path]::GetTempPath() }
$systemRoot = Join-Path $localAppDataRoot 'PC_REHD_Code_X'
$temporaryRoot = Join-Path ([System.IO.Path]::GetTempPath()) 'PC_REHD_Code_X'
$managedBaselinePython = Join-Path $localAppDataRoot 'Programs\PC_REHD_Code_X\Python314\python.exe'
$checks = New-Object System.Collections.Generic.List[object]

function Add-Check([string]$id, [string]$label, [string]$status, [string]$detail = '', [string]$repair = '') {
    $normalized = ([string]$status).ToUpperInvariant()
    $row = [pscustomobject][ordered]@{
        id = $id
        label = $label
        status = $normalized
        detail = $detail
        repair = $repair
    }
    $checks.Add($row) | Out-Null
    if (-not $Json.IsPresent) {
        $text = if ($detail) { "[$normalized] $label - $detail" } else { "[$normalized] $label" }
        Write-Host $text
    }
    return $row
}

function ConvertTo-ProcessArgument([string]$value) {
    if ($null -eq $value -or $value.Length -eq 0) { return '""' }
    if ($value -notmatch '[\s"]') { return $value }
    $builder = New-Object System.Text.StringBuilder
    [void]$builder.Append('"')
    $slashes = 0
    foreach ($character in $value.ToCharArray()) {
        if ($character -eq '\') {
            $slashes += 1
            continue
        }
        if ($character -eq '"') {
            [void]$builder.Append(('\' * ($slashes * 2 + 1)))
            [void]$builder.Append('"')
            $slashes = 0
            continue
        }
        if ($slashes -gt 0) {
            [void]$builder.Append(('\' * $slashes))
            $slashes = 0
        }
        [void]$builder.Append($character)
    }
    if ($slashes -gt 0) { [void]$builder.Append(('\' * ($slashes * 2))) }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Invoke-CapturedProcess(
    [string]$filePath,
    [string[]]$arguments,
    [int]$timeoutSeconds = 60,
    [string]$workingDirectory = ''
) {
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $filePath
    $startInfo.Arguments = (($arguments | ForEach-Object { ConvertTo-ProcessArgument ([string]$_) }) -join ' ')
    $startInfo.WorkingDirectory = if ($workingDirectory) { $workingDirectory } else { $scriptRoot }
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    if ($startInfo.PSObject.Properties['StandardOutputEncoding']) {
        $utf8 = New-Object System.Text.UTF8Encoding($false)
        $startInfo.StandardOutputEncoding = $utf8
        $startInfo.StandardErrorEncoding = $utf8
    }
    $pythonEnvironmentKeys = @(
        $startInfo.EnvironmentVariables.Keys |
            Where-Object { ([string]$_).StartsWith('PYTHON', [System.StringComparison]::OrdinalIgnoreCase) }
    )
    foreach ($variableName in $pythonEnvironmentKeys) {
        [void]$startInfo.EnvironmentVariables.Remove([string]$variableName)
    }
    $startInfo.EnvironmentVariables['PYTHONUTF8'] = '1'
    $startInfo.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'
    $startInfo.EnvironmentVariables['PYTHONDONTWRITEBYTECODE'] = '1'
    $startInfo.EnvironmentVariables['PYTHONNOUSERSITE'] = '1'
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) { throw "Process did not start: $filePath" }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit([Math]::Max(1, $timeoutSeconds) * 1000)) {
            try { $process.Kill() } catch {}
            throw "Process timed out after $timeoutSeconds seconds: $filePath"
        }
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()
        return [pscustomobject]@{
            ExitCode = $process.ExitCode
            Stdout = [string]$stdout
            Stderr = [string]$stderr
        }
    }
    finally {
        $process.Dispose()
    }
}

function Test-WritableDirectory([string]$path) {
    New-Item -ItemType Directory -Path $path -Force | Out-Null
    $probe = Join-Path $path ('.system_probe_' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [System.IO.File]::WriteAllText($probe, 'ok', (New-Object System.Text.UTF8Encoding($false)))
        return (Test-Path -LiteralPath $probe -PathType Leaf)
    }
    finally {
        Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
    }
}

function Add-PythonCandidate(
    [System.Collections.Generic.List[object]]$list,
    [string]$path,
    [string]$source,
    [int]$priority
) {
    $raw = [Environment]::ExpandEnvironmentVariables([string]$path).Trim().Trim('"')
    if (-not $raw) { return }
    try { $full = [System.IO.Path]::GetFullPath($raw) } catch { return }
    if (-not (Test-Path -LiteralPath $full -PathType Leaf)) { return }
    if (@($list | Where-Object { $_.Path.Equals($full, [System.StringComparison]::OrdinalIgnoreCase) }).Count -eq 0) {
        $list.Add([pscustomobject][ordered]@{
            Path = $full
            Source = $source
            Priority = $priority
        }) | Out-Null
    }
}

function Get-ApprovedPythonRuntimePaths {
    $result = New-Object System.Collections.Generic.List[string]
    $runtimeRoot = Join-Path $localAppDataRoot 'CodexV4\RE6'
    if (-not (Test-Path -LiteralPath $runtimeRoot -PathType Container)) { return @() }
    foreach ($releaseRoot in @(Get-ChildItem -LiteralPath $runtimeRoot -Directory -ErrorAction SilentlyContinue)) {
        $statePath = Join-Path $releaseRoot.FullName 'state\bootstrap.json'
        if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) { continue }
        try {
            $state = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
            $runtimeAB = $state.python_runtime_ab
            if ($null -eq $runtimeAB -or [string]$runtimeAB.schema -ne 'pc-rehd-code-x-python-runtime-ab-v1') { continue }
            $recordedRelease = [string]$runtimeAB.release_root
            if ($recordedRelease) {
                try {
                    if (-not [System.IO.Path]::GetFullPath($recordedRelease).Equals($scriptRoot, [System.StringComparison]::OrdinalIgnoreCase)) { continue }
                }
                catch { continue }
            }
            $active = $runtimeAB.active
            if ($null -eq $active -or ([string]$active.status).ToLowerInvariant() -ne 'approved') { continue }
            $path = [string]$active.python_exe
            if ($path -and (Test-Path -LiteralPath $path -PathType Leaf) -and -not $result.Contains($path)) {
                $result.Add([System.IO.Path]::GetFullPath($path)) | Out-Null
            }
        }
        catch {}
    }
    return @($result | ForEach-Object { $_ })
}

function Get-PythonCandidates {
    $list = New-Object System.Collections.Generic.List[object]
    foreach ($approvedPath in Get-ApprovedPythonRuntimePaths) {
        Add-PythonCandidate $list $approvedPath 'bootstrap-approved-active' -100
    }
    Add-PythonCandidate $list $managedBaselinePython 'managed-baseline' -50
    # The release-owned runtime is known to match the bundled cp314 payloads.
    # Prefer it to unrelated machine-wide installs, while preserving an
    # explicitly approved A/B runtime and an already managed baseline.
    Add-PythonCandidate $list (Join-Path $scriptRoot 'Python\pythoncore-3.14-64\python.exe') 'project-runtime' -40
    Add-PythonCandidate $list (Join-Path $scriptRoot 'Python\pythoncore-3.12-64\python.exe') 'project-runtime' -39
    Add-PythonCandidate $list (Join-Path $scriptRoot 'Python\bin\python.exe') 'project-runtime' -38
    Add-PythonCandidate $list ([Environment]::GetEnvironmentVariable('PC_REHD_CODE_X_PYTHON', 'Process')) 'explicit-process' 0
    Add-PythonCandidate $list ([Environment]::GetEnvironmentVariable('PC_REHD_CODE_X_PYTHON', 'User')) 'explicit-user' 1
    Add-PythonCandidate $list ([Environment]::GetEnvironmentVariable('CODEX_PRIMARY_PYTHON', 'Process')) 'explicit-legacy' 2
    Add-PythonCandidate $list (Join-Path $localAppDataRoot 'Programs\PC_REHD_Code_X\Python312\python.exe') 'managed-install' 11
    Add-PythonCandidate $list (Join-Path $localAppDataRoot 'Programs\Python\Python314\python.exe') 'user-install' 12
    Add-PythonCandidate $list (Join-Path $localAppDataRoot 'Programs\Python\Python312\python.exe') 'user-install' 13
    Add-PythonCandidate $list (Join-Path $localAppDataRoot 'Python\pythoncore-3.14-64\python.exe') 'user-runtime' 14
    Add-PythonCandidate $list (Join-Path $localAppDataRoot 'Python\pythoncore-3.12-64\python.exe') 'user-runtime' 15
    foreach ($root in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if (-not $root) { continue }
        Add-PythonCandidate $list (Join-Path $root 'Python314\python.exe') 'system-install' 16
        Add-PythonCandidate $list (Join-Path $root 'Python312\python.exe') 'system-install' 17
    }
    foreach ($hive in @('HKCU:', 'HKLM:')) {
        foreach ($minor in @(14, 12)) {
            $installKey = Join-Path $hive ("SOFTWARE\Python\PythonCore\3.$minor\InstallPath")
            try {
                $properties = Get-ItemProperty -LiteralPath $installKey -ErrorAction Stop
                Add-PythonCandidate $list ([string]$properties.ExecutablePath) 'registry-install' 18
                Add-PythonCandidate $list (Join-Path ([string]$properties.'(default)') 'python.exe') 'registry-install' 18
            }
            catch {}
        }
    }
    foreach ($command in @(Get-Command python.exe -All -ErrorAction SilentlyContinue)) {
        Add-PythonCandidate $list ([string]$command.Source) 'path' 20
    }
    return @($list | ForEach-Object { $_ })
}

function Get-PythonInfo([object]$candidate) {
    $path = [string]$candidate.Path
    try {
        $code = "import struct,sys;print(str(sys.version_info.major)+'|'+str(sys.version_info.minor)+'|'+str(struct.calcsize('P')*8)+'|'+sys.executable)"
        $probe = Invoke-CapturedProcess $path @('-I', '-B', '-c', $code) 15 $scriptRoot
        $line = ([string]($probe.Stdout -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -Last 1)).Trim()
        $parts = $line -split '\|', 4
        if ($probe.ExitCode -ne 0 -or $parts.Count -ne 4) { return $null }
        $major = 0
        $minor = 0
        $bits = 0
        if (-not [int]::TryParse($parts[0], [ref]$major)) { return $null }
        if (-not [int]::TryParse($parts[1], [ref]$minor)) { return $null }
        if (-not [int]::TryParse($parts[2], [ref]$bits)) { return $null }
        if ($major -lt 3 -or $bits -ne 64) { return $null }
        return [pscustomobject]@{
            # The candidate path has already launched successfully. Do not replace it
            # with sys.executable, which can be mojibake on embedded Python in a CJK path.
            Path = [System.IO.Path]::GetFullPath($path)
            ReportedPath = [string]$parts[3]
            Source = [string]$candidate.Source
            Major = $major
            Minor = $minor
            Bits = $bits
            VersionRank = -(($major * 1000) + $minor)
            Priority = [int]$candidate.Priority
        }
    }
    catch { return $null }
}

function Find-HealthyPythons {
    $healthy = @()
    foreach ($candidate in Get-PythonCandidates) {
        $info = Get-PythonInfo $candidate
        if ($null -ne $info) { $healthy += $info }
    }
    return @($healthy | Sort-Object Priority, VersionRank, Path)
}

function Get-BundledPythonInstaller {
    $installer = Get-ChildItem -LiteralPath $scriptRoot -Filter 'python-3.14*-amd64.exe' -File -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending | Select-Object -First 1
    return $installer
}

function Invoke-BundledPythonInstaller([bool]$repair) {
    $installer = Get-BundledPythonInstaller
    if ($null -eq $installer) {
        return [pscustomobject]@{ Action = $(if ($repair) { 'bundled-repair' } else { 'bundled-install' }); Success = $false; Detail = 'Bundled Python 3.14 installer is missing.' }
    }
    $target = Join-Path $localAppDataRoot 'Programs\PC_REHD_Code_X\Python314'
    New-Item -ItemType Directory -Path (Split-Path -Path $target -Parent) -Force | Out-Null
    if (-not $Json.IsPresent) {
        $verb = if ($repair) { 'Repairing' } else { 'Installing' }
        Write-Host ("[REPAIR] $verb bundled Python 3.14.6 - this is only used when every existing runtime failed")
    }
    $arguments = if ($repair) {
        @('/quiet', '/repair', '/norestart')
    }
    else {
        @(
            '/quiet', '/norestart', 'InstallAllUsers=0', ('TargetDir=' + $target), 'PrependPath=0',
            'Include_launcher=0', 'Include_test=0', 'Include_doc=0', 'Include_debug=0',
            'Include_symbols=0', 'Shortcuts=0', 'Include_pip=1', 'Include_tcltk=1'
        )
    }
    try {
        $result = Invoke-CapturedProcess $installer.FullName $arguments 900 $scriptRoot
        $accepted = $result.ExitCode -in @(0, 1641, 3010)
        $detail = "exit=$($result.ExitCode) installer=$($installer.Name)"
        if (-not $accepted -and $result.Stderr.Trim()) { $detail += ' stderr=' + $result.Stderr.Trim() }
        return [pscustomobject]@{
            Action = $(if ($repair) { 'bundled-repair' } else { 'bundled-install' })
            Success = $accepted
            Detail = $detail
        }
    }
    catch {
        return [pscustomobject]@{
            Action = $(if ($repair) { 'bundled-repair' } else { 'bundled-install' })
            Success = $false
            Detail = $_.Exception.Message
        }
    }
}

function Invoke-WingetPythonInstall([int]$minor) {
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $winget) {
        return [pscustomobject]@{ Action = "winget-python-3.$minor"; Success = $false; Detail = 'winget.exe is unavailable.' }
    }
    $packageId = "Python.Python.3.$minor"
    if (-not $Json.IsPresent) {
        Write-Host ("[REPAIR] Trying winget recovery for Python 3.$minor")
    }
    $arguments = @(
        'install', '--id', $packageId, '--exact', '--scope', 'user', '--silent',
        '--accept-package-agreements', '--accept-source-agreements', '--disable-interactivity'
    )
    try {
        $result = Invoke-CapturedProcess ([string]$winget.Source) $arguments 1800 $scriptRoot
        $accepted = $result.ExitCode -eq 0
        $detail = "exit=$($result.ExitCode) package=$packageId"
        if (-not $accepted) {
            $tail = ([string]($result.Stderr + "`n" + $result.Stdout)).Trim()
            if ($tail.Length -gt 1200) { $tail = $tail.Substring($tail.Length - 1200) }
            if ($tail) { $detail += ' output=' + $tail }
        }
        return [pscustomobject]@{ Action = "winget-python-3.$minor"; Success = $accepted; Detail = $detail }
    }
    catch {
        return [pscustomobject]@{ Action = "winget-python-3.$minor"; Success = $false; Detail = $_.Exception.Message }
    }
}

function Invoke-BootstrapCandidate([object]$pythonInfo, [string]$mode) {
    try {
        if (-not $Json.IsPresent) {
            Write-Host ("[CHECK] Bootstrap runtime - Python $($pythonInfo.Major).$($pythonInfo.Minor) [$($pythonInfo.Source)]")
        }
        $result = Invoke-CapturedProcess $pythonInfo.Path @('-I', '-B', $bootstrapPath, $mode, '--json') 180 $scriptRoot
        $payload = ConvertFrom-LastJsonLine $result.Stdout
        $overall = if ($null -eq $payload) { 'MISSING' } else { ([string]$payload.status).ToUpperInvariant() }
        $ready = ($overall -eq 'PASS' -and $result.ExitCode -eq 0) -or
            ($overall -eq 'DEGRADED' -and $result.ExitCode -eq 2)
        $classification = if ($ready) {
            [pscustomobject]@{ Class = 'ready'; RuntimeRecoveryRecommended = $false }
        }
        else {
            Get-BootstrapFailureClassification $payload
        }
        $detail = "Python $($pythonInfo.Major).$($pythonInfo.Minor) [$($pythonInfo.Source)] exit=$($result.ExitCode) status=$overall"
        if (-not $ready) {
            $reason = if ($null -eq $payload) { [string]$result.Stderr } else { ([string]$payload.error + ' ' + [string]$result.Stderr).Trim() }
            if ($reason.Length -gt 1200) { $reason = $reason.Substring($reason.Length - 1200) }
            if ($reason) { $detail += ' detail=' + $reason }
        }
        return [pscustomobject]@{
            Ready = $ready
            Python = $pythonInfo
            Result = $result
            Payload = $payload
            Detail = $detail
            FailureClass = $classification.Class
            RuntimeRecoveryRecommended = [bool]$classification.RuntimeRecoveryRecommended
        }
    }
    catch {
        return [pscustomobject]@{
            Ready = $false
            Python = $pythonInfo
            Result = $null
            Payload = $null
            Detail = "Python $($pythonInfo.Major).$($pythonInfo.Minor) [$($pythonInfo.Source)] could not run Bootstrap: $($_.Exception.Message)"
            FailureClass = 'bootstrap-protocol'
            RuntimeRecoveryRecommended = $true
        }
    }
}

function Find-ReadyPythonBootstrap([string]$mode, [System.Collections.Generic.List[object]]$attempts) {
    $lastAttempt = $null
    foreach ($pythonInfo in Find-HealthyPythons) {
        $attempt = Invoke-BootstrapCandidate $pythonInfo $mode
        $lastAttempt = $attempt
        $attempts.Add([pscustomobject]@{
            action = 'bootstrap'
            python = $pythonInfo.Path
            version = "$($pythonInfo.Major).$($pythonInfo.Minor)"
            source = $pythonInfo.Source
            success = [bool]$attempt.Ready
            failure_class = [string]$attempt.FailureClass
            runtime_recovery_recommended = [bool]$attempt.RuntimeRecoveryRecommended
            detail = $attempt.Detail
        }) | Out-Null
        if ($attempt.Ready) { return $attempt }
    }
    return $lastAttempt
}

function ConvertFrom-LastJsonLine([string]$text) {
    $complete = ([string]$text).Trim()
    if ($complete) {
        try {
            $payload = $complete | ConvertFrom-Json -ErrorAction Stop
            if ($null -ne $payload -and $null -ne $payload.PSObject.Properties['status']) { return $payload }
        }
        catch {}
    }
    $lines = @($text -split "`r?`n" | Where-Object { $_.Trim() })
    for ($index = $lines.Count - 1; $index -ge 0; $index--) {
        try {
            $payload = $lines[$index] | ConvertFrom-Json -ErrorAction Stop
            if ($null -ne $payload -and $null -ne $payload.PSObject.Properties['status']) { return $payload }
        }
        catch {}
    }
    return $null
}

function Get-BootstrapFailureClassification([object]$payload) {
    if ($null -eq $payload) {
        return [pscustomobject]@{ Class = 'bootstrap-protocol'; RuntimeRecoveryRecommended = $true }
    }
    $rows = @($payload.checks)
    $runtimeRow = $rows | Where-Object { $_.id -eq 'bootstrap_python' } | Select-Object -First 1
    $dependencyRow = $rows | Where-Object { $_.id -eq 'python_dependencies' } | Select-Object -First 1
    $sourceRow = $rows | Where-Object { $_.id -eq 'python_sources' } | Select-Object -First 1
    if ($null -ne $runtimeRow -and ([string]$runtimeRow.status).ToUpperInvariant() -eq 'FAIL') {
        return [pscustomobject]@{ Class = 'python-runtime'; RuntimeRecoveryRecommended = $true }
    }
    if ($null -ne $dependencyRow -and ([string]$dependencyRow.status).ToUpperInvariant() -eq 'FAIL') {
        return [pscustomobject]@{ Class = 'python-dependencies'; RuntimeRecoveryRecommended = $true }
    }
    if ($null -ne $sourceRow -and ([string]$sourceRow.status).ToUpperInvariant() -eq 'FAIL') {
        return [pscustomobject]@{ Class = 'python-source'; RuntimeRecoveryRecommended = $false }
    }
    return [pscustomobject]@{ Class = 'component-contract'; RuntimeRecoveryRecommended = $false }
}

function Test-RuntimeRecoveryRecommended([System.Collections.Generic.List[object]]$attempts) {
    $bootstrapRows = @($attempts | Where-Object { $_.action -eq 'bootstrap' })
    if ($bootstrapRows.Count -eq 0) { return $true }
    if (@($bootstrapRows | Where-Object { $_.failure_class -in @('python-source', 'component-contract') }).Count -gt 0) {
        return $false
    }
    return @($bootstrapRows | Where-Object { $_.runtime_recovery_recommended -eq $true }).Count -gt 0
}

$exitCode = 1
try {
    Add-Check 'powershell' 'PowerShell host' 'PASS' ($PSVersionTable.PSEdition + ' ' + $PSVersionTable.PSVersion) | Out-Null

    $requiredSources = @(
        'codex_python_runtime_bootstrap.py',
        'codex_python_export_bridge.py',
        'codex_re6_mod_import_fbx.py',
        'codex_fbx_probe.py',
        'codex_re6_tex_decode.py',
        'codex_re6_auxiliary_max_bridge.py',
        'PC-REHD Code X Launcher.py'
    )
    $missingSources = @($requiredSources | Where-Object { -not (Test-Path -LiteralPath (Join-Path $scriptRoot $_) -PathType Leaf) })
    if ($missingSources.Count -gt 0) {
        Add-Check 'sources' 'Python source set' 'FAIL' ('Missing: ' + ($missingSources -join ', ')) | Out-Null
        throw 'Required Python source files are missing.'
    }
    Add-Check 'sources' 'Python source set' 'PASS' ($requiredSources.Count.ToString() + ' required files') | Out-Null

    if (-not (Test-WritableDirectory $systemRoot)) { throw "State directory is not writable: $systemRoot" }
    Add-Check 'state_directory' 'System state directory' 'PASS' $systemRoot | Out-Null
    if (-not (Test-WritableDirectory $temporaryRoot)) { throw "Temporary directory is not writable: $temporaryRoot" }
    Add-Check 'temporary_directory' 'Temporary work directory' 'PASS' $temporaryRoot | Out-Null

    if (-not $Json.IsPresent) {
        Write-Host ('[CHECK] Python modules - ' + $(if ($CheckOnly.IsPresent) { 'health check' } else { 'first initialization and safe repair' }))
    }
    $bootstrapMode = if ($CheckOnly.IsPresent) { '--system-check' } else { '--system-initialize' }
    $runtimeAttempts = New-Object System.Collections.Generic.List[object]
    $recoveryUsed = $false
    # Try every already available runtime before changing the machine. The
    # recovery loop below installs Python only when all of them actually fail.
    $bootstrapAttempt = Find-ReadyPythonBootstrap $bootstrapMode $runtimeAttempts
    $bootstrapReady = $null -ne $bootstrapAttempt -and $bootstrapAttempt.Ready -eq $true

    if (-not $bootstrapReady -and -not $CheckOnly.IsPresent -and (Test-RuntimeRecoveryRecommended $runtimeAttempts)) {
        foreach ($repairStep in @(
            { Invoke-BundledPythonInstaller $false },
            { Invoke-BundledPythonInstaller $true },
            { Invoke-WingetPythonInstall 14 },
            { Invoke-WingetPythonInstall 12 }
        )) {
            $recoveryUsed = $true
            $repairResult = & $repairStep
            $runtimeAttempts.Add([pscustomobject]@{
                action = $repairResult.Action
                python = ''
                version = ''
                source = 'system-recovery'
                success = [bool]$repairResult.Success
                detail = [string]$repairResult.Detail
            }) | Out-Null
            $bootstrapAttempt = Find-ReadyPythonBootstrap $bootstrapMode $runtimeAttempts
            $bootstrapReady = $null -ne $bootstrapAttempt -and $bootstrapAttempt.Ready -eq $true
            if ($bootstrapReady) { break }
            if (-not (Test-RuntimeRecoveryRecommended $runtimeAttempts)) { break }
        }
    }

    if (-not $bootstrapReady) {
        $healthyRuntimes = @(Find-HealthyPythons)
        $failureTail = @($runtimeAttempts | Select-Object -Last 8 | ForEach-Object { $_.action + ': ' + $_.detail }) -join ' | '
        if ($failureTail.Length -gt 4000) { $failureTail = $failureTail.Substring($failureTail.Length - 4000) }
        if ($healthyRuntimes.Count -eq 0) {
            if ($CheckOnly.IsPresent) {
                Add-Check 'python_runtime' 'Python x64 runtime' 'FAIL' 'No supported runtime was found; CheckOnly did not modify the system.' 'Run the BAT without -CheckOnly to execute bundled install, bundled repair, and winget recovery.' | Out-Null
                throw 'No supported Python runtime is available. Run the BAT without -CheckOnly to execute automatic recovery.'
            }
            Add-Check 'python_runtime' 'Python x64 runtime' 'FAIL' 'All local, bundled-install, bundled-repair, and winget recovery paths failed.' $failureTail | Out-Null
            throw ('No supported Python runtime could be started after all recovery plans. ' + $failureTail).Trim()
        }
        $failedPython = $bootstrapAttempt.Python
        $pythonStatus = if ($recoveryUsed) { 'REPAIRED' } else { 'PASS' }
        Add-Check 'python_runtime' 'Python x64 runtime' $pythonStatus ("Python $($failedPython.Major).$($failedPython.Minor) x64 starts correctly | $($failedPython.Path)") | Out-Null
        if ($null -ne $bootstrapAttempt.Payload) {
            foreach ($row in @($bootstrapAttempt.Payload.checks)) {
                Add-Check ([string]$row.id) ([string]$row.label) ([string]$row.status) ([string]$row.detail) ([string]$row.repair) | Out-Null
            }
            $componentError = ([string]$bootstrapAttempt.Payload.error + ' ' + [string]$bootstrapAttempt.Result.Stderr).Trim()
            throw ("Python is healthy; a Python component contract failed [$($bootstrapAttempt.FailureClass)]. " + $componentError).Trim()
        }
        Add-Check 'bootstrap_contract' 'Python Bootstrap contract' 'FAIL' 'Python starts, but Bootstrap returned no valid status contract.' $failureTail | Out-Null
        throw ('Python starts, but Bootstrap could not return a valid contract. ' + $failureTail).Trim()
    }

    $pythonInfo = $bootstrapAttempt.Python
    $bootstrapResult = $bootstrapAttempt.Result
    $payload = $bootstrapAttempt.Payload
    $failedBootstrapAttempts = @($runtimeAttempts | Where-Object { $_.action -eq 'bootstrap' -and $_.success -ne $true }).Count
    $pythonStatus = if ($recoveryUsed -or $failedBootstrapAttempts -gt 0) { 'REPAIRED' } else { 'PASS' }
    $pythonDetail = "Python $($pythonInfo.Major).$($pythonInfo.Minor) x64 [$($pythonInfo.Source)] | $($pythonInfo.Path)"
    if ($failedBootstrapAttempts -gt 0) { $pythonDetail += " | fallback after $failedBootstrapAttempts failed runtime contract(s)" }
    Add-Check 'python_runtime' 'Python x64 runtime' $pythonStatus $pythonDetail | Out-Null

    foreach ($row in @($payload.checks)) {
        Add-Check ([string]$row.id) ([string]$row.label) ([string]$row.status) ([string]$row.detail) ([string]$row.repair) | Out-Null
    }
    $overall = ([string]$payload.status).ToUpperInvariant()
    if (($overall -eq 'PASS' -and $bootstrapResult.ExitCode -ne 0) -or
        ($overall -eq 'DEGRADED' -and $bootstrapResult.ExitCode -ne 2) -or
        $overall -notin @('PASS', 'DEGRADED')) {
        throw ([string]$payload.error + ' ' + [string]$bootstrapResult.Stderr).Trim()
    }
    if ($overall -eq 'DEGRADED') {
        Add-Check 'system_result' 'PC-REHD Code X system readiness' 'WARN' 'Core MOD functions are ready; at least one optional capability is unavailable.' | Out-Null
        $exitCode = 2
    }
    else {
        Add-Check 'system_result' 'PC-REHD Code X system readiness' 'PASS' 'Initialization and module health checks completed.' | Out-Null
        $exitCode = 0
    }

    if ($Json.IsPresent) {
        [pscustomobject][ordered]@{
            schema = 'pc-rehd-code-x-system-detector-v1'
            status = $overall
            check_only = $CheckOnly.IsPresent
            python = $pythonInfo
            runtime_attempts = @($runtimeAttempts | ForEach-Object { $_ })
            directories = $payload.directories
            checks = @($checks | ForEach-Object { $_ })
            bootstrap = $payload
        } | ConvertTo-Json -Depth 16 -Compress
    }
}
catch {
    $message = [string]$_.Exception.Message
    if (@($checks | Where-Object { $_.id -eq 'system_result' }).Count -eq 0) {
        Add-Check 'system_result' 'PC-REHD Code X system readiness' 'FAIL' $message | Out-Null
    }
    if ($Json.IsPresent) {
        [pscustomobject][ordered]@{
            schema = 'pc-rehd-code-x-system-detector-v1'
            status = 'FAIL'
            check_only = $CheckOnly.IsPresent
            checks = @($checks | ForEach-Object { $_ })
            error = $message
        } | ConvertTo-Json -Depth 12 -Compress
    }
    $exitCode = 1
}

exit $exitCode
