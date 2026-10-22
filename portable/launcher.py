"""离线安装版桌面启动器：管理内置 MySQL、Redis 与 Web 服务。"""

import ctypes
import hashlib
import json
import os
import secrets
import socket
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk
from urllib.request import urlopen
import webbrowser

import yaml


ROOT = Path(__file__).resolve().parent
APP_DIR = ROOT / "app"
RUNTIME_DIR = ROOT / "runtime"
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
PYTHON = RUNTIME_DIR / "python" / "python.exe"
MYSQLD = RUNTIME_DIR / "mysql" / "bin" / "mysqld.exe"
REDIS_SERVER = RUNTIME_DIR / "redis" / "redis-server.exe"
MYSQL_DATA = DATA_DIR / "mysql"
REDIS_DATA = DATA_DIR / "redis"
PID_FILE = DATA_DIR / "runtime-pids.json"
URL = "http://127.0.0.1:8000"
NO_WINDOW = subprocess.CREATE_NO_WINDOW
PORTS = {"app": 8000, "mysql": 3307, "redis": 6380}


def mysql_path(path):
    """MySQL 组件加载对非 ASCII 路径不稳定，优先使用 Windows 短路径。"""
    path = str(Path(path).resolve())
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetShortPathNameW(path, buffer, len(buffer))
    return buffer.value if length and length < len(buffer) else path


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def wait_port(port, opened=True, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_open(port) is opened:
            return True
        time.sleep(0.25)
    return False


def healthy():
    try:
        with urlopen(URL + "/api/v1/health", timeout=1) as response:
            return response.status == 200
    except OSError:
        return False


def process_command(pid):
    command = (
        f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine "
        "| ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True, text=True, creationflags=NO_WINDOW,
    )
    try:
        return json.loads(result.stdout) if result.stdout.strip() else ""
    except json.JSONDecodeError:
        return ""


def load_pids():
    try:
        return json.loads(PID_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_pids(pids):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(json.dumps(pids), encoding="utf-8")


def terminate_owned(name, expected_fragment):
    pid = load_pids().get(name)
    if not pid:
        return
    command = process_command(pid)
    if expected_fragment.lower() not in command.lower():
        return
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        capture_output=True, text=True, creationflags=NO_WINDOW,
    )


def start_process(name, command, cwd, log_name, env=None):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = (LOG_DIR / log_name).open("ab")
    process = subprocess.Popen(
        [str(part) for part in command], cwd=cwd, env=env,
        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        creationflags=NO_WINDOW,
    )
    pids = load_pids()
    pids[name] = process.pid
    save_pids(pids)
    return process.pid


def initialize_mysql():
    marker = MYSQL_DATA / "mysql"
    if marker.is_dir():
        return
    MYSQL_DATA.mkdir(parents=True, exist_ok=True)
    base_dir = mysql_path(MYSQLD.parent.parent)
    data_dir = mysql_path(MYSQL_DATA)
    result = subprocess.run(
        [str(MYSQLD), "--no-defaults", "--initialize-insecure",
         f"--basedir={base_dir}", f"--datadir={data_dir}",
         f"--plugin-dir={base_dir}\\lib\\plugin"],
        cwd=ROOT, capture_output=True, text=True, creationflags=NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError("内置 MySQL 初始化失败：\n" + (result.stderr or result.stdout)[-2000:])


def start_mysql():
    if port_open(PORTS["mysql"]):
        raise RuntimeError(f"端口 {PORTS['mysql']} 已被其他程序占用。")
    initialize_mysql()
    base_dir = mysql_path(MYSQLD.parent.parent)
    data_dir = mysql_path(MYSQL_DATA)
    start_process(
        "mysql",
        [MYSQLD, "--no-defaults", f"--basedir={base_dir}",
         f"--datadir={data_dir}", f"--plugin-dir={base_dir}\\lib\\plugin",
         f"--port={PORTS['mysql']}",
         "--bind-address=127.0.0.1", "--mysqlx=0", "--skip-log-bin",
         "--character-set-server=utf8mb4", "--console"],
        ROOT, "mysql.log",
    )
    if not wait_port(PORTS["mysql"], timeout=45):
        raise RuntimeError("内置 MySQL 未能启动，请查看 logs/mysql.log。")
    env = runtime_env()
    code = (
        "import pymysql; "
        f"c=pymysql.connect(host='127.0.0.1',port={PORTS['mysql']},user='root',password='',autocommit=True); "
        "c.cursor().execute('CREATE DATABASE IF NOT EXISTS citic_futures CHARACTER SET utf8mb4'); c.close()"
    )
    result = subprocess.run([str(PYTHON), "-c", code], cwd=APP_DIR, env=env,
                            capture_output=True, text=True, creationflags=NO_WINDOW)
    if result.returncode:
        raise RuntimeError("初始化应用数据库失败：" + (result.stderr or result.stdout)[-1000:])


def start_redis():
    if port_open(PORTS["redis"]):
        raise RuntimeError(f"端口 {PORTS['redis']} 已被其他程序占用。")
    REDIS_DATA.mkdir(parents=True, exist_ok=True)
    start_process(
        "redis",
        [REDIS_SERVER, "--port", str(PORTS["redis"]), "--bind", "127.0.0.1",
         "--protected-mode", "yes", "--dir", REDIS_DATA, "--dbfilename", "dump.rdb",
         "--appendonly", "yes"],
        ROOT, "redis.log",
    )
    if not wait_port(PORTS["redis"], timeout=15):
        raise RuntimeError("内置 Redis 未能启动，请查看 logs/redis.log。")


def runtime_env():
    env = os.environ.copy()
    env.update(
        PYTHONUTF8="1", PYTHONIOENCODING="utf-8", FUTURES_PROFILE="local",
        FUTURES_MYSQL_HOST="127.0.0.1", FUTURES_MYSQL_PORT=str(PORTS["mysql"]),
        FUTURES_MYSQL_USER="root", FUTURES_REDIS_HOST="127.0.0.1",
        FUTURES_REDIS_PORT=str(PORTS["redis"]),
    )
    return env


def start_service():
    if healthy():
        return
    if port_open(PORTS["app"]):
        raise RuntimeError("端口 8000 已被其他程序占用。")
    for path in (PYTHON, MYSQLD, REDIS_SERVER, APP_DIR / "run.py"):
        if not path.is_file():
            raise RuntimeError(f"安装文件缺失：{path}")
    started = []
    try:
        start_mysql()
        started.append(("mysql", "mysqld"))
        start_redis()
        started.append(("redis", "redis-server"))
        start_process("app", [PYTHON, "run.py"], APP_DIR, "backend.log", runtime_env())
        started.append(("app", "run.py"))
        deadline = time.time() + 30
        while time.time() < deadline:
            if healthy():
                return
            time.sleep(0.5)
        raise RuntimeError("Web 服务未在 30 秒内就绪，请查看 logs/backend.log。")
    except Exception:
        for name, fragment in reversed(started):
            terminate_owned(name, fragment)
        raise


def stop_service():
    terminate_owned("app", "run.py")
    wait_port(PORTS["app"], opened=False, timeout=10)
    terminate_owned("redis", "redis-server")
    terminate_owned("mysql", "mysqld")
    wait_port(PORTS["redis"], opened=False, timeout=10)
    wait_port(PORTS["mysql"], opened=False, timeout=20)
    save_pids({})


def users_file():
    return APP_DIR / "config" / "users.yaml"


def has_user():
    try:
        data = yaml.safe_load(users_file().read_text(encoding="utf-8")) or {}
        return bool(data.get("users"))
    except OSError:
        return False


def create_first_user(parent):
    if has_user():
        return True
    messagebox.showinfo("首次运行", "请创建一个本机模拟交易账号。", parent=parent)
    username = simpledialog.askstring("创建账号", "用户名：", parent=parent)
    if not username or not username.strip():
        return False
    password = simpledialog.askstring("创建账号", "密码（至少 10 位）：", show="*", parent=parent)
    if not password or len(password) < 10:
        messagebox.showerror("创建失败", "密码至少需要 10 位。", parent=parent)
        return False
    repeated = simpledialog.askstring("创建账号", "再次输入密码：", show="*", parent=parent)
    if password != repeated:
        messagebox.showerror("创建失败", "两次输入的密码不一致。", parent=parent)
        return False
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 120_000)
    payload = {"users": [{
        "username": username.strip(), "role": "sim", "display_name": "模拟用户",
        "password_hash": f"pbkdf2$120000${salt.hex()}${digest.hex()}",
    }]}
    users_file().parent.mkdir(parents=True, exist_ok=True)
    users_file().write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
    secret_file = APP_DIR / "config" / "session_secret.key"
    secret_file.write_text(secrets.token_hex(32), encoding="utf-8")
    return True


class Console(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("期货模拟交易系统")
        self.geometry("760x500")
        self.minsize(620, 380)
        self.busy = False
        self.status = tk.StringVar(value="正在检查运行状态…")
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="期货模拟交易系统", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
        ttk.Label(frame, textvariable=self.status, font=("Microsoft YaHei UI", 11)).pack(anchor="w", pady=(12, 14))
        bar = ttk.Frame(frame)
        bar.pack(fill="x")
        self.buttons = []
        for label, action in (("启动", "start"), ("停止", "stop"), ("重启", "restart")):
            button = ttk.Button(bar, text=label, command=lambda value=action: self.run_action(value))
            button.pack(side="left", padx=(0, 10))
            self.buttons.append(button)
        ttk.Button(bar, text="打开网页", command=lambda: webbrowser.open(URL)).pack(side="left")
        ttk.Button(bar, text="退出", command=self.destroy).pack(side="right")
        ttk.Label(frame, text=f"数据目录：{DATA_DIR}").pack(anchor="w", pady=(20, 4))
        ttk.Label(frame, text=f"日志目录：{LOG_DIR}").pack(anchor="w", pady=(0, 8))
        self.log_view = tk.Text(frame, wrap="word", state="disabled", font=("Consolas", 9))
        self.log_view.pack(fill="both", expand=True)
        self.after(100, self.refresh)

    def refresh(self):
        status = "运行中" if healthy() else "未运行"
        self.status.set(status if not self.busy else self.status.get())
        log_file = LOG_DIR / "backend.log"
        content = log_file.read_bytes()[-16000:].decode("utf-8", errors="replace") if log_file.exists() else "暂无日志。"
        self.log_view.configure(state="normal")
        self.log_view.delete("1.0", "end")
        self.log_view.insert("end", content)
        self.log_view.see("end")
        self.log_view.configure(state="disabled")
        self.after(3000, self.refresh)

    def run_action(self, action):
        if self.busy:
            return
        if action in ("start", "restart") and not create_first_user(self):
            return
        self.busy = True
        self.status.set({"start": "正在启动…", "stop": "正在停止…", "restart": "正在重启…"}[action])
        for button in self.buttons:
            button.configure(state="disabled")

        def work():
            error = None
            try:
                if action in ("stop", "restart"):
                    stop_service()
                if action in ("start", "restart"):
                    start_service()
            except Exception as exc:
                error = str(exc)
            self.after(0, lambda: self.done(error, action))

        threading.Thread(target=work, daemon=True).start()

    def done(self, error, action):
        self.busy = False
        for button in self.buttons:
            button.configure(state="normal")
        if error:
            messagebox.showerror("操作失败", error, parent=self)
        elif action in ("start", "restart"):
            webbrowser.open(URL)
        self.status.set("运行中" if healthy() else "未运行")


if __name__ == "__main__":
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("FuturesTradingSystem.Portable")
    Console().mainloop()
