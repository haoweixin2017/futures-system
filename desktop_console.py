"""本机服务桌面控制台；双击“桌面控制台.bat”打开。"""

import json
import os
import re
import subprocess
import threading
import time
import tkinter as tk
import ctypes
from ctypes import wintypes
from pathlib import Path
from tkinter import messagebox, ttk
from urllib.request import urlopen
import webbrowser


ROOT = Path(__file__).resolve().parent
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
LOG = ROOT / "logs" / "desktop-console.log"
URL = "http://127.0.0.1:8000"
NO_WINDOW = subprocess.CREATE_NO_WINDOW
TRAY_MESSAGE = 0x8001  # WM_APP + 1


class NotifyIconData(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD), ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wintypes.HICON),
    ]


user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                            wintypes.WPARAM, wintypes.LPARAM)
user32.GetParent.argtypes = [wintypes.HWND]
user32.GetParent.restype = wintypes.HWND
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
user32.SetWindowLongPtrW.restype = ctypes.c_void_p
user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.CallWindowProcW.restype = ctypes.c_ssize_t
user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
user32.LoadIconW.restype = wintypes.HICON
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NotifyIconData)]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL


def listener_pid():
    result = subprocess.run(
        ["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True,
        creationflags=NO_WINDOW, check=True,
    )
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 5 and fields[1] == "127.0.0.1:8000" and fields[3] == "LISTENING":
            return int(fields[4])
    return None


def healthy():
    try:
        with urlopen(URL + "/api/v1/health", timeout=1) as response:
            return response.status == 200
    except OSError:
        return False


def is_project_server(pid):
    command = f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine | ConvertTo-Json -Compress"
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True, text=True, creationflags=NO_WINDOW, check=True,
    )
    if not result.stdout.strip():
        return False
    command_line = json.loads(result.stdout)
    return bool(re.search(r'(^|[\\/\s\"])run\.py(?=[\s\"]|$)', command_line, re.I))


def start_service():
    pid = listener_pid()
    if pid:
        if healthy() and is_project_server(pid):
            return
        raise RuntimeError("端口 8000 已被占用；请先检查当前服务状态。")
    if not PYTHON.is_file():
        raise RuntimeError(f"未找到虚拟环境：{PYTHON}")
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("ab") as output:
        env = os.environ.copy()
        env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", FUTURES_PROFILE="local")
        process = subprocess.Popen(
            [str(PYTHON), "run.py"], cwd=ROOT, env=env,
            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
            creationflags=NO_WINDOW,
        )
    for _ in range(840):
        if healthy():
            return
        if process.poll() is not None:
            raise RuntimeError(f"启动失败；请查看日志中的依赖服务/授权提示：{LOG}")
        time.sleep(0.5)
    raise RuntimeError(f"服务未在 420 秒内就绪，请查看日志：{LOG}")


def stop_service():
    pid = listener_pid()
    if not pid:
        return
    if not healthy() or not is_project_server(pid):
        raise RuntimeError(f"端口 8000 由 PID {pid} 占用，但无法确认是本项目服务；未结束该进程。")
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                   capture_output=True, text=True, creationflags=NO_WINDOW, check=True)
    for _ in range(20):
        if not listener_pid():
            return
        time.sleep(0.25)
    raise RuntimeError("停止命令已执行，但端口 8000 仍被占用。")


