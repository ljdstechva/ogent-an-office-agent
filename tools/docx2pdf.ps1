[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Docx,

    [Parameter(Mandatory = $true)]
    [string]$OutPdf,

    [ValidateSet('Auto', 'Word', 'LibreOffice')]
    [string]$Engine = 'Auto',

    [switch]$Force,

    [string]$WordPidFile
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

trap {
    [System.Console]::Error.WriteLine("ERROR: $($_.Exception.Message)")
    exit 1
}

function Get-AbsoluteOutputPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    if ([System.IO.Path]::IsPathRooted($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }

    return [System.IO.Path]::GetFullPath(
        (Join-Path -Path (Get-Location).Path -ChildPath $Path)
    )
}

function Assert-Extension {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedExtension,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $actual = [System.IO.Path]::GetExtension($Path)
    if (-not $actual.Equals($ExpectedExtension, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label must use the $ExpectedExtension extension: $Path"
    }
}

function Release-ComObject {
    param([object]$ComObject)

    if ($null -ne $ComObject -and [System.Runtime.InteropServices.Marshal]::IsComObject($ComObject)) {
        [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($ComObject)
    }
}

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class OgentWordWindow {
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern IntPtr FindWindow(string className, string windowName);

    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
}
'@

function Remove-GeneratedOutput {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        Remove-Item -LiteralPath $Path -Force
    }
}

function Assert-ValidPdf {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "PDF output was not created: $Path"
    }

    $file = Get-Item -LiteralPath $Path
    if ($file.Length -le 4) {
        throw "PDF output is empty or truncated: $Path"
    }

    $stream = $null
    try {
        $stream = [System.IO.File]::OpenRead($Path)
        $signatureBytes = New-Object byte[] 5
        $read = $stream.Read($signatureBytes, 0, $signatureBytes.Length)
        $signature = [System.Text.Encoding]::ASCII.GetString($signatureBytes, 0, $read)
        if (-not $signature.StartsWith('%PDF-', [System.StringComparison]::Ordinal)) {
            throw "Output does not have a valid PDF header: $Path"
        }
    }
    finally {
        if ($null -ne $stream) {
            $stream.Dispose()
        }
    }
}

function Invoke-WordDocxToPdf {
    param(
        [Parameter(Mandatory = $true)][string]$InputPath,
        [Parameter(Mandatory = $true)][string]$OutputPath,
        [string]$PidFilePath
    )

    $word = $null
    $document = $null
    $wordQuitCleanly = $false
    try {
        $word = New-Object -ComObject Word.Application
        if (-not [string]::IsNullOrWhiteSpace($PidFilePath)) {
            $resolvedPidFile = Get-AbsoluteOutputPath -Path $PidFilePath
            $wordCaption = 'OgentWordPid-' + [System.Guid]::NewGuid().ToString('N')
            $word.Caption = $wordCaption
            $wordWindow = [OgentWordWindow]::FindWindow('OpusApp', $wordCaption)
            if ($wordWindow -eq [IntPtr]::Zero) {
                throw 'Could not locate the Word automation window.'
            }
            $wordPid = [uint32]0
            [void][OgentWordWindow]::GetWindowThreadProcessId(
                $wordWindow,
                [ref]$wordPid
            )
            if ($wordPid -eq 0) {
                throw 'Could not determine the Word automation process id.'
            }
            [System.IO.File]::WriteAllText(
                $resolvedPidFile,
                $wordPid.ToString([System.Globalization.CultureInfo]::InvariantCulture)
            )
        }

        $word.Visible = $false
        $word.DisplayAlerts = 0
        $document = $word.Documents.Open($InputPath, $false, $true)
        $document.SaveAs2($OutputPath, 17) # wdFormatPDF
    }
    finally {
        if ($null -ne $document) {
            try {
                $document.Close($false)
            }
            catch {
                Write-Warning "Word could not close the source document cleanly: $($_.Exception.Message)"
            }
            finally {
                Release-ComObject -ComObject $document
            }
        }

        if ($null -ne $word) {
            try {
                $word.Quit()
                $wordQuitCleanly = $true
            }
            catch {
                Write-Warning "Word could not quit cleanly: $($_.Exception.Message)"
            }
            finally {
                Release-ComObject -ComObject $word
            }
        }

        [System.GC]::Collect()
        [System.GC]::WaitForPendingFinalizers()
        if (
            $wordQuitCleanly -and
            -not [string]::IsNullOrWhiteSpace($PidFilePath) -and
            (Test-Path -LiteralPath $PidFilePath -PathType Leaf)
        ) {
            Remove-Item -LiteralPath $PidFilePath -Force
        }
    }
}

function Invoke-LibreOfficeDocxToPdf {
    param(
        [Parameter(Mandatory = $true)][string]$InputPath,
        [Parameter(Mandatory = $true)][string]$OutputPath
    )

    # The .com launcher is the console twin of soffice.exe. PowerShell waits
    # for it and reliably populates $LASTEXITCODE for headless conversions.
    $soffice = 'C:\Program Files\LibreOffice\program\soffice.com'
    if (-not (Test-Path -LiteralPath $soffice -PathType Leaf)) {
        throw "LibreOffice was not found at $soffice"
    }

    $temporaryRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
    $temporaryDirectory = Join-Path -Path $temporaryRoot -ChildPath ("ogent-docx2pdf-" + [System.Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $temporaryDirectory | Out-Null
    $profileDirectory = Join-Path -Path $temporaryDirectory -ChildPath 'profile'
    $profileUri = ([System.Uri]$profileDirectory).AbsoluteUri

    try {
        $previousErrorActionPreference = $ErrorActionPreference
        try {
            # LibreOffice can write harmless startup warnings to stderr even
            # when conversion succeeds. Judge the native process by its exit
            # code and generated file instead of PowerShell's stderr wrapper.
            $ErrorActionPreference = 'Continue'
            $conversionOutput = & $soffice `
                "-env:UserInstallation=$profileUri" `
                '--headless' `
                '--convert-to' 'pdf' `
                '--outdir' $temporaryDirectory `
                $InputPath 2>&1
            $exitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }

        $generatedName = [System.IO.Path]::GetFileNameWithoutExtension($InputPath) + '.pdf'
        $generatedPath = Join-Path -Path $temporaryDirectory -ChildPath $generatedName
        if ($exitCode -ne 0 -or -not (Test-Path -LiteralPath $generatedPath -PathType Leaf)) {
            $details = ($conversionOutput | Out-String).Trim()
            if ([string]::IsNullOrWhiteSpace($details)) {
                $details = 'LibreOffice produced no diagnostic output.'
            }
            throw "LibreOffice DOCX export failed (exit $exitCode): $details"
        }

        Move-Item -LiteralPath $generatedPath -Destination $OutputPath -Force
    }
    finally {
        $resolvedTemporaryDirectory = [System.IO.Path]::GetFullPath($temporaryDirectory)
        if (
            $resolvedTemporaryDirectory.StartsWith($temporaryRoot, [System.StringComparison]::OrdinalIgnoreCase) -and
            (Test-Path -LiteralPath $resolvedTemporaryDirectory)
        ) {
            Remove-Item -LiteralPath $resolvedTemporaryDirectory -Recurse -Force
        }
    }
}

$resolvedDocx = (Resolve-Path -LiteralPath $Docx -ErrorAction Stop).ProviderPath
$resolvedOutPdf = Get-AbsoluteOutputPath -Path $OutPdf

Assert-Extension -Path $resolvedDocx -ExpectedExtension '.docx' -Label 'Input'
Assert-Extension -Path $resolvedOutPdf -ExpectedExtension '.pdf' -Label 'Output'

$outputDirectory = Split-Path -Path $resolvedOutPdf -Parent
if (-not (Test-Path -LiteralPath $outputDirectory -PathType Container)) {
    New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
}

if (Test-Path -LiteralPath $resolvedOutPdf) {
    if (-not $Force) {
        throw "Output already exists. Choose a new path or pass -Force: $resolvedOutPdf"
    }
    Remove-Item -LiteralPath $resolvedOutPdf -Force
}

$wordFailure = $null
if ($Engine -in @('Auto', 'Word')) {
    try {
        Invoke-WordDocxToPdf `
            -InputPath $resolvedDocx `
            -OutputPath $resolvedOutPdf `
            -PidFilePath $WordPidFile
        Assert-ValidPdf -Path $resolvedOutPdf

        [pscustomobject]@{
            Input = $resolvedDocx
            Output = $resolvedOutPdf
            Engine = 'Word COM'
            Bytes = (Get-Item -LiteralPath $resolvedOutPdf).Length
        }
        return
    }
    catch {
        Remove-GeneratedOutput -Path $resolvedOutPdf
        $wordFailure = $_.Exception.Message
        if ($Engine -eq 'Word') {
            throw "Word PDF export failed: $wordFailure"
        }
    }
}

try {
    Invoke-LibreOfficeDocxToPdf -InputPath $resolvedDocx -OutputPath $resolvedOutPdf
    Assert-ValidPdf -Path $resolvedOutPdf

    [pscustomobject]@{
        Input = $resolvedDocx
        Output = $resolvedOutPdf
        Engine = 'LibreOffice'
        Bytes = (Get-Item -LiteralPath $resolvedOutPdf).Length
        WordFallbackReason = $wordFailure
    }
}
catch {
    Remove-GeneratedOutput -Path $resolvedOutPdf
    $libreOfficeFailure = $_.Exception.Message
    if ($Engine -eq 'Auto' -and -not [string]::IsNullOrWhiteSpace($wordFailure)) {
        throw "PDF export failed. Word COM: $wordFailure LibreOffice fallback: $libreOfficeFailure"
    }

    throw "LibreOffice PDF export failed: $libreOfficeFailure"
}
