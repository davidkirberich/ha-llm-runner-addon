<#
.SYNOPSIS
    Copies the add-on to a Home Assistant instance as a local add-on and (re)builds it.

.DESCRIPTION
    Requires the "Advanced SSH & Web Terminal" add-on with your public key in
    authorized_keys. The add-on is copied to /local_apps/ha_llm_runner (older
    versions: /addons/ha_llm_runner) and shows up under local add-ons as
    "HA LLM Runner" (slug local_ha_llm_runner). Stop the
    GitHub-installed version while testing: both use the same MQTT topics.

.EXAMPLE
    .\scripts\deploy-local.ps1 -HostName 192.168.10.5
.EXAMPLE
    .\scripts\deploy-local.ps1 -CheckOnly
#>
param(
    [string]$HostName = "homeassistant.local",
    [string]$User = "root",
    [int]$Port = 22,
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"
$source = Join-Path $PSScriptRoot "..\ha-llm-runner" | Resolve-Path
$folder = "ha_llm_runner"
$slug = "local_ha_llm_runner"
$remote = "$User@$HostName"
$sshOptions = @("-o", "ConnectTimeout=10", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new")
$sshArgs = @("-p", $Port) + $sshOptions

foreach ($tool in "ssh", "scp", "tar") {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "'$tool' was not found. Install the Windows 'OpenSSH Client' feature."
    }
}

Write-Host "Checking SSH connection to $remote ..."
# Newer Home Assistant versions call add-ons "apps" and mount the local folder at /local_apps
$base = ssh @sshArgs $remote "command -v ha >/dev/null && for d in /local_apps /addons; do if [ -d `$d ]; then echo `$d; break; fi; done"
if ($LASTEXITCODE -ne 0 -or -not $base) {
    throw "No SSH access to $remote, or the local add-on folder / the 'ha' CLI is missing. Check host, user, port and authorized_keys."
}
$target = "$($base.Trim())/$folder"
Write-Host "Connection OK. Target: $target"
if ($CheckOnly) { return }

$archive = Join-Path ([IO.Path]::GetTempPath()) "ha_llm_runner_deploy.tar.gz"
try {
    Write-Host "Packing $source ..."
    tar --exclude=__pycache__ --exclude=.pytest_cache --exclude="*.pyc" -czf $archive -C $source .
    if ($LASTEXITCODE -ne 0) { throw "tar failed." }

    Write-Host "Uploading ..."
    scp -P $Port @sshOptions -q $archive "${remote}:/tmp/ha_llm_runner_deploy.tar.gz"
    if ($LASTEXITCODE -ne 0) { throw "scp failed." }
}
finally {
    Remove-Item $archive -ErrorAction SilentlyContinue
}

# Single quotes only: Windows PowerShell 5.1 mangles double quotes in native arguments.
$script = @(
    "set -e",
    "rm -rf $target.new && mkdir -p $target.new",
    "tar -xzf /tmp/ha_llm_runner_deploy.tar.gz -C $target.new",
    "rm -f /tmp/ha_llm_runner_deploy.tar.gz",
    "rm -rf $target && mv $target.new $target",
    "if ha apps --help >/dev/null 2>&1; then CLI=apps; else CLI=addons; fi",
    "ha store reload >/dev/null 2>&1 || ha `$CLI reload >/dev/null",
    "if ha `$CLI rebuild $slug 2>/dev/null; then echo 'Rebuilt.'",
    "elif ha `$CLI update $slug 2>/dev/null; then echo 'Updated to the new version.'",
    "else ha `$CLI install $slug && echo 'Installed. Configure it in Home Assistant, then start it.'",
    "fi"
) -join "`n"

Write-Host "Building on Home Assistant (this can take several minutes) ..."
ssh @sshArgs $remote $script
if ($LASTEXITCODE -ne 0) { throw "Build failed. See the output above or the Supervisor log." }
Write-Host "Done. Logs: Settings > Add-ons > HA LLM Runner > Log."
