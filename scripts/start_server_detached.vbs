' 以「完全脱离当前会话」的方式启动期货交易系统服务。
' 用途: 避免被调用方进程退出时连带杀掉子进程 (nohup/& 在非交互环境下会被回收)。
' 生成一个隐藏窗口运行 uvicorn, 并把 stdout/stderr 重定向到日志文件。

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

base = "D:\期货实时交易系统"
python = base & "\.venv\Scripts\python.exe"
logFile = base & "\backups\server.log"

' 0 = 隐藏窗口, False = 不等待返回
cmd = "cmd /c """ & python & """ -m uvicorn backend.main:app --app-dir """ & base & """ --host 0.0.0.0 --port 8000 >> """ & logFile & """ 2>&1"

shell.CurrentDirectory = base
shell.Run cmd, 0, False
