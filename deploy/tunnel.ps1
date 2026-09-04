# Open the SSH tunnel to the instance and the app in the browser.
#
#   deploy\tunnel.ps1 -InstanceIp 13.212.x.x
#
# The app is never exposed on a public port; this tunnel is the only way in.
# Keep the window open while using the app — closing it (or Ctrl+C) drops the
# tunnel. The browser tab may load before the tunnel is up; just refresh.
param(
    [Parameter(Mandatory = $true)][string]$InstanceIp,
    [string]$KeyFile = "$HOME\.ssh\spending-tracker.pem",
    [string]$User = "ubuntu",
    [int]$Port = 8000
)

if (-not (Test-Path $KeyFile)) {
    Write-Error "Key file not found: $KeyFile (pass -KeyFile if it lives elsewhere)"
    exit 1
}

Start-Process "http://localhost:$Port"
ssh -i $KeyFile -L "${Port}:localhost:${Port}" "$User@$InstanceIp"
