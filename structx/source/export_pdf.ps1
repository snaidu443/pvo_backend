# Saves a PowerPoint file as PDF, one page per slide, using the locally
# installed Microsoft PowerPoint (COM automation). Called by powerpoint.py
# with every value passed as a parameter -- this script is fixed text, never
# assembled from input.
#
# $InputPath is a throwaway copy of the user's deck (in a temp directory)
# from which powerpoint.py has already removed the hidden slides, so the PDF
# has one page per visible slide whatever PowerPoint's own "include hidden
# slides" export option says. The copy is opened read-only and without a
# window and only closed again; only the PDF is written. PowerPoint itself is quit only if no other presentation is
# open, so a user's own PowerPoint session is left alone.

param(
    [Parameter(Mandatory = $true)][string]$InputPath,
    [Parameter(Mandatory = $true)][string]$OutputPath
)

$ErrorActionPreference = 'Stop'

$app = New-Object -ComObject PowerPoint.Application
$pres = $null
try {
    # Open(FileName, ReadOnly=msoTrue, Untitled=msoFalse, WithWindow=msoFalse)
    $pres = $app.Presentations.Open($InputPath, -1, 0, 0)
    # SaveAs(FileName, ppSaveAsPDF=32)
    $pres.SaveAs($OutputPath, 32)
}
finally {
    if ($null -ne $pres) { $pres.Close() }
    if ($app.Presentations.Count -eq 0) { $app.Quit() }
    [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($app)
}
