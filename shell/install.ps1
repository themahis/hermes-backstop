# Adds (or with -Uninstall removes) one line in your PowerShell profile that loads the Backstop hook.
# It never changes your execution policy; it tells you if the policy will stop the profile from loading.
param([switch]$Uninstall)

$ErrorActionPreference = 'Stop'

$hook = Join-Path $PSScriptRoot 'backstop.ps1'
$line = ". `"$hook`"  # backstop"
$profilePath = $PROFILE.CurrentUserCurrentHost

if ($Uninstall) {
    if (Test-Path -LiteralPath $profilePath) {
        $kept = Get-Content -LiteralPath $profilePath | Where-Object { $_ -notmatch '# backstop$' }
        Set-Content -LiteralPath $profilePath -Value $kept -Encoding utf8
    }
    Write-Host "Backstop removed from $profilePath (open a new terminal)."
    return
}

if (-not (Test-Path -LiteralPath $profilePath)) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $profilePath) | Out-Null
    New-Item -ItemType File -Force -Path $profilePath | Out-Null
}
if (-not (Select-String -LiteralPath $profilePath -Pattern '# backstop$' -Quiet)) {
    Add-Content -LiteralPath $profilePath -Value $line -Encoding utf8
}
Write-Host "Backstop added to $profilePath"

$policy = Get-ExecutionPolicy
if ($policy -in @('Restricted', 'AllSigned', 'Undefined')) {
    Write-Host ""
    Write-Host "Your execution policy is '$policy', so PowerShell will not load your profile." -ForegroundColor Yellow
    Write-Host "To allow local scripts for your user only, run:" -ForegroundColor Yellow
    Write-Host "    Set-ExecutionPolicy -Scope CurrentUser RemoteSigned" -ForegroundColor Yellow
    Write-Host "Or try it in one window:  powershell -ExecutionPolicy Bypass -NoExit -File `"$hook`""
} else {
    Write-Host "Open a new terminal. A failed build/test command now sends its output to Hermes for a read-only diagnosis."
}
