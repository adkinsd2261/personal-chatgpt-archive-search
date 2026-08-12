[CmdletBinding()]
param(
    [switch]$CopyToken
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$cachePath = Join-Path $projectRoot ".cache"
$secretPath = Join-Path $cachePath "archive-context-token.clixml"
$serviceOutPath = Join-Path $cachePath "archive-context-service.stdout.log"
$serviceErrorPath = Join-Path $cachePath "archive-context-service.stderr.log"
$tunnelOutPath = Join-Path $cachePath "archive-context-tunnel.stdout.log"
$tunnelErrorPath = Join-Path $cachePath "archive-context-tunnel.stderr.log"
$localHealthUrl = "http://127.0.0.1:8766/api/health"
$publicContextUrl = "https://archive.javlin.ai/api/context"

function ConvertFrom-SecureToken {
    param([Security.SecureString]$SecureToken)

    $tokenPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureToken)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($tokenPointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($tokenPointer)
    }
}

function Save-ProtectedToken {
    param([string]$Token)

    $secureToken = ConvertTo-SecureString -String $Token -AsPlainText -Force
    $secureToken | Export-Clixml -LiteralPath $secretPath -Force
}

function New-ArchiveToken {
    $tokenBytes = New-Object byte[] 32
    $generator = New-Object Security.Cryptography.RNGCryptoServiceProvider
    try {
        $generator.GetBytes($tokenBytes)
        return [Convert]::ToBase64String($tokenBytes)
    }
    finally {
        $generator.Dispose()
        [Array]::Clear($tokenBytes, 0, $tokenBytes.Length)
    }
}

function Test-AuthenticatedHealth {
    param([string]$Token)

    try {
        $null = Invoke-RestMethod `
            -Uri $localHealthUrl `
            -Headers @{ Authorization = "Bearer $Token" } `
            -Method Get `
            -TimeoutSec 4
        return $true
    }
    catch {
        return $false
    }
}

function Test-LocalPort {
    $client = New-Object Net.Sockets.TcpClient
    try {
        $connection = $client.BeginConnect("127.0.0.1", 8766, $null, $null)
        if (-not $connection.AsyncWaitHandle.WaitOne(400)) {
            return $false
        }
        $client.EndConnect($connection)
        return $true
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

function Test-PublicTunnel {
    $body = '{"query":"startup connectivity check","depth":"light"}'
    try {
        $response = Invoke-WebRequest `
            -Uri $publicContextUrl `
            -Method Post `
            -ContentType "application/json" `
            -Body $body `
            -TimeoutSec 8 `
            -UseBasicParsing
        return ([int]$response.StatusCode -eq 401)
    }
    catch {
        if ($_.Exception.Response) {
            return ([int]$_.Exception.Response.StatusCode -eq 401)
        }
        return $false
    }
}

function Wait-Until {
    param(
        [scriptblock]$Condition,
        [int]$TimeoutSeconds,
        [string]$FailureMessage
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (& $Condition) {
            return
        }
        Start-Sleep -Milliseconds 500
    }
    throw $FailureMessage
}

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "Python environment not found at $pythonPath"
}

New-Item -ItemType Directory -Path $cachePath -Force | Out-Null

$tokenWasCreated = $false
$tokenWasAdopted = $false
if (Test-Path -LiteralPath $secretPath -PathType Leaf) {
    $protectedToken = Import-Clixml -LiteralPath $secretPath
    if ($protectedToken -isnot [Security.SecureString]) {
        throw "The protected token file is invalid: $secretPath"
    }
    $archiveToken = ConvertFrom-SecureToken -SecureToken $protectedToken
}
else {
    $clipboardToken = [string](Get-Clipboard -Raw -ErrorAction SilentlyContinue)
    $clipboardToken = $clipboardToken.Trim()
    if ($clipboardToken.Length -ge 32 -and (Test-AuthenticatedHealth -Token $clipboardToken)) {
        $archiveToken = $clipboardToken
        $tokenWasAdopted = $true
    }
    else {
        $archiveToken = New-ArchiveToken
        $tokenWasCreated = $true
    }
    Save-ProtectedToken -Token $archiveToken
}

$env:ARCHIVE_CONTEXT_TOKEN = $archiveToken

if (-not (Test-AuthenticatedHealth -Token $archiveToken)) {
    if (Test-LocalPort) {
        throw "Port 8766 is already in use by a service with a different token. Close that old archive service window, then run this launcher again."
    }

    Start-Process `
        -FilePath $pythonPath `
        -ArgumentList @("-m", "archive_context.service") `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $serviceOutPath `
        -RedirectStandardError $serviceErrorPath

    Wait-Until `
        -TimeoutSeconds 30 `
        -Condition { Test-AuthenticatedHealth -Token $archiveToken } `
        -FailureMessage "The archive service did not become healthy. Check $serviceErrorPath"
}

if (-not (Test-PublicTunnel)) {
    $cloudflared = Get-Command cloudflared -ErrorAction SilentlyContinue
    if (-not $cloudflared) {
        throw "cloudflared is not installed or is not on PATH."
    }

    Start-Process `
        -FilePath $cloudflared.Source `
        -ArgumentList @(
            "tunnel",
            "--no-autoupdate",
            "--loglevel", "warn",
            "run",
            "--url", "http://127.0.0.1:8766",
            "archive-context"
        ) `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $tunnelOutPath `
        -RedirectStandardError $tunnelErrorPath

    Wait-Until `
        -TimeoutSeconds 35 `
        -Condition { Test-PublicTunnel } `
        -FailureMessage "The public tunnel did not become reachable. Check $tunnelErrorPath"
}

if ($CopyToken -or $tokenWasCreated) {
    Set-Clipboard -Value $archiveToken
}

Write-Host "Archive Context is ready." -ForegroundColor Green
Write-Host "Local service: healthy"
Write-Host "Public tunnel: reachable at https://archive.javlin.ai"
Write-Host "Token: protected for this Windows user in .cache (not stored in Git)"

if ($tokenWasAdopted) {
    Write-Host "The current working token was adopted from the clipboard; no GPT authentication update is needed."
}
elseif ($tokenWasCreated) {
    Write-Host "A new token was created and copied to the clipboard. Paste it into the GPT Action once."
}
elseif ($CopyToken) {
    Write-Host "The existing token was copied to the clipboard."
}
