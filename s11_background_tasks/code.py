#!/usr/bin/env python3
"""
s11_background_tasks.py - Background Tasks
（s11_background_tasks.py - 后台任务）

    Main thread                              Background thread
    +------------------------------+         +----------------------+
    | bash(run_in_background=True) | ------> | run command          |
    | return bg_id                 |         | queue result         |
    | continue agent loop          | <------ +----------------------+
    | next turn: collect           |
    +------------------------------+
    （主线程 vs 后台线程：
      模型把某条 bash 标记为「后台执行」后，主线程不干等，
      只拿回一个后台任务 ID（如 bg_0001）就继续往下跑 agent 循环；
      真正的命令在后台线程里执行，结果先入队，
      等**下一轮**再被主线程收集、以通知的形式塞回对话。
      这样耗时的命令就不会把整个循环卡住。

      更详细的英文说明、图示和用法见同目录 README.md / README.zh.md。）
"""

import atexit      # 注册进程退出时的清理函数
import glob        # 文件名模式匹配（glob 工具用）
import os          # 环境变量、平台判断
import re          # 正则：危险命令匹配
import signal      # 信号处理：进程被终止时清理子进程
import subprocess  # 启动子进程执行 shell 命令
import threading   # 线程：后台任务在独立线程里跑；用锁保护共享状态
import time        # 短暂 sleep，给进程留一点退出时间
from pathlib import Path  # 面向对象的路径操作

try:
    import readline

    # 以下四行是 macOS 上 libedit 库的 UTF-8 修复，让终端能正确处理中文。
    readline.parse_and_bind("set bind-tty-special-chars off")
    readline.parse_and_bind("set input-meta on")
    readline.parse_and_bind("set output-meta on")
    readline.parse_and_bind("set convert-meta off")
except ImportError:
    # Windows 自带 Python 没有 readline，跳过即可，不影响主逻辑。
    pass

from anthropic import Anthropic  # Anthropic 官方 Python SDK 客户端
from dotenv import load_dotenv   # 从 .env 文件加载环境变量

# override=True：即使系统里已有同名环境变量，也用 .env 里的值覆盖。
load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    # 配了自定义端点时移除可能冲突的 ANTHROPIC_AUTH_TOKEN，避免两种鉴权打架。
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()  # 工作目录：文件工具与路径校验的基准
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]  # 从环境变量读模型 ID

# 根据操作系统告诉模型它面对的 shell 语法，避免生成错误的命令风格。
ENVIRONMENT_PROMPT = (
    "Windows: the bash tool runs through cmd.exe; use cmd.exe syntax, not Unix "
    "Bash or PowerShell syntax, and prefer dedicated file tools for file operations"
    if os.name == "nt"
    else "Unix-like: the bash tool runs the system shell"
)
# 系统提示词里多了一句关键约定：只有「相互独立」的 bash 命令才允许放后台。
# 因为后台命令不阻塞主线程，若几件事本有先后依赖，放后台就会乱序执行。
SYSTEM = (
    f"You are a coding agent at {WORKDIR}. Environment: {ENVIRONMENT_PROMPT}. "
    "Use tools to solve tasks. "
    "Set run_in_background to true only for independent Bash commands."
)


# -- From s04: tool implementations --
# 工具实现。本课在 s04 基础上重写了 bash：改用 Popen 管理子进程，
# 并加了一套「进程登记簿 + 退出清理」，保证进程退出时不会留下野进程。

# 进程登记簿：登记当前所有活着的 shell 子进程，退出时统一收尸。
_shell_processes: set[subprocess.Popen] = set()
# 保护登记簿的锁。用 RLock（可重入锁）：同一线程可以重复加锁而不死锁。
_shell_process_lock = threading.RLock()


def _stop_process_group(process: subprocess.Popen):
    """Stop a shell process and its children (cross-platform).

    POSIX uses process-group signals (SIGTERM then SIGKILL). Windows has
    neither ``os.killpg`` nor ``signal.SIGKILL``, so it falls back to
    ``Popen.terminate()`` / ``Popen.kill()``.
    """
    # 停止一个 shell 进程及其子进程（跨平台）。
    # 为什么连子进程一起杀？因为用 shell=True 启动时，真正干活的可能是
    # shell 派生出来的子进程；只杀 shell 会留下野进程。
    if os.name == "nt":
        # Windows 没有进程组信号，只能退而求其次：先 terminate（温和），
        # 不行再 kill（强制）。
        for stop in (process.terminate, process.kill):
            if process.poll() is not None:
                return  # 进程已经退出了
            try:
                stop()
            except OSError:
                return  # 进程已不存在
            try:
                process.wait(timeout=0.05)  # 给它一点时间退出
            except subprocess.TimeoutExpired:
                continue  # 还没退，换更强硬的手段
        return

    # SIGKILL is POSIX-only; fall back to SIGTERM on platforms without it.
    # POSIX：给整个进程组发信号——先 SIGTERM（客气），再 SIGKILL（强杀）。
    # 若平台没有 SIGKILL，就退回用 SIGTERM。
    for sig in (signal.SIGTERM, getattr(signal, "SIGKILL", signal.SIGTERM)):
        try:
            os.killpg(process.pid, sig)  # 发给「进程组」（killpg 内部处理组号）
        except (ProcessLookupError, OSError):
            return  # 进程组已不存在
        time.sleep(0.05)


