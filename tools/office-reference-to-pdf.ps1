[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InputFile,

    [Parameter(Mandatory = $true)]
    [string]$OutPdf,

    [Parameter(Mandatory = $true)]
    [string]$PidFile
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Release-ComObject {
    param([object]$ComObject)

    if (
        $null -ne $ComObject -and
        [System.Runtime.InteropServices.Marshal]::IsComObject($ComObject)
    ) {
        [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject(
            $ComObject
        )
    }
}

function Assert-ValidPdf {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw 'Office visual export did not create a PDF.'
    }
    $file = Get-Item -LiteralPath $Path
    if ($file.Length -le 5) {
        throw 'Office visual export created an empty or truncated PDF.'
    }
    $stream = $null
    try {
        $stream = [System.IO.File]::OpenRead($Path)
        $signatureBytes = New-Object byte[] 5
        $read = $stream.Read($signatureBytes, 0, $signatureBytes.Length)
        $signature = [System.Text.Encoding]::ASCII.GetString(
            $signatureBytes,
            0,
            $read
        )
        if (-not $signature.Equals('%PDF-', [System.StringComparison]::Ordinal)) {
            throw 'Office visual export did not produce a valid PDF signature.'
        }
    }
    finally {
        if ($null -ne $stream) {
            $stream.Dispose()
        }
    }
}

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class OgentReferenceWindow {
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern IntPtr FindWindow(string className, string windowName);

    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
}
'@

function Write-TrackedProcess {
    param(
        [Parameter(Mandatory = $true)][object]$Application,
        [Parameter(Mandatory = $true)][string]$Extension,
        [Parameter(Mandatory = $true)][string]$Path,
        [int[]]$ExistingProcessIds = @()
    )

    $window = [IntPtr]::Zero
    $expectedName = ''
    switch ($Extension) {
        '.docx' {
            $window = [IntPtr]$Application.Hwnd
            $expectedName = 'WINWORD.EXE'
        }
        '.pptx' {
            $window = [IntPtr]$Application.HWND
            $expectedName = 'POWERPNT.EXE'
        }
        '.xlsx' {
            $window = [IntPtr]$Application.Hwnd
            $expectedName = 'EXCEL.EXE'
        }
        default {
            throw 'Unsupported Office reference type.'
        }
    }
    if ($window -eq [IntPtr]::Zero) {
        throw 'Could not identify the Office automation window.'
    }
    $processId = [uint32]0
    [void][OgentReferenceWindow]::GetWindowThreadProcessId(
        $window,
        [ref]$processId
    )
    if ($processId -eq 0) {
        throw 'Could not identify the Office automation process.'
    }
    if ($processId -in $ExistingProcessIds) {
        throw (
            'Microsoft Office reused a pre-existing application process; ' +
            'native visual export was refused to protect the open application.'
        )
    }
    $process = Get-Process -Id $processId -ErrorAction Stop
    $actualName = $process.ProcessName + '.EXE'
    if (
        -not $actualName.Equals(
            $expectedName,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    ) {
        throw 'The tracked Office process did not match the expected application.'
    }
    [pscustomobject]@{
        pid = [int]$processId
        process_name = $expectedName
    } | ConvertTo-Json -Compress | Set-Content -LiteralPath $Path -Encoding utf8NoBOM
    return [int]$processId
}

function Stop-TrackedProcess {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return
    }
    $removeRecord = $false
    try {
        $record = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
        $processId = [int]$record.pid
        $expectedName = [string]$record.process_name
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$processId"
        if ($null -eq $process) {
            $removeRecord = $true
        }
        elseif (
            $null -ne $process -and
            $process.Name.Equals(
                $expectedName,
                [System.StringComparison]::OrdinalIgnoreCase
            ) -and
            $process.CommandLine -match '(?i)(/Automation|-Embedding)'
        ) {
            Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
            try {
                Wait-Process -Id $processId -Timeout 5 -ErrorAction Stop
            }
            catch {
                if ($null -ne (Get-Process -Id $processId -ErrorAction SilentlyContinue)) {
                    throw 'The tracked Office automation process did not exit.'
                }
            }
            $removeRecord = $true
        }
        else {
            throw (
                'The tracked Office process no longer matches its safe ' +
                'automation identity; its cleanup record was retained.'
            )
        }
    }
    finally {
        if ($removeRecord) {
            Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
        }
    }
}

function Invoke-OfficeExport {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string]$Extension,
        [Parameter(Mandatory = $true)][string]$TrackingFile
    )

    $application = $null
    $document = $null
    $quitCleanly = $false
    $ownsApplication = $false
    $processBaseName = switch ($Extension) {
        '.docx' { 'WINWORD' }
        '.pptx' { 'POWERPNT' }
        '.xlsx' { 'EXCEL' }
    }
    $existingProcessIds = @(
        Get-Process -Name $processBaseName -ErrorAction SilentlyContinue |
            ForEach-Object { [int]$_.Id }
    )
    try {
        switch ($Extension) {
            '.docx' {
                $application = New-Object -ComObject Word.Application
                [void](Write-TrackedProcess `
                    -Application $application `
                    -Extension $Extension `
                    -Path $TrackingFile `
                    -ExistingProcessIds $existingProcessIds)
                $ownsApplication = $true
                $application.Visible = $false
                $application.DisplayAlerts = 0
                $document = $application.Documents.Open($Source, $false, $true)
                $document.ExportAsFixedFormat($Destination, 17)
            }
            '.pptx' {
                $application = New-Object -ComObject PowerPoint.Application
                [void](Write-TrackedProcess `
                    -Application $application `
                    -Extension $Extension `
                    -Path $TrackingFile `
                    -ExistingProcessIds $existingProcessIds)
                $ownsApplication = $true
                # ReadOnly=-1, Untitled=0, WithWindow=0
                $document = $application.Presentations.Open(
                    $Source,
                    -1,
                    0,
                    0
                )
                $document.ExportAsFixedFormat($Destination, 2)
            }
            '.xlsx' {
                $application = New-Object -ComObject Excel.Application
                [void](Write-TrackedProcess `
                    -Application $application `
                    -Extension $Extension `
                    -Path $TrackingFile `
                    -ExistingProcessIds $existingProcessIds)
                $ownsApplication = $true
                $application.Visible = $false
                $application.DisplayAlerts = $false
                $document = $application.Workbooks.Open($Source, 0, $true)
                $document.ExportAsFixedFormat(0, $Destination)
            }
            default {
                throw 'Unsupported Office reference type.'
            }
        }
    }
    finally {
        if ($null -ne $document) {
            try {
                switch ($Extension) {
                    '.docx' { $document.Close($false) }
                    '.pptx' { $document.Close() }
                    '.xlsx' { $document.Close($false) }
                }
            }
            catch {
                Write-Warning 'The read-only Office reference did not close cleanly.'
            }
            finally {
                Release-ComObject -ComObject $document
            }
        }
        if ($null -ne $application -and $ownsApplication) {
            try {
                $application.Quit()
                $quitCleanly = $true
            }
            catch {
                Write-Warning 'The Office automation application did not quit cleanly.'
            }
            finally {
                Release-ComObject -ComObject $application
            }
        }
        elseif ($null -ne $application) {
            Release-ComObject -ComObject $application
        }
        [System.GC]::Collect()
        [System.GC]::WaitForPendingFinalizers()
        if (
            $quitCleanly -and
            (Test-Path -LiteralPath $TrackingFile -PathType Leaf)
        ) {
            Remove-Item -LiteralPath $TrackingFile -Force
        }
    }
}

