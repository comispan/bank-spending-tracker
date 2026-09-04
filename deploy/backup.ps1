# Pull data/ (the SQLite file and the uploaded statement PDFs) off the
# instance into a timestamped local folder. Run it from anywhere:
#
#   deploy\backup.ps1 -InstanceIp 13.212.x.x
#
# Backups land in backups\<timestamp>\data\ beside the repo, which is
# gitignored — this is real financial data and must never be committed.
param(
    [Parameter(Mandatory = $true)][string]$InstanceIp,
    [string]$KeyFile = "$HOME\.ssh\spending-tracker.pem",
    [string]$User = "ubuntu",
    [string]$RemoteRepo = "bank-spending-tracker"
)

$repoRoot = Split-Path $PSScriptRoot -Parent
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$dest = Join-Path $repoRoot "backups\$stamp"
New-Item -ItemType Directory -Force $dest | Out-Null

scp -i $KeyFile -r "${User}@${InstanceIp}:${RemoteRepo}/data" $dest
if ($LASTEXITCODE -ne 0) {
    Write-Error "scp failed — is the instance running and the IP current?"
    exit 1
}
Write-Host "Backed up to $dest"
