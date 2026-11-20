#Requires -RunAsAdministrator
param([Parameter(Mandatory)][string]$Root)
$ErrorActionPreference = 'Stop'
$installRoot = (Get-Item -LiteralPath $Root).FullName
foreach ($relative in 'runtime\python.exe','runtime_character\blender.exe','SETUP_RECEIPT.json') {
    if (-not (Test-Path -LiteralPath (Join-Path $installRoot $relative) -PathType Leaf)) {
        throw "Missing installed component: $relative"
    }
}
if (Test-Path -LiteralPath (Join-Path $installRoot '.setup\git-sync.pending')) {
    throw 'Base setup is incomplete.'
}
$serviceName = 'RmrProbe' + [Guid]::NewGuid().ToString('N')
$probeDir = Join-Path $env:ProgramData ("RGBMoCapRemotePhase0\" + $serviceName)
$resultDir = Join-Path $probeDir 'result'
$null = New-Item -ItemType Directory -Path $resultDir
$serviceExe = Join-Path $probeDir ($serviceName + '.exe')
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'session_probe.py') -Destination $probeDir
$compiler = Join-Path $env:SystemRoot 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
& $compiler /nologo /target:exe /reference:System.ServiceProcess.dll "/out:$serviceExe" (Join-Path $PSScriptRoot 'SessionProbeService.cs')
if ($LASTEXITCODE -ne 0) { throw 'Probe service compilation failed.' }
# LocalService reads the probe and writes only its own result directory.
& icacls.exe $probeDir /grant '*S-1-5-19:(OI)(CI)RX' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Cannot grant probe read access.' }
& icacls.exe $resultDir /grant '*S-1-5-19:(OI)(CI)M' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Cannot grant probe result write access.' }
$created = Invoke-CimMethod -ClassName Win32_Service -MethodName Create -Arguments @{
    Name=$serviceName; DisplayName=$serviceName; PathName=('"' + $serviceExe + '"')
    ServiceType=[byte]16; ErrorControl=[byte]1; StartMode='Manual'; StartName='NT AUTHORITY\LocalService'
}
if ($created.ReturnValue -ne 0) { throw "Service create failed ($($created.ReturnValue))." }
try {
    $upstreamLock = [IO.File]::Open((Join-Path $installRoot '.setup\app.lock'), 'Open', 'ReadWrite', 'ReadWrite')
    $upstreamLock.Lock(0, 1)
    $service = [System.ServiceProcess.ServiceController]::new($serviceName)
    $service.Start([string[]]@($installRoot))
    $service.WaitForStatus('Stopped', [TimeSpan]::FromMinutes(8))
    $report = Join-Path $resultDir 'session_report.json'
    Write-Host "Probe evidence: $resultDir"
    $result = Get-Content -Raw -Encoding UTF8 -LiteralPath $report | ConvertFrom-Json
    $result | ConvertTo-Json -Depth 6
    if (-not $result.passed) { throw 'Session 0 probe failed. Inspect session_report.json and service.log.' }
} finally {
    if ($upstreamLock) { $upstreamLock.Dispose() }
    if ($service) { $service.Dispose() }
    $ownService = Get-CimInstance Win32_Service -Filter "Name='$serviceName'"
    if ($ownService) {
        $removed = Invoke-CimMethod -InputObject $ownService -MethodName Delete
        if ($removed.ReturnValue -ne 0) { Write-Warning "Cannot remove the temporary service ($($removed.ReturnValue))." }
    }
}