class Console(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("期货模拟交易系统 · 桌面控制台")
        self.geometry("740x470")
        self.minsize(600, 360)
        self.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self.update_idletasks()
        self.tray_hwnd = user32.GetParent(self.winfo_id())
        self.tray_proc = WNDPROC(self.on_window_message)
        self.old_proc = user32.SetWindowLongPtrW(self.tray_hwnd, -4, self.tray_proc)
        self.tray_data = NotifyIconData()
        self.tray_data.cbSize = ctypes.sizeof(NotifyIconData)
        self.tray_data.hWnd = self.tray_hwnd
        self.tray_data.uID = 1
        self.tray_data.uFlags = 1 | 2 | 4  # message, icon, tooltip
        self.tray_data.uCallbackMessage = TRAY_MESSAGE
        self.tray_data.hIcon = user32.LoadIconW(None, ctypes.c_void_p(32512))
        self.tray_data.szTip = "期货模拟交易系统 · 单击打开控制台"
        self.tray_visible = False
        self.busy = False
        self.status = tk.StringVar(value="正在检查服务状态…")

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="本机服务控制", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
        ttk.Label(frame, textvariable=self.status, font=("Microsoft YaHei UI", 11)).pack(anchor="w", pady=(12, 14))
        bar = ttk.Frame(frame)
        bar.pack(fill="x")
        self.buttons = []
        for label, action in (("启动服务", "start"), ("停止服务", "stop"), ("重启服务", "restart")):
            button = ttk.Button(bar, text=label, command=lambda name=action: self.run_action(name))
            button.pack(side="left", padx=(0, 10))
            self.buttons.append(button)
        ttk.Button(bar, text="打开网页", command=lambda: webbrowser.open(URL)).pack(side="left")
        ttk.Button(bar, text="退出控制台", command=self.destroy).pack(side="right")
        ttk.Label(frame, text="点击右上角 X 会收起到右下角通知区域；单击托盘图标可恢复。", foreground="#666666").pack(anchor="w", pady=(10, 0))
        ttk.Label(frame, text=f"服务日志：{LOG}").pack(anchor="w", pady=(22, 6))
        self.log_view = tk.Text(frame, wrap="word", state="disabled", font=("Consolas", 9))
        self.log_view.pack(fill="both", expand=True)
        self.after(100, self.refresh)

    def on_window_message(self, hwnd, message, wparam, lparam):
        if message == TRAY_MESSAGE and lparam == 0x202:  # WM_LBUTTONUP
            self.restore_from_tray()
            return 0
        return user32.CallWindowProcW(self.old_proc, hwnd, message, wparam, lparam)

    def hide_to_tray(self):
        if shell32.Shell_NotifyIconW(0, ctypes.byref(self.tray_data)):
            self.tray_visible = True
            self.withdraw()
        else:
            self.iconify()

    def restore_from_tray(self):
        if self.tray_visible:
            shell32.Shell_NotifyIconW(2, ctypes.byref(self.tray_data))
            self.tray_visible = False
        self.deiconify()
        self.lift()

    def destroy(self):
        if self.tray_visible:
            shell32.Shell_NotifyIconW(2, ctypes.byref(self.tray_data))
            self.tray_visible = False
        user32.SetWindowLongPtrW(self.tray_hwnd, -4, self.old_proc)
        super().destroy()

    def refresh(self):
        self.check_status()
        self.after(3000, self.refresh)

    def check_status(self):
        def check():
            try:
                pid = listener_pid()
                status = (f"运行中 · PID {pid}" if is_project_server(pid) else f"端口被占用 · PID {pid}") if pid else "未运行"
                content = LOG.read_bytes()[-16000:].decode("utf-8", errors="replace") if LOG.exists() else "暂无控制台日志。"
                self.after(0, lambda: self.show(status, content))
            except Exception as exc:
                self.after(0, lambda error=str(exc): self.status.set(f"状态检查失败：{error}"))
        threading.Thread(target=check, daemon=True).start()

    def show(self, status, content):
        if not self.busy:
            self.status.set(status)
        if self.log_view.get("1.0", "end-1c") != content:
            self.log_view.configure(state="normal")
            self.log_view.delete("1.0", "end")
            self.log_view.insert("end", content)
            self.log_view.see("end")
            self.log_view.configure(state="disabled")

    def run_action(self, action):
        if self.busy:
            return
        self.busy = True
        self.status.set({"start": "正在启动…", "stop": "正在停止…", "restart": "正在重启…"}[action])
        for button in self.buttons:
            button.configure(state="disabled")

        def work():
            try:
                if action in ("stop", "restart"):
                    stop_service()
                if action in ("start", "restart"):
                    start_service()
                self.after(0, lambda: self.done(None))
            except Exception as exc:
                self.after(0, lambda error=str(exc): self.done(error))
        threading.Thread(target=work, daemon=True).start()

    def done(self, error):
        self.busy = False
        for button in self.buttons:
            button.configure(state="normal")
        if error:
            messagebox.showerror("服务控制失败", error)
        self.check_status()


if __name__ == "__main__":
    Console().mainloop()
