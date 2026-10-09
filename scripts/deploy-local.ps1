<#
.SYNOPSIS
    Copies the add-on to a Home Assistant instance as a local test add-on ("staging") and (re)builds it.

.DESCRIPTION
    Requires the "Advanced SSH & Web Terminal" add-on with your public key in
    authorized_keys. The add-on is copied to /local_apps/ha_llm_runner (older
    versions: /addons/ha_llm_runner) and shows up under local add-ons as
    "HA LLM Runner" (slug local_ha_llm_runner).

    The version installed from the add-on store is production. On every deploy,
    the configuration of the local add-on is replaced by a fresh copy of the
    production configuration (llm_tasks.yaml, processors/, memory/ and options;
    not audit/). Nothing is ever copied back to production.

    The local add-on is left stopped. Stop the production add-on before starting
    it: both use the same MQTT topics and sensors.

    -Remove uninstalls the local add-on together with its configuration.

.EXAMPLE
    .\scripts\deploy-local.ps1 -HostName 192.168.10.5
.EXAMPLE
    .\scripts\deploy-local.ps1 -CheckOnly
.EXAMPLE
    .\scripts\deploy-local.ps1 -Remove
#>
param(
    [string]$HostName = "homeassistant.local",
    [string]$User = "root",
    [int]$Port = 22,
    [switch]$CheckOnly,
    [switch]$Remove
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

# Remote scripts are sent via stdin, so double quotes and $variables reach bash unchanged;
# tr drops the CRs PowerShell adds.
$common = @'
set -e
if ha apps --help >/dev/null 2>&1; then CLI=apps; else CLI=addons; fi
for d in /app_configs /addon_configs; do if [ -d "$d" ]; then CONFIGS=$d; break; fi; done
info() { ha $CLI info "$1" --raw-json; }
installed() { [ -n "$(info "$1" 2>/dev/null | jq -r '.data.version // empty')" ]; }

'@

function Invoke-Remote([string]$Body, [string]$Failure) {
    $text = ($common + $Body).Replace("__SLUG__", $slug).Replace("__FOLDER__", $folder).Replace("__TARGET__", $target)
    $text | ssh @sshArgs $remote "tr -d '\r' | bash -s"
    if ($LASTEXITCODE -ne 0) { throw $Failure }
}

if ($Remove) {
    $removeScript = @'
if installed __SLUG__; then
  ha $CLI stop __SLUG__ >/dev/null 2>&1 || true
  ha $CLI uninstall __SLUG__ --remove-config
else
  echo "The local add-on is not installed."
fi
rm -rf "$CONFIGS/__SLUG__" __TARGET__ __TARGET__.new
ha store reload >/dev/null 2>&1 || ha $CLI reload >/dev/null
echo "Local add-on removed."
'@
    Invoke-Remote $removeScript "Removing failed. See the output above."
    return
}

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

$deployScript = @'
rm -rf __TARGET__.new && mkdir -p __TARGET__.new
tar -xzf /tmp/ha_llm_runner_deploy.tar.gz -C __TARGET__.new
rm -f /tmp/ha_llm_runner_deploy.tar.gz
rm -rf __TARGET__ && mv __TARGET__.new __TARGET__
ha store reload >/dev/null 2>&1 || ha $CLI reload >/dev/null
# Stopped first, so a rebuild does not restart it with the old configuration.
ha $CLI stop __SLUG__ >/dev/null 2>&1 || true
if installed __SLUG__; then
  if ha $CLI rebuild __SLUG__ 2>/dev/null; then echo "Rebuilt."; else ha $CLI update __SLUG__ && echo "Updated to the new version."; fi
else
  ha $CLI install __SLUG__ && echo "Installed."
fi
ha $CLI stop __SLUG__ >/dev/null 2>&1 || true

PROD=$(ha $CLI --raw-json | jq -r '.data.addons[] | select(.slug | endswith("___FOLDER__")) | select(.slug != "__SLUG__") | .slug' | head -1)
if [ -z "$PROD" ]; then
  echo "No store version installed, so there is no production configuration to copy. Configure the local add-on in Home Assistant."
  exit 0
fi
echo "Replacing the local configuration with a copy of $PROD ..."
STAGING=$CONFIGS/__SLUG__
rm -rf "$STAGING" && mkdir -p "$STAGING"
for item in llm_tasks.yaml processors memory; do
  if [ -e "$CONFIGS/$PROD/$item" ]; then cp -a "$CONFIGS/$PROD/$item" "$STAGING/"; fi
done
rm -rf "$STAGING/processors/__pycache__"
echo "Copied: $(ls "$STAGING" | tr '\n' ' ')"
post_options() {
  curl -sf -X POST -H "Authorization: Bearer $SUPERVISOR_TOKEN" -H "Content-Type: application/json" \
    -d "$1" "http://supervisor/addons/__SLUG__/options" >/dev/null
}
STAGING_OPTS=$(info __SLUG__ | jq -c '.data.options')
PROD_OPTS=$(info "$PROD" | jq -c '.data.options')
# New options keep their staging defaults; options the new version dropped are left out on the retry.
if post_options "$(jq -nc --argjson s "$STAGING_OPTS" --argjson p "$PROD_OPTS" '{options: ($s + $p)}')"; then
  echo "Options copied."
elif post_options "$(jq -nc --argjson s "$STAGING_OPTS" --argjson p "$PROD_OPTS" '{options: ($s + ($p | with_entries(select(.key as $k | $s | has($k)))))}')"; then
  echo "Options copied (options this version no longer knows were left out)."
else
  echo "WARNING: the options could not be copied. Set them in Home Assistant."
fi
echo "Stop $PROD before starting the local add-on: both write the same MQTT topics and sensors."
'@
Write-Host "Building on Home Assistant (this can take several minutes) ..."
Invoke-Remote $deployScript "Build failed. See the output above or the Supervisor log."
Write-Host "Done. The local add-on is stopped; start it under Settings > Add-ons > HA LLM Runner."
