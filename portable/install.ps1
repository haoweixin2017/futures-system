$ErrorActionPreference = 'Stop'
$installRoot = Join-Path $env:LOCALAPPDATA 'FuturesTradingSystem'
if ($installRoot -match '[^\x00-\x7F]') {
    $installRoot = Join-Path ([Environment]::GetFolderPath('CommonDocuments')) 'FuturesTradingSystem'
}
if ($installRoot -match '[^\x00-\x7F]') {
    throw "MySQL 不支持当前安装路径中的非 ASCII 字符，请使用英文 Windows 用户名或选择纯英文安装目录。"
}
$payload = Join-Path $PSScriptRoot 'payload.zip'

if (Test-Path $installRoot) {
    $backup = "$installRoot.backup-$(Get-Date -Format yyyyMMddHHmmss)"
    Move-Item -LiteralPath $installRoot -Destination $backup
}

New-Item -ItemType Directory -Path $installRoot -Force | Out-Null
Expand-Archive -LiteralPath $payload -DestinationPath $installRoot -Force

$vcredist = Join-Path $installRoot 'runtime\vcredist\VC_redist.x64.exe'
$needsVCRuntime = -not ((Test-Path "$env:SystemRoot\System32\msvcp140.dll") -and (Test-Path "$env:SystemRoot\System32\vcruntime140_1.dll"))
if ($needsVCRuntime) {
    $vcProcess = Start-Process -FilePath $vcredist -ArgumentList '/install','/quiet','/norestart' -Verb RunAs -Wait -PassThru
    if ($vcProcess.ExitCode -notin @(0, 1638, 3010)) {
        throw "Microsoft Visual C++ 运行库安装失败，退出码：$($vcProcess.ExitCode)"
    }
}

$shell = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath('Desktop')
$shortcut = $shell.CreateShortcut((Join-Path $desktop '期货模拟交易系统.lnk'))
$shortcut.TargetPath = Join-Path $installRoot 'runtime\python\pythonw.exe'
$shortcut.Arguments = '"' + (Join-Path $installRoot 'launcher.py') + '"'
$shortcut.WorkingDirectory = $installRoot
$shortcut.Save()

$startMenu = Join-Path ([Environment]::GetFolderPath('Programs')) '期货模拟交易系统'
New-Item -ItemType Directory -Path $startMenu -Force | Out-Null
$menuShortcut = $shell.CreateShortcut((Join-Path $startMenu '期货模拟交易系统.lnk'))
$menuShortcut.TargetPath = $shortcut.TargetPath
$menuShortcut.Arguments = $shortcut.Arguments
$menuShortcut.WorkingDirectory = $installRoot
$menuShortcut.Save()

Start-Process -FilePath $shortcut.TargetPath -ArgumentList $shortcut.Arguments -WorkingDirectory $installRoot
[System.Windows.Forms.MessageBox]::Show('安装完成。桌面已创建“期货模拟交易系统”快捷方式。','期货模拟交易系统') | Out-Null
