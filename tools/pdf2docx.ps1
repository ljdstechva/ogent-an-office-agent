[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Pdf,

    [Parameter(Mandatory = $true)]
    [string]$OutDocx,

    [ValidateSet('Auto', 'Word', 'LibreOffice')]
    [string]$Engine = 'Auto',

    [ValidateRange(1, 1000000)]
    [int]$MinimumTextCharacters = 20,

    [switch]$WordVisible,

    [switch]$Force
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

function Disable-WordPdfConversionWarning {
    # This is the same per-user preference Word creates when the user checks
    # "Do not show this message again" on its first PDF Reflow warning.
    $wordOptionsPath = 'HKCU:\Software\Microsoft\Office\16.0\Word\Options'
    $valueName = 'DisableConvertPDFWarning'

    if (-not (Test-Path -LiteralPath $wordOptionsPath)) {
        New-Item -Path $wordOptionsPath -Force | Out-Null
    }

    $current = Get-ItemProperty -LiteralPath $wordOptionsPath -Name $valueName -ErrorAction SilentlyContinue
    if ($null -eq $current -or [int]$current.$valueName -ne 1) {
        New-ItemProperty `
            -LiteralPath $wordOptionsPath `
            -Name $valueName `
            -PropertyType DWord `
            -Value 1 `
            -Force | Out-Null
    }
}

function Remove-GeneratedOutput {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        Remove-Item -LiteralPath $Path -Force
    }
}

function Assert-NonEmptyFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label was not created: $Path"
    }

    $file = Get-Item -LiteralPath $Path
    if ($file.Length -le 0) {
        throw "$Label is empty: $Path"
    }
}

function Get-DocxTextCharacterCount {
    param([Parameter(Mandatory = $true)][string]$Path)

    Add-Type -AssemblyName System.IO.Compression.FileSystem

    $archive = $null
    $stream = $null
    $reader = $null
    try {
        $archive = [System.IO.Compression.ZipFile]::OpenRead($Path)
        $entry = $archive.GetEntry('word/document.xml')
        if ($null -eq $entry) {
            throw "Converted DOCX does not contain word/document.xml: $Path"
        }

        $stream = $entry.Open()
        $reader = New-Object System.IO.StreamReader($stream)
        $xmlText = $reader.ReadToEnd()

        $xml = New-Object System.Xml.XmlDocument
        $xml.PreserveWhitespace = $true
        $xml.LoadXml($xmlText)

        $text = ($xml.SelectNodes("//*[local-name()='t']") | ForEach-Object { $_.InnerText }) -join ''
        return ([System.Text.RegularExpressions.Regex]::Replace($text, '\s+', '')).Length
    }
    finally {
        if ($null -ne $reader) {
            $reader.Dispose()
        }
        elseif ($null -ne $stream) {
            $stream.Dispose()
        }

        if ($null -ne $archive) {
            $archive.Dispose()
        }
    }
}

function Assert-TextBasedConversion {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][int]$MinimumCharacters
    )

    $characterCount = Get-DocxTextCharacterCount -Path $Path
    if ($characterCount -lt $MinimumCharacters) {
        throw "[SCANNED_PDF] Converted document contains only $characterCount non-whitespace text character(s). The PDF appears scanned or image-only and needs OCR before this pipeline can edit it."
    }

    return $characterCount
}

function Invoke-WordPdfToDocx {
    param(
        [Parameter(Mandatory = $true)][string]$InputPath,
        [Parameter(Mandatory = $true)][string]$OutputPath,
        [switch]$ShowWindow
    )

    $word = $null
    $document = $null
    $wordOptions = $null
    try {
        Disable-WordPdfConversionWarning

        $word = New-Object -ComObject Word.Application
        $word.Visible = [bool]$ShowWindow
        $word.DisplayAlerts = 0
        $wordOptions = $word.Options
        $wordOptions.ConfirmConversions = $false

        # ConfirmConversions := false, ReadOnly := true.
        $document = $word.Documents.Open($InputPath, $false, $true)
        $document.SaveAs2($OutputPath, 16) # wdFormatXMLDocument
    }
    finally {
        if ($null -ne $document) {
            try {
                $document.Close($false)
            }
            catch {
                Write-Warning "Word could not close the converted document cleanly: $($_.Exception.Message)"
            }
            finally {
                Release-ComObject -ComObject $document
            }
        }

        if ($null -ne $wordOptions) {
            Release-ComObject -ComObject $wordOptions
        }

        if ($null -ne $word) {
            try {
                $word.Quit()
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
    }
}

function Invoke-LibreOfficePdfToDocx {
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
    $temporaryDirectory = Join-Path -Path $temporaryRoot -ChildPath ("ogent-pdf2docx-" + [System.Guid]::NewGuid().ToString('N'))
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
                '--infilter=writer_pdf_import' `
                '--convert-to' 'docx' `
                '--outdir' $temporaryDirectory `
                $InputPath 2>&1
            $exitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }

        $generatedName = [System.IO.Path]::GetFileNameWithoutExtension($InputPath) + '.docx'
        $generatedPath = Join-Path -Path $temporaryDirectory -ChildPath $generatedName
        if ($exitCode -ne 0 -or -not (Test-Path -LiteralPath $generatedPath -PathType Leaf)) {
            $details = ($conversionOutput | Out-String).Trim()
            if ([string]::IsNullOrWhiteSpace($details)) {
                $details = 'LibreOffice produced no diagnostic output.'
            }
            throw "LibreOffice PDF import failed (exit $exitCode): $details"
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

$resolvedPdf = (Resolve-Path -LiteralPath $Pdf -ErrorAction Stop).ProviderPath
$resolvedOutDocx = Get-AbsoluteOutputPath -Path $OutDocx

Assert-Extension -Path $resolvedPdf -ExpectedExtension '.pdf' -Label 'Input'
Assert-Extension -Path $resolvedOutDocx -ExpectedExtension '.docx' -Label 'Output'

$outputDirectory = Split-Path -Path $resolvedOutDocx -Parent
if (-not (Test-Path -LiteralPath $outputDirectory -PathType Container)) {
    New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
}

if (Test-Path -LiteralPath $resolvedOutDocx) {
    if (-not $Force) {
        throw "Output already exists. Choose a new path or pass -Force: $resolvedOutDocx"
    }
    Remove-Item -LiteralPath $resolvedOutDocx -Force
}

$wordFailure = $null
if ($Engine -in @('Auto', 'Word')) {
    try {
        Invoke-WordPdfToDocx -InputPath $resolvedPdf -OutputPath $resolvedOutDocx -ShowWindow:$WordVisible
        Assert-NonEmptyFile -Path $resolvedOutDocx -Label 'DOCX output'
        $textCharacters = Assert-TextBasedConversion -Path $resolvedOutDocx -MinimumCharacters $MinimumTextCharacters

        [pscustomobject]@{
            Input = $resolvedPdf
            Output = $resolvedOutDocx
            Engine = 'Word COM'
            TextCharacters = $textCharacters
        }
        return
    }
    catch {
        Remove-GeneratedOutput -Path $resolvedOutDocx
        if ($_.Exception.Message.StartsWith('[SCANNED_PDF]', [System.StringComparison]::Ordinal)) {
            throw $_.Exception.Message
        }

        $wordFailure = $_.Exception.Message
        if ($Engine -eq 'Word') {
            throw "Word PDF Reflow failed: $wordFailure"
        }
    }
}

try {
    Invoke-LibreOfficePdfToDocx -InputPath $resolvedPdf -OutputPath $resolvedOutDocx
    Assert-NonEmptyFile -Path $resolvedOutDocx -Label 'DOCX output'
    $textCharacters = Assert-TextBasedConversion -Path $resolvedOutDocx -MinimumCharacters $MinimumTextCharacters

    [pscustomobject]@{
        Input = $resolvedPdf
        Output = $resolvedOutDocx
        Engine = 'LibreOffice'
        TextCharacters = $textCharacters
        WordFallbackReason = $wordFailure
    }
}
catch {
    Remove-GeneratedOutput -Path $resolvedOutDocx
    if ($_.Exception.Message.StartsWith('[SCANNED_PDF]', [System.StringComparison]::Ordinal)) {
        throw $_.Exception.Message
    }

    $libreOfficeFailure = $_.Exception.Message
    if ($Engine -eq 'Auto' -and -not [string]::IsNullOrWhiteSpace($wordFailure)) {
        throw "PDF conversion failed. Word PDF Reflow: $wordFailure LibreOffice fallback: $libreOfficeFailure"
    }

    throw "LibreOffice PDF import failed: $libreOfficeFailure"
}
