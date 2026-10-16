# «Senden an > Buchhaltung (Beleg)»: jede Datei geht über den eingeschränkten SSH-Zugang in inbox/ auf dem Server.
# Danach erscheint sie in aeradex unter «Beleg hochladen» bzw. als Entwurf, sobald sie eingelesen ist.
param([string]$Alias = "aeradex-buch", [Parameter(ValueFromRemainingArguments = $true)][string[]]$Files)
Add-Type -AssemblyName PresentationFramework
$ok = @(); $bad = @()
foreach ($path in $Files) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { continue }
    $name = [IO.Path]::GetFileName($path)
    # cmd redirects the file as bytes (PowerShell 5 would re-encode a binary pipe)
    $out = & cmd.exe /c "ssh -o BatchMode=yes $Alias inbox `"$name`" < `"$path`" 2>&1"
    if ($LASTEXITCODE -eq 0) { $ok += "$name → $out" } else { $bad += "${name}: $out" }
}
$text = ""
if ($ok) { $text += "Im Eingang der Buchhaltung:`n" + ($ok -join "`n") }
if ($bad) { $text += "`n`nNicht gesendet:`n" + ($bad -join "`n") }
if (-not $text) { $text = "Keine Datei ausgewählt." }
[System.Windows.MessageBox]::Show($text, "Buchhaltung") | Out-Null
