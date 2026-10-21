# Ein Windows-PC: Buchhaltung im Browser + das eigene Claude Code mit den aeradex-Werkzeugen auf dem Server.
# Das Buch liegt auf dem Server (aeradex serve + Plugin «zugriff»); auf dem PC liegt nichts davon. Kein WSL nötig.
#
# In PowerShell (als die Person, nicht als Administrator):
#   Set-ExecutionPolicy -Scope Process Bypass
#   .\install.ps1 -Server buch.example.ch -Benutzer aeradex -Url https://buch.example.ch
#   (optional -KnownHosts known_hosts: Server-Schlüssel fest hinterlegen statt beim ersten Verbinden bestätigen)
#
# Wiederholbar: bestehender Schlüssel und Einstellungen bleiben.
param(
    [Parameter(Mandatory = $true)][string]$Server,
    [Parameter(Mandatory = $true)][string]$Benutzer,
    [Parameter(Mandatory = $true)][string]$Url,
    [string]$Alias = "aeradex-buch",
    [string]$KnownHosts = ""
)
$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$ssh = Join-Path $env:USERPROFILE ".ssh"
$key = Join-Path $ssh $Alias
$tools = Join-Path $env:LOCALAPPDATA "aeradex-buchhaltung"
New-Item -ItemType Directory -Force -Path $ssh, $tools | Out-Null

# 1. SSH (in Windows 10/11 eingebaut)
if (-not (Get-Command ssh.exe -ErrorAction SilentlyContinue)) {
    throw "ssh fehlt. Einstellungen > Apps > Optionale Features > «OpenSSH-Client» hinzufügen, dann nochmals starten."
}

# 2. Eigener Schlüssel (nur für die Buchhaltung; auf dem Server auf die Werkzeuge beschränkt)
if (-not (Test-Path $key)) {
    Start-Process ssh-keygen.exe -ArgumentList "-q", "-t", "ed25519", "-f", "`"$key`"", "-N", '""', "-C", "$($env:USERNAME.ToLower())-buchhaltung" `
        -Wait -NoNewWindow
}

# 3. Server hinterlegen; mit -KnownHosts ist sein Schlüssel fest geprüft (kein «trust on first use»)
$config = Join-Path $ssh "config"
$hostCheck = "    StrictHostKeyChecking accept-new"
if ($KnownHosts) {
    Copy-Item $KnownHosts (Join-Path $ssh "known_hosts_$Alias") -Force
    $hostCheck = "    UserKnownHostsFile ~/.ssh/known_hosts_$Alias`n    StrictHostKeyChecking yes"
}
$block = @"

Host $Alias
    HostName $Server
    User $Benutzer
    IdentityFile ~/.ssh/$Alias
    IdentitiesOnly yes
$hostCheck
    ServerAliveInterval 30
"@
if (-not ((Test-Path $config) -and (Select-String -Path $config -Pattern "^Host $Alias$" -Quiet))) {
    Add-Content -Path $config -Value $block -Encoding ascii
}

# 4. Claude Code (eigenes Abo) mit den aeradex-Werkzeugen auf dem Server
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    Write-Host "Installiere Claude Code ..."
    Invoke-RestMethod https://claude.ai/install.ps1 | Invoke-Expression
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + $env:Path
}
if (Get-Command claude -ErrorAction SilentlyContinue) {
    $list = (& claude mcp list 2>$null) -join "`n"
    if ($list -notmatch "aeradex") {
        & claude mcp add --scope user aeradex -- ssh $Alias mcp
    }
} else {
    Write-Host "Claude Code nicht gefunden: nach der Installation ein neues PowerShell-Fenster öffnen und das Skript nochmals starten."
}

# 5. Desktop-Link «Buchhaltung» (Browser, Login)
$desktop = [Environment]::GetFolderPath("Desktop")
Set-Content -Path (Join-Path $desktop "Buchhaltung.url") -Value "[InternetShortcut]`r`nURL=$Url`r`n" -Encoding ascii

# 6. Rechtsklick > Senden an > «Buchhaltung (Beleg)»: PDFs und Fotos landen im Eingang auf dem Server
Copy-Item (Join-Path $here "belege-senden.ps1") (Join-Path $tools "belege-senden.ps1") -Force
$sendTo = [Environment]::GetFolderPath("SendTo")
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut((Join-Path $sendTo "Buchhaltung (Beleg).lnk"))
$lnk.TargetPath = "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe"
$lnk.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$(Join-Path $tools 'belege-senden.ps1')`" -Alias $Alias"
$lnk.Description = "Beleg in den Eingang der Buchhaltung schicken"
$lnk.Save()

Write-Host ""
Write-Host "Fertig. Diesen Schlüssel in aeradex unter Einstellungen > Buch > Zugriff eintragen (lassen):"
Write-Host ""
Get-Content "$key.pub"
Write-Host ""
Write-Host "Danach: «claude» starten und mit dem eigenen Konto anmelden. In Claude stehen die Buchhaltungs-Werkzeuge bereit."
Write-Host "Buchhaltung im Browser: Desktop-Link «Buchhaltung» ($Url)."
