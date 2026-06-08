<#
.SYNOPSIS
    Issues (or renews) a Let's Encrypt certificate via Posh-ACME using the Cloudflare plugin

.DESCRIPTION
    Wraps Posh-ACME's New-PACertificate against the Let's Encrypt production
    CA (LE_PROD). DNS validation is fully automated through Cloudflare, so the
    domain's zone must be hosted on Cloudflare and the supplied API token must
    have Zone:DNS:Edit (and Zone:Zone:Read) on that zone.

    Posh-ACME stores the token ENCRYPTED in the order config, so subsequent
    renewals (Submit-Renewal) work without re-supplying it.

.PARAMETER Domain
    The FQDN to issue the certificate for, e.g. app.trawlr.net

.PARAMETER CFToken
    A Cloudflare API token with DNS edit rights on the domain's zone.

.PARAMETER Contact
    ACME account contact email

.EXAMPLE
    .\New-Certificate.ps1 -Domain app.trawlr.net -CFToken "abcd1234..."
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$Domain,

    [Parameter(Mandatory)]
    [string]$CFToken,

    [Parameter(Mandatory)]
    [string]$Contact
)

$ErrorActionPreference = "Stop"

# --- Ensure the module is available ---------------------------------------
if (-not (Get-Module -ListAvailable -Name Posh-ACME)) {
    throw "Posh-ACME is not installed. Run: Install-Module Posh-ACME -Scope CurrentUser"
}
Import-Module Posh-ACME
Set-PAServer LE_PROD
$pArgs = @{
    CFToken = (ConvertTo-SecureString $CFToken -AsPlainText -Force)
}

Write-Host "Requesting certificate for $Domain via Cloudflare DNS-01..." -ForegroundColor Cyan

# --- Issue / renew --------------------------------------------------------
$cert = New-PACertificate -Domain $Domain `
    -Plugin Cloudflare `
    -PluginArgs $pArgs `
    -AcceptTOS `
    -Contact $Contact

# --- Report -----------------------------------------------------------------
Write-Host ""
Write-Host "Certificate issued for $($cert.Subject)" -ForegroundColor Green
Write-Host "  Thumbprint : $($cert.Thumbprint)"
Write-Host "  Not After  : $($cert.NotAfter)"
Write-Host ""
Write-Host "Files written to:" -ForegroundColor Cyan

# Emit each generated file as a clickable file:// link
$cert | Select-Object CertFile, KeyFile, ChainFile, FullChainFile, PfxFile, PfxFullChain |
    Get-Member -MemberType NoteProperty |
    ForEach-Object {
        $path = $cert.($_.Name)
        if ($path -and (Test-Path $path)) {
            $uri = ([System.Uri]$path).AbsoluteUri
            Write-Host ("  {0,-13}: {1}" -f $_.Name, $uri)
        }
    }

Write-Host ""
Write-Host ("Order folder: {0}" -f ([System.Uri]((Split-Path $cert.CertFile))).AbsoluteUri) -ForegroundColor Cyan

# Return the cert object for downstream scripting
$cert
