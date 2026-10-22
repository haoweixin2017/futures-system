param(
    [string]$OutputDirectory = (Join-Path $PSScriptRoot 'dist')
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$outputRoot = [System.IO.Path]::GetFullPath($OutputDirectory)
$portableRoot = Join-Path $outputRoot 'FuturesTradingSystem'
$payloadZip = Join-Path $outputRoot 'payload.zip'
$installer = Join-Path $outputRoot '期货模拟交易系统-离线安装包.exe'
$installerBuild = Join-Path $outputRoot 'FuturesTradingSystem-Setup.exe'

if (-not $outputRoot.StartsWith([System.IO.Path]::GetFullPath($PSScriptRoot), [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "输出目录必须位于 portable 目录内：$outputRoot"
}

if (Test-Path $outputRoot) {
    Remove-Item -LiteralPath $outputRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $portableRoot -Force | Out-Null

function Copy-Tree($Source, $Destination, $ExcludeDirectories = @(), $ExcludeFiles = @()) {
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    $arguments = @($Source, $Destination, '/E', '/R:1', '/W:1', '/NFL', '/NDL', '/NJH', '/NJS', '/NP')
    if ($ExcludeDirectories.Count) { $arguments += '/XD'; $arguments += $ExcludeDirectories }
    if ($ExcludeFiles.Count) { $arguments += '/XF'; $arguments += $ExcludeFiles }
    & robocopy @arguments | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "复制失败：$Source -> $Destination (robocopy $LASTEXITCODE)" }
}

# 应用代码与静态资源（不带现机账号、密钥、日志和历史数据）。
$appRoot = Join-Path $portableRoot 'app'
New-Item -ItemType Directory -Path $appRoot -Force | Out-Null
Copy-Tree (Join-Path $projectRoot 'backend') (Join-Path $appRoot 'backend') @('__pycache__') @('*.pyc')
Copy-Tree (Join-Path $projectRoot 'frontend') (Join-Path $appRoot 'frontend')
Copy-Tree (Join-Path $projectRoot 'migrations') (Join-Path $appRoot 'migrations') @('__pycache__') @('*.pyc')
Copy-Item (Join-Path $projectRoot 'run.py') $appRoot
Copy-Item (Join-Path $projectRoot 'alembic.ini') $appRoot
New-Item -ItemType Directory -Path (Join-Path $appRoot 'config') -Force | Out-Null
Copy-Item (Join-Path $PSScriptRoot 'settings.yaml') (Join-Path $appRoot 'config\settings.yaml')
Copy-Item (Join-Path $projectRoot 'config\instruments.yaml') (Join-Path $appRoot 'config\instruments.yaml')
Copy-Item (Join-Path $projectRoot 'config\signals.yaml') (Join-Path $appRoot 'config\signals.yaml')
Set-Content -LiteralPath (Join-Path $appRoot 'config\users.yaml') -Value "users: []" -Encoding utf8

Copy-Item (Join-Path $PSScriptRoot 'launcher.py') $portableRoot
Copy-Item (Join-Path $PSScriptRoot 'README.txt') $portableRoot
New-Item -ItemType Directory -Path (Join-Path $portableRoot 'data') -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $portableRoot 'logs') -Force | Out-Null

# 可携带 Python：标准库 + Tk + 项目 venv 依赖。
$pythonSource = 'D:\Python311'
$pythonTarget = Join-Path $portableRoot 'runtime\python'
New-Item -ItemType Directory -Path $pythonTarget -Force | Out-Null
foreach ($file in @('python.exe','pythonw.exe','python311.dll','python3.dll','vcruntime140.dll','vcruntime140_1.dll','LICENSE.txt')) {
    Copy-Item (Join-Path $pythonSource $file) $pythonTarget
}
Copy-Tree (Join-Path $pythonSource 'DLLs') (Join-Path $pythonTarget 'DLLs') @('__pycache__') @('*.pdb','*.lib')
Copy-Tree (Join-Path $pythonSource 'Lib') (Join-Path $pythonTarget 'Lib') @('__pycache__','site-packages','test','tests','ensurepip','idlelib') @('*.pyc')
Copy-Tree (Join-Path $pythonSource 'tcl') (Join-Path $pythonTarget 'tcl')
Copy-Tree (Join-Path $projectRoot '.venv\Lib\site-packages') (Join-Path $pythonTarget 'Lib\site-packages') @('__pycache__','tests','test') @('*.pyc','*.pdb','*.lib')

# MySQL Community Server：仅携带运行所需文件，不携带现机 data 与调试符号。
$mysqlSource = 'D:\mysql'
$mysqlTarget = Join-Path $portableRoot 'runtime\mysql'
New-Item -ItemType Directory -Path (Join-Path $mysqlTarget 'bin') -Force | Out-Null
Copy-Item (Join-Path $mysqlSource 'bin\mysqld.exe') (Join-Path $mysqlTarget 'bin')
Get-ChildItem (Join-Path $mysqlSource 'bin') -Filter '*.dll' | Where-Object { $_.Name -notmatch 'debug' } | Copy-Item -Destination (Join-Path $mysqlTarget 'bin')
Copy-Tree (Join-Path $mysqlSource 'share') (Join-Path $mysqlTarget 'share')
if (Test-Path (Join-Path $mysqlSource 'lib\plugin')) {
    Copy-Tree (Join-Path $mysqlSource 'lib\plugin') (Join-Path $mysqlTarget 'lib\plugin') @() @('*.pdb','*.lib')
}
Copy-Item (Join-Path $mysqlSource 'LICENSE') $mysqlTarget
Copy-Item (Join-Path $mysqlSource 'README') $mysqlTarget

# Redis Windows 运行时；不携带现机 dump、配置和日志。
$redisSource = 'D:\Redis'
$redisTarget = Join-Path $portableRoot 'runtime\redis'
New-Item -ItemType Directory -Path $redisTarget -Force | Out-Null
Copy-Item (Join-Path $redisSource 'redis-server.exe') $redisTarget
Copy-Item (Join-Path $redisSource 'EventLog.dll') $redisTarget
Copy-Item (Join-Path $redisSource '00-RELEASENOTES') $redisTarget

# MySQL 在空白 Windows 上需要 Microsoft Visual C++ 2015-2022 x64 运行库。
$vcRedistSource = 'C:\ProgramData\Package Cache\{91ee571b-0e8a-4c65-9eaf-2e2f5fc60c00}\VC_redist.x64.exe'
if (-not (Test-Path $vcRedistSource)) { throw "未找到 VC_redist.x64.exe：$vcRedistSource" }
$vcRedistTarget = Join-Path $portableRoot 'runtime\vcredist'
New-Item -ItemType Directory -Path $vcRedistTarget -Force | Out-Null
Copy-Item $vcRedistSource $vcRedistTarget

# 先生成可解压验证的绿色版 ZIP。
New-Item -ItemType Directory -Path $outputRoot -Force | Out-Null
Compress-Archive -Path (Join-Path $portableRoot '*') -DestinationPath $payloadZip -CompressionLevel Optimal

# 用 Windows 自带 IExpress 将 payload.zip 和安装脚本封装为单个 EXE。
# IExpress 不可靠地处理中文源路径，因此在纯 ASCII 临时目录完成封装，再移回输出目录。
$iexpressRoot = Join-Path $env:TEMP ("fts-iexpress-" + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $iexpressRoot -Force | Out-Null
Copy-Item $payloadZip (Join-Path $iexpressRoot 'payload.zip')
Copy-Item (Join-Path $PSScriptRoot 'install.cmd') (Join-Path $iexpressRoot 'install.cmd')
Copy-Item (Join-Path $PSScriptRoot 'install.ps1') (Join-Path $iexpressRoot 'install.ps1')
$sed = Join-Path $iexpressRoot 'package.sed'
$iexpressOutput = Join-Path $iexpressRoot 'FuturesTradingSystem-Setup.exe'
$sedText = @"
[Version]
Class=IEXPRESS
SEDVersion=3
[Options]
PackagePurpose=InstallApp
ShowInstallProgramWindow=1
HideExtractAnimation=0
UseLongFileName=1
InsideCompressed=0
CAB_FixedSize=0
CAB_ResvCodeSigning=0
RebootMode=N
InstallPrompt=
DisplayLicense=
FinishMessage=
TargetName=$iexpressOutput
FriendlyName=Futures Trading System Offline Setup
AppLaunched=cmd.exe /c install.cmd
PostInstallCmd=<None>
AdminQuietInstCmd=
UserQuietInstCmd=
SourceFiles=SourceFiles
[Strings]
FILE0=payload.zip
FILE1=install.cmd
FILE2=install.ps1
[SourceFiles]
SourceFiles0=$iexpressRoot\
[SourceFiles0]
%FILE0%=
%FILE1%=
%FILE2%=
"@
Set-Content -LiteralPath $sed -Value $sedText -Encoding ascii
& "$env:SystemRoot\System32\iexpress.exe" /N /Q $sed
$deadline = (Get-Date).AddMinutes(3)
while (-not (Test-Path $iexpressOutput) -and (Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 500
}
if (-not (Test-Path $iexpressOutput)) { throw 'IExpress 未在 3 分钟内生成安装包。' }
# 文件出现后仍可能正在写入，等待长度稳定。
$lastLength = -1
do {
    Start-Sleep -Milliseconds 500
    $length = (Get-Item -LiteralPath $iexpressOutput).Length
    $stable = ($length -gt 0 -and $length -eq $lastLength)
    $lastLength = $length
} while (-not $stable -and (Get-Date) -lt $deadline)
Move-Item -LiteralPath $iexpressOutput -Destination $installer

$hash = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash
Set-Content -LiteralPath (Join-Path $outputRoot 'SHA256.txt') -Value $hash -Encoding ascii
Write-Host "安装包：$installer"
Write-Host "SHA256：$hash"
