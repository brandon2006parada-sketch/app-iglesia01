param(
    [string]$BackupDirectory
)

$ErrorActionPreference = 'Stop'

if (-not $BackupDirectory) {
    $BackupDirectory = Join-Path $PSScriptRoot 'backups'
}

$envFile = Join-Path $PSScriptRoot '.env'
if (-not (Test-Path $envFile)) {
    throw 'No existe .env en la carpeta del proyecto.'
}

$envLines = Get-Content $envFile
$uriLine = $envLines | Where-Object { $_ -match '^\s*MONGO_URI\s*=' } | Select-Object -First 1
$dbLine = $envLines | Where-Object { $_ -match '^\s*MONGO_DB_NAME\s*=' } | Select-Object -First 1
if (-not $uriLine) {
    throw 'Falta MONGO_URI en .env.'
}

$mongoUri = ($uriLine -replace '^\s*MONGO_URI\s*=\s*', '').Trim().Trim('"').Trim("'")
$dbName = if ($dbLine) { ($dbLine -replace '^\s*MONGO_DB_NAME\s*=\s*', '').Trim().Trim('"').Trim("'") } else { 'iglesia' }
if (-not $mongoUri -or $mongoUri -match '<usuario>|<password>|cluster\.mongodb\.net') {
    throw 'MONGO_URI parece ser un ejemplo; actualiza .env con la conexión real antes de hacer la copia.'
}

$mongoDump = Get-Command mongodump -ErrorAction SilentlyContinue
$mongoDumpPath = if ($mongoDump) { $mongoDump.Source } else { 'C:\Program Files\MongoDB\Tools\100\bin\mongodump.exe' }
if (-not (Test-Path $mongoDumpPath)) {
    throw 'No se encontró mongodump. Instala MongoDB Database Tools y abre una terminal nueva.'
}

New-Item -ItemType Directory -Path $BackupDirectory -Force | Out-Null
$timestamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$archive = Join-Path $BackupDirectory "${dbName}_${timestamp}.archive.gz"
& $mongoDumpPath "--uri=$mongoUri" "--db=$dbName" "--archive=$archive" --gzip
if ($LASTEXITCODE -ne 0) {
    Remove-Item $archive -ErrorAction SilentlyContinue
    throw "mongodump falló con código $LASTEXITCODE. Revisa MongoDB y la URI sin compartirla."
}

Write-Output "Copia completada: $archive"
Write-Warning 'El archivo contiene datos privados. Guárdalo en un lugar cifrado y fuera de GitHub.'
