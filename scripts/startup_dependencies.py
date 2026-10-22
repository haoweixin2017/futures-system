"""Windows 启动前检查依赖；不加载交易引擎或改写数据库。"""
import base64
import socket
import subprocess
import sys
import time


def port_ready(host, port):
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def start_windows_service(name):
    if name not in ('MySQL', 'Redis'):
        raise ValueError('不支持的依赖服务')
    if sys.platform != 'win32':
        raise RuntimeError(f'{name} 无法连接，请先启动对应数据库服务。')
    # 仅服务启动命令提权，主应用继续以当前用户运行。
    command = (
        "$ErrorActionPreference='Stop'; "
        f"Start-Service -Name '{name}' -ErrorAction Stop; "
        f"(Get-Service -Name '{name}').WaitForStatus('Running', "
        "[TimeSpan]::FromSeconds(60))"
    )
    encoded = base64.b64encode(command.encode('utf-16le')).decode('ascii')
    script = (
        "$ErrorActionPreference='Stop'; try { "
        f"$svc=Get-Service -Name '{name}'; "
        "if ($svc.Status -eq 'Running') { exit 0 }; "
        "$admin=([Security.Principal.WindowsPrincipal]"
        "[Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole("
        "[Security.Principal.WindowsBuiltInRole]::Administrator); "
        f"if ($admin) {{ {command} }} else {{ "
        "$child=Start-Process -FilePath "
        "\"$PSHOME\\powershell.exe\" -Verb RunAs -WindowStyle Hidden "
        f"-ArgumentList '-NoProfile -NonInteractive -EncodedCommand {encoded}' "
        "-Wait -PassThru; exit $child.ExitCode } "
        "} catch { Write-Error $_ -ErrorAction Continue; exit 1 }"
    )
    try:
        result = subprocess.run(
            ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
            creationflags=subprocess.CREATE_NO_WINDOW, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f'{name} 自动启动失败或超时，请以管理员身份启动该服务。') from exc
    if result.returncode:
        raise RuntimeError(
            f'{name} 自动启动失败（可能未安装、启动异常或取消授权）；'
            f'请以管理员身份执行 Start-Service {name}，再重试。')


def ensure_dependencies(config):
    for name, target in [('MySQL', config.mysql), ('Redis', config.redis)]:
        if port_ready(target.host, target.port):
            print(f'[启动检查] {name} 端口已就绪。', flush=True)
            continue
        if target.host.lower() not in ('127.0.0.1', 'localhost', '::1'):
            raise RuntimeError(
                f'远程 {name} {target.host}:{target.port} 无法连接，请检查远程服务和网络。')
        print(f'[启动检查] {name} 未就绪，正在启动；如弹出 Windows 授权提示请允许。', flush=True)
        start_windows_service(name)
        deadline = time.monotonic() + 60
        while not port_ready(target.host, target.port):
            if time.monotonic() >= deadline:
                raise RuntimeError(f'{name} 启动后端口 {target.host}:{target.port} 仍未就绪。')
            time.sleep(0.5)
        print(f'[启动检查] {name} 端口已就绪。', flush=True)