def _stop_all_shell_processes():
    # 把所有登记在册的 shell 进程全部停掉（退出时调用）。
    with _shell_process_lock:
        processes = list(_shell_processes)  # 先复制一份，避免边遍历边改动
    for process in processes:
        _stop_process_group(process)


def _handle_termination_signal(signum, _frame):
    # 收到 SIGTERM 时：先清理所有子进程，再退出。
    # 退出码用 128 + 信号号，这是 Unix 的惯例（进程被信号 N 杀死时退出码为 128+N）。
    _stop_all_shell_processes()
    raise SystemExit(128 + signum)


atexit.register(_stop_all_shell_processes)  # 正常退出时也清理
signal.signal(signal.SIGTERM, _handle_termination_signal)  # 被 SIGTERM 杀掉时清理


def _run_bash_process(command: str) -> tuple[str, int | None]:
    # 执行一条 shell 命令，返回 (输出文本, 退出码)；超时或启动失败时退出码为 None。
    process = None
    try:
        # start_new_session (setsid) exists only on POSIX; skip it on Windows.
        # POSIX 上设 start_new_session=True，让子进程另起一个会话/进程组，
        # 这样就能用 killpg 一次性把整组（含孙进程）都杀掉；Windows 不支持，故跳过。
        popen_kwargs = {} if os.name == "nt" else {"start_new_session": True}
        process = subprocess.Popen(
            command,
            shell=True,              # 交给系统 shell 解析（支持管道、重定向等）
            cwd=WORKDIR,             # 固定在工作目录下执行
            stdout=subprocess.PIPE,  # 捕获标准输出
            stderr=subprocess.PIPE,  # 捕获标准错误
            text=True, errors="replace",  # 按文本返回；解码失败也不崩
            **popen_kwargs,
        )
        with _shell_process_lock:
            _shell_processes.add(process)  # 登记，便于退出时清理
        stdout, stderr = process.communicate(timeout=120)  # 等它跑完（最多 120 秒）
        output = (stdout + stderr).strip()
        return (output[:50000] if output else "(no output)"), process.returncode
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)", None
    except OSError as error:
        return f"Error: {type(error).__name__}: {error}", None
    finally:
        # 无论成功还是失败，都要把进程收干净、并从登记簿里移除。
        if process is not None:
            _stop_process_group(process)
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass
            with _shell_process_lock:
                _shell_processes.discard(process)


def _format_bash_result(output: str, exit_code: int | None) -> str:
    # 统一格式化输出：退出码为 0 或未知（None）时原样返回；
    # 非 0 说明命令失败了，就在输出前面加一行错误提示，让模型能察觉。
    if exit_code in (0, None):
        return output
    return f"Error: command exited with status {exit_code}\n{output}"


def run_bash(command: str, run_in_background: bool = False) -> str:
    # bash 工具的入口。注意 run_in_background 参数在这里其实用不到——
    # 「是否放后台」的判断在 execute_tool 里由 should_run_background 完成，
    # 能走到这里的都一定是「前台执行」，所以直接同步跑完返回即可。
    return _format_bash_result(*_run_bash_process(command))


def run_read(path: str, limit: int | None = None) -> str:
    # 读文件：相对路径拼到工作目录后 resolve（折叠 ../ 等）。
    try:
        file_path = (WORKDIR / path).resolve()
        lines = file_path.read_text(encoding="utf-8").splitlines()
        if limit and limit < len(lines):
            # 只读前 limit 行，并附上还剩多少行的提示。
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as error:
        return f"Error: {error}"  # 出错转成文本交给模型，不打断循环