function Invoke-LibreOfficeExport {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string]$ScratchParent
    )

    $candidates = @(
        'C:\Program Files\LibreOffice\program\soffice.com',
        'C:\Program Files (x86)\LibreOffice\program\soffice.com'
    )
    $soffice = $candidates |
        Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
        Select-Object -First 1
    if ([string]::IsNullOrWhiteSpace($soffice)) {
        throw 'LibreOffice is not installed.'
    }
    $scratch = Join-Path `
        -Path $ScratchParent `
        -ChildPath ('.libreoffice-' + [System.Guid]::NewGuid().ToString('N'))
    $profile = Join-Path -Path $scratch -ChildPath 'profile'
    New-Item -ItemType Directory -Path $profile -Force | Out-Null
    $profileUri = ([System.Uri]$profile).AbsoluteUri
    try {
        $previousPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            $diagnostic = & $soffice `
                "-env:UserInstallation=$profileUri" `
                '--headless' `
                '--convert-to' 'pdf' `
                '--outdir' $scratch `
                $Source 2>&1
            $exitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previousPreference
        }
        $generatedName = (
            [System.IO.Path]::GetFileNameWithoutExtension($Source) + '.pdf'
        )
        $generated = Join-Path -Path $scratch -ChildPath $generatedName
        if (
            $exitCode -ne 0 -or
            -not (Test-Path -LiteralPath $generated -PathType Leaf)
        ) {
            $detail = ($diagnostic | Out-String).Trim()
            throw "LibreOffice export failed (exit $exitCode): $detail"
        }
        Move-Item -LiteralPath $generated -Destination $Destination -Force
    }
    finally {
        if (Test-Path -LiteralPath $scratch) {
            Remove-Item -LiteralPath $scratch -Recurse -Force
        }
    }
}

$resolvedInput = (Resolve-Path -LiteralPath $InputFile -ErrorAction Stop).ProviderPath
$resolvedOutput = [System.IO.Path]::GetFullPath($OutPdf)
$resolvedPidFile = [System.IO.Path]::GetFullPath($PidFile)
$extension = [System.IO.Path]::GetExtension($resolvedInput).ToLowerInvariant()
if ($extension -notin @('.docx', '.xlsx', '.pptx')) {
    throw 'The Office reference must be DOCX, XLSX, or PPTX.'
}
if (
    -not [System.IO.Path]::GetExtension($resolvedOutput).Equals(
        '.pdf',
        [System.StringComparison]::OrdinalIgnoreCase
    )
) {
    throw 'The Office visual output must use a PDF extension.'
}
$inputDirectory = [System.IO.Path]::GetDirectoryName($resolvedInput)
$outputDirectory = [System.IO.Path]::GetDirectoryName($resolvedOutput)
$pidDirectory = [System.IO.Path]::GetDirectoryName($resolvedPidFile)
$inputPrefix = $inputDirectory.TrimEnd('\') + '\'
if (
    -not ($outputDirectory.TrimEnd('\') + '\').StartsWith(
        $inputPrefix,
        [System.StringComparison]::OrdinalIgnoreCase
    ) -or
    -not $pidDirectory.Equals(
        $outputDirectory,
        [System.StringComparison]::OrdinalIgnoreCase
    )
) {
    throw 'Office reference output must stay inside its temporary attachment directory.'
}
New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
Remove-Item -LiteralPath $resolvedOutput -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $resolvedPidFile -Force -ErrorAction SilentlyContinue

$officeFailure = $null
try {
    Invoke-OfficeExport `
        -Source $resolvedInput `
        -Destination $resolvedOutput `
        -Extension $extension `
        -TrackingFile $resolvedPidFile
    Assert-ValidPdf -Path $resolvedOutput
}
catch {
    $officeFailure = $_.Exception.Message
    Remove-Item -LiteralPath $resolvedOutput -Force -ErrorAction SilentlyContinue
    Stop-TrackedProcess -Path $resolvedPidFile
    try {
        Invoke-LibreOfficeExport `
            -Source $resolvedInput `
            -Destination $resolvedOutput `
            -ScratchParent $outputDirectory
        Assert-ValidPdf -Path $resolvedOutput
    }
    catch {
        Remove-Item -LiteralPath $resolvedOutput -Force -ErrorAction SilentlyContinue
        throw (
            'Office visual export failed. Microsoft Office: ' +
            $officeFailure +
            ' LibreOffice: ' +
            $_.Exception.Message
        )
    }
}
finally {
    if (Test-Path -LiteralPath $resolvedPidFile -PathType Leaf) {
        Stop-TrackedProcess -Path $resolvedPidFile
    }
}

[pscustomobject]@{
    Engine = if ($null -eq $officeFailure) { 'Microsoft Office' } else { 'LibreOffice' }
    Bytes = (Get-Item -LiteralPath $resolvedOutput).Length
}
