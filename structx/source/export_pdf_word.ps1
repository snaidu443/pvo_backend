# Saves a Word document as PDF using the locally installed Microsoft Word
# (COM automation). Called by word.py through office.run_export with every
# value passed as a parameter -- this script is fixed text, never assembled
# from input.
#
# $InputPath is a throwaway copy of the user's document (in a temp
# directory). It is opened read-only, without a window and with alerts off
# (no dialog can block an unattended run), exported, and closed without
# saving; only the PDF is written. Word itself is quit only if no other
# document is open, so a user's own Word session is left alone.

param(
    [Parameter(Mandatory = $true)][string]$InputPath,
    [Parameter(Mandatory = $true)][string]$OutputPath
)

$ErrorActionPreference = 'Stop'

$app = New-Object -ComObject Word.Application
$doc = $null
try {
    $app.Visible = $false
    $app.DisplayAlerts = 0   # wdAlertsNone
    # Open(FileName, ConfirmConversions=false, ReadOnly=true, AddToRecentFiles=false)
    $doc = $app.Documents.Open($InputPath, $false, $true, $false)
    # ExportAsFixedFormat(OutputFileName, wdExportFormatPDF=17)
    $doc.ExportAsFixedFormat($OutputPath, 17)
}
finally {
    # Close(SaveChanges=wdDoNotSaveChanges=0)
    if ($null -ne $doc) { $doc.Close(0) }
    if ($app.Documents.Count -eq 0) { $app.Quit() }
    [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($app)
}