def run_write(path: str, content: str) -> str:
    # 写文件：整文件覆盖；parents=True 按需建目录，exist_ok=True 已存在也不报错。
    try:
        file_path = (WORKDIR / path).resolve()
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as error:
        return f"Error: {error}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    # 编辑文件：只替换第一处精确匹配（最后的参数 1），比整体重写更安全省 token。
    try:
        file_path = (WORKDIR / path).resolve()
        text = file_path.read_text(encoding="utf-8")
        if old_text not in text:
            return f"Error: text not found in {path}"  # 找不到原文就报错
        file_path.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {path}"
    except Exception as error:
        return f"Error: {error}"


def run_glob(pattern: str) -> str:
    # 查找文件：集合去重 + 排序保证输出稳定；** 表示递归匹配。
    try:
        matches = sorted({
            match
            for match in glob.glob(pattern, root_dir=WORKDIR, recursive=True)
            # 只保留确实在工作目录内的结果，防止越界。
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR)
        })
        shown = matches[:200]  # 最多 200 条，防刷屏和吃掉上下文
        if len(matches) > 200:
            shown.append("... (more matches omitted; narrow the pattern)")
        return "\n".join(shown) if shown else "(no matches)"
    except Exception as error:
        return f"Error: {error}"


# 工具定义：注意 bash 多了一个 run_in_background 布尔参数——这是本课的核心开关。
TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object",
                      "properties": {
                          "command": {"type": "string"},
                          "run_in_background": {"type": "boolean"}},
                      "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "limit": {"type": "integer"}},
                      "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "old_text": {"type": "string"},
                                     "new_text": {"type": "string"}},
                      "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern; ** matches recursively.",
     "input_schema": {"type": "object",
                      "properties": {"pattern": {"type": "string"}},
                      "required": ["pattern"]}},
]

# 分发表：工具名 -> 处理函数。
TOOL_HANDLERS = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
}


# -- From s04: hooks and permission checks --
# 钩子系统与 s04 相同：在关键节点插入回调，实现权限控制、日志、统计等横切逻辑。

# 四个事件各对应一个回调列表。
HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}


def register_hook(event: str, callback):
    # 注册：把回调追加到对应事件的列表。
    HOOKS[event].append(callback)


def trigger_hooks(event: str, *args):
    # 触发：依次调用回调；某个回调返回非 None 就视为拦截，立即停止并返回该结果。
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None


DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]  # 硬黑名单：命中直接拒
DESTRUCTIVE_COMMAND_WORD = re.compile(
    # 匹配作为独立命令出现的 rm/del（前置边界避免误伤 "firm" 这类子串）
    r"(?i)(?:^|[;&|()\n])\s*(?:rm|del)(?=\s|$|[;&|()])"
)
DESTRUCTIVE = ["rm ", "> /etc/", "chmod 777"]  # 其他危险特征（需人工确认）


def contains_destructive_command(command: str) -> bool:
    return bool(DESTRUCTIVE_COMMAND_WORD.search(command))


def permission_hook(block):
    # 工具执行前：bash 先查硬黑名单（命中直接拒绝），再查危险特征（询问用户）；
    # 文件工具则检查路径是否越界。
    if block.name == "bash":
        command = block.input.get("command", "")
        for pattern in DENY_LIST:
            if pattern in command:
                print(f"\n\033[31m[blocked] '{pattern}'\033[0m")  # 红色提示
                return "Permission denied by deny list"
        if contains_destructive_command(command) or any(
            keyword in command for keyword in DESTRUCTIVE
        ):
            print("\n\033[33m[permission] Potentially destructive command\033[0m")
            print(f"   Tool: {block.name}({block.input})")
            choice = input("   Allow? [y/N] ").strip().lower()
            if choice not in ("y", "yes"):  # 默认拒绝（直接回车 = N）
                return "Permission denied by user"

    if block.name in ("read_file", "write_file", "edit_file"):
        # 文件工具：路径解析后落在工作目录之外，就询问用户是否放行。
        path = block.input.get("path", "")
        if not (WORKDIR / path).resolve().is_relative_to(WORKDIR):
            print("\n\033[33m[permission] Access outside workspace\033[0m")
            print(f"   Tool: {block.name}({block.input})")
            choice = input("   Allow? [y/N] ").strip().lower()
            if choice not in ("y", "yes"):
                return "Permission denied by user"
    return None


def log_hook(block):
    # 只观测不干预：灰色打印工具名与前两个参数（截断 60 字符），返回 None 放行。
    preview = str(list(block.input.values())[:2])[:60]
    print(f"\033[90m[HOOK] {block.name}({preview})\033[0m")
    return None


def large_output_hook(block, output):
    # 执行后告警：输出超过 10 万字符就提醒一句，不修改结果。
    if len(str(output)) > 100000:
        print(
            f"\033[33m[HOOK] Large output from {block.name}: "
            f"{len(str(output))} chars\033[0m"
        )
    return None


