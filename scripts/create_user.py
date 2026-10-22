"""用户管理命令行工具。

用法 (在项目根目录):
    python scripts/create_user.py list                          # 查看用户
    python scripts/create_user.py add <用户名> <sim|live|admin>  # 新建用户 (交互输入密码)
    python scripts/create_user.py add <用户名> <角色> <密码>     # 新建用户 (命令行密码)
    python scripts/create_user.py passwd <用户名> <新密码>       # 修改密码
    python scripts/create_user.py drop <用户名>                  # 删除用户
"""

import getpass
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import yaml  # noqa: E402

from backend.common.security import hash_password  # noqa: E402

USERS_FILE = PROJECT_ROOT / "config" / "users.yaml"
VALID_ROLES = {"sim", "live", "admin"}
COMMON_WEAK_PASSWORDS = {"123456", "12345678", "password", "admin"}


def validate_password(password: str) -> str:
    if len(password) < 10 or password.lower() in COMMON_WEAK_PASSWORDS:
        raise ValueError("密码至少 10 位，且不能使用常见默认密码")
    return password


def load():
    if USERS_FILE.exists():
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {"users": []}
    return {"users": []}


def save(data):
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    data = load()
    users = {u["username"]: u for u in data.get("users", [])}

    if cmd == "list":
        for u in data.get("users", []):
            print(f"{u['username']:<16} 角色={u['role']:<6} 名称={u.get('display_name', '')}")
        if not users:
            print("(无用户)")
        return

    if cmd == "add":
        if len(sys.argv) < 4:
            print("用法: create_user.py add <用户名> <sim|live|admin> [密码]")
            return
        username, role = sys.argv[2], sys.argv[3]
        if role not in VALID_ROLES:
            print(f"角色必须是 {'/'.join(VALID_ROLES)}")
            return
        if username in users:
            print(f"用户 {username} 已存在, 如需重置请用 passwd")
            return
        password = sys.argv[4] if len(sys.argv) > 4 else getpass.getpass("设置密码: ")
        try:
            password = validate_password(password)
        except ValueError as exc:
            print(exc)
            return
        data["users"].append({
            "username": username,
            "role": role,
            "display_name": {"sim": "模拟用户", "live": "正式用户", "admin": "管理员"}[role],
            "password_hash": hash_password(password),
        })
        save(data)
        print(f"用户 {username} ({role}) 创建成功")
        return

    if cmd == "passwd":
        if len(sys.argv) < 3:
            print("用法: create_user.py passwd <用户名> [新密码]")
            return
        username = sys.argv[2]
        if username not in users:
            print(f"用户 {username} 不存在")
            return
        password = sys.argv[3] if len(sys.argv) > 3 else getpass.getpass("新密码: ")
        try:
            password = validate_password(password)
        except ValueError as exc:
            print(exc)
            return
        users[username]["password_hash"] = hash_password(password)
        data["users"] = list(users.values())
        save(data)
        print(f"用户 {username} 密码已更新")
        return

    if cmd == "drop":
        if len(sys.argv) < 3:
            print("用法: create_user.py drop <用户名>")
            return
        username = sys.argv[2]
        if username not in users:
            print(f"用户 {username} 不存在")
            return
        data["users"] = [u for u in data["users"] if u["username"] != username]
        save(data)
        print(f"用户 {username} 已删除")
        return

    print(__doc__)


if __name__ == "__main__":
    main()