def context_inject_hook(query: str):
    # 用户提交提示词时触发：打印一下当前工作目录。
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {WORKDIR}\033[0m")
    return None


def summary_hook(messages: list):
    # 会话结束（Stop）时触发：统计本次会话用了几次工具调用并打印。
    tool_count = sum(
        1
        for message in messages
        for block in (
            message.get("content")
            if isinstance(message.get("content"), list)
            else []  # content 不是列表，就当作没有内容块
        )
        if isinstance(block, dict) and block.get("type") == "tool_result"
    )
    print(f"\033[90m[HOOK] Stop: session used {tool_count} tool calls\033[0m")
    return None


# 注册回调。
register_hook("UserPromptSubmit", context_inject_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", summary_hook)


def call_tool(block) -> str:
    # 真正执行一个工具：查分发表、按关键字展开参数调用处理函数。
    # 注意本课特意把它从 execute_tool 里拆出来——因为 execute_tool 现在还多了
    # 一步「判断要不要放后台」，拆开后职责更清晰。
    handler = TOOL_HANDLERS.get(block.name)
    try:
        # **block.input 按关键字展开参数字典；未知工具给出提示。
        output = handler(**block.input) if handler else f"Unknown: {block.name}"
    except Exception as error:
        output = f"Error: {error}"  # 工具抛异常不打断循环，转成文本交给模型
    return str(output)


# -- New in s11: background execution --
# s11 新增：后台执行。核心是一个「后台任务管理器」，
# 让耗时的 bash 命令在独立线程里跑，主循环不必干等。

class BackgroundManager:
    # 后台任务管理器：负责启动后台任务、记录状态、收集结果。

    def __init__(self):
        self.tasks: dict[str, dict] = {}   # task_id -> 任务元信息（命令、状态…）
        self.results: dict[str, str] = {}  # task_id -> 执行结果文本
        self._ready: list[str] = []        # 已完成、等待被收集的 task_id 队列
        self._counter = 0                  # 自增计数器，用来生成 task_id
        self._lock = threading.Lock()      # 锁：保护上面这些共享状态（多线程会并发访问）

    def start(self, block) -> str:
        # 启动一个后台任务，返回 task_id（形如 bg_0001）。
        if block.name != "bash":
            raise ValueError("Only Bash commands can run in the background")
        command = block.input.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("Bash command cannot be empty")

        with self._lock:
            self._counter += 1
            task_id = f"bg_{self._counter:04d}"  # 补零成 4 位，如 bg_0001
            self.tasks[task_id] = {
                "tool_use_id": block.id,
                "command": command,
                "status": "running",  # 刚启动，状态是「运行中」
            }

        # daemon=True：主程序退出时这个线程会被强制结束，不会拖住整个进程。
        thread = threading.Thread(
            target=self._run,
            args=(task_id, command),
            daemon=True,
        )
        try:
            thread.start()
        except Exception:
            # 线程都没能启动：把刚登记的任务撤掉，避免留下"僵尸"记录。
            with self._lock:
                self.tasks.pop(task_id, None)
            raise
        print(f"  [background] started {task_id}: {command[:60]}")
        return task_id

    def _run(self, task_id: str, command: str):
        # 后台线程的入口：真正执行命令，然后把结果和状态记下来。
        try:
            output, exit_code = _run_bash_process(command)
            result = _format_bash_result(output, exit_code)
            status = "completed" if exit_code == 0 else "failed"
        except Exception as error:
            result = f"Error: {type(error).__name__}: {error}"
            status = "failed"

        with self._lock:
            task = self.tasks.get(task_id)
            if task is None:
                return  # 任务已被移除（比如被取消），不再处理
            task["status"] = status
            self.results[task_id] = result
            self._ready.append(task_id)  # 标记为「可以收集了」

    def collect(self) -> list[str]:
        # 收集所有已完成的后台任务结果，打包成给模型看的「通知」文本。
        # 只有在 collect 时才会真正把结果从字典里取出（并移除），避免重复上报。
        with self._lock:
            ready = []
            for task_id in self._ready:
                task = self.tasks.pop(task_id, None)
                result = self.results.pop(task_id, "")
                if task is not None:
                    ready.append((task_id, task, result))
            self._ready.clear()

        notifications = []
        for task_id, task, result in ready:
            # 用类似 XML 的结构包裹，让模型一眼认出「这是一条后台任务完成通知」。
            notifications.append(
                f"<task_notification>\n"
                f"  <task_id>{task_id}</task_id>\n"
                f"  <status>{task['status']}</status>\n"
                f"  <command>{task['command']}</command>\n"
                f"  <summary>{result[:500]}</summary>\n"
                f"</task_notification>"
            )
            print(f"  [background] collected {task_id}: {task['status']}")
        return notifications


BACKGROUND = BackgroundManager()  # 模块级单例
background_tasks = BACKGROUND.tasks      # 暴露内部字典，便于观察/调试
background_results = BACKGROUND.results


def should_run_background(tool_name: str, tool_input: dict) -> bool:
    # 该不该放后台：仅当是 bash，且模型显式传了 run_in_background=True。
    return (
        tool_name == "bash"
        and tool_input.get("run_in_background") is True
    )


def start_background_task(block) -> str:
    return BACKGROUND.start(block)


def collect_background_results() -> list[str]:
    return BACKGROUND.collect()


def inject_background_results(messages: list) -> int:
    # 把已完成的后台任务通知注入到对话里，返回注入了几条。
    notifications = collect_background_results()
    if not notifications:
        return 0

    blocks = [{"type": "text", "text": item} for item in notifications]
    # 通常要把通知并入「最后一条用户消息」，但要小心别把整条历史搞坏：
    if messages and messages[-1].get("role") == "user":
        content = messages[-1].get("content", "")
        if isinstance(content, list):
            content.extend(blocks)       # 内容本来就是块列表，直接追加
        else:
            messages[-1]["content"] = [  # 原来是纯文本，先包成块列表再追加
                {"type": "text", "text": str(content)},
                *blocks,
            ]
    else:
        # 最后一条不是 user（比如历史为空），就新起一条 user 消息承载通知。
        messages.append({"role": "user", "content": blocks})
    return len(notifications)


def execute_tool(block) -> str:
    # 执行工具：先过 PreToolUse 钩子 → 判断要不要放后台 → 执行 → 过 PostToolUse。
    blocked = trigger_hooks("PreToolUse", block)
    if blocked is not None:
        return str(blocked)  # 被拦截：把拦截原因当作结果返回

    if should_run_background(block.name, block.input):
        # 放后台：立刻返回一个「已启动」的占位结果，真正的输出稍后再收集。
        try:
            task_id = start_background_task(block)
            output = (
                f"[Background task {task_id} started] "
                "The result will be collected on a later turn."
            )
        except Exception as error:
            output = f"Error: {error}"
    else:
        output = call_tool(block)  # 前台：同步执行，直接拿到结果

    trigger_hooks("PostToolUse", block, output)
    return output


# -- Agent loop --

def agent_loop(messages: list):
    while True:
        # 【关键】每轮调用模型之前，先把后台任务的完成通知注入对话。
        # 放在这里是因为：后台结果只可能在「下一轮」被看到，
        # 而且必须在调用模型之前塞进去，模型才有机会读到它。
        inject_background_results(messages)
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=TOOLS,
            max_tokens=8000,
        )
        # 把模型回复追加为 assistant 消息。
        messages.append({"role": "assistant", "content": response.content})

        # 筛出 tool_use 块；没有则这一轮结束。
        tool_calls = [
            block for block in response.content if block.type == "tool_use"
        ]
        if not tool_calls:
            # 触发 Stop 钩子；若返回内容则当作新的用户消息继续循环。
            force = trigger_hooks("Stop", messages)
            if force:
                messages.append({"role": "user", "content": force})
                continue
            return

        # 逐个执行工具调用并收集结果。
        results = []
        for block in tool_calls:
            output = execute_tool(block)
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": output,
            })
        # 把结果作为 user 消息送回，进入下一轮。
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    print("s11: Background Tasks - explicit background Bash execution")
    print("Enter a question, press Enter to send. Type q to quit.\n")

    history = []  # 对话历史，由 agent_loop 就地增删
    while True:   # 外层交互循环：反复读用户输入，直到用户退出
        try:
            # \001/\002 tell Readline the ANSI escapes have zero display width.
            # （\001/\002 告诉 readline 中间的 ANSI 转义码不占显示宽度，提示符才不会错位）
            query = input("\001\033[36m\002s11 >> \001\033[0m\002")
        except (EOFError, KeyboardInterrupt):
            break  # Ctrl+D（EOF）或 Ctrl+C（中断）时退出
        if query.strip().lower() in ("q", "exit", ""):
            break
        # 触发 UserPromptSubmit 钩子（本课只是打印工作目录）。
        trigger_hooks("UserPromptSubmit", query)
        history.append({"role": "user", "content": query})
        agent_loop(history)
        # 打印模型最后的文字回复（跳过 tool_use 块）。
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(block.text)
        print()  # 空行，让下一条提示符清爽地开始
