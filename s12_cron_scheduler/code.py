#!/usr/bin/env python3
"""
s12_cron_scheduler.py - Cron Scheduler
（s12_cron_scheduler.py - Cron 定时调度器）

    +--------------------------+   09:00   +-----------------------+
    | 0 9 * * *               | --------> | [Scheduled] run tests |
    | prompt: "run tests"      |           +-----------+-----------+
    +--------------------------+                       |
          scheduled_jobs                    cron_queue | agent idle
                                                        v
                                                +-------------+
                                                | Agent Loop  |
                                                +-------------+
    （图示说明：
      左边是「任务表」scheduled_jobs，登记着每个定时任务(cron 表达式 + 要执行的 prompt)。
      后台的调度线程每秒检查一次时间，命中整分钟时把任务丢进「待办队列」cron_queue，
      并打印 [Scheduled] run tests 这样的到点提示。
      真正执行不是调度线程干的——它把任务排队，等主线程上的 Agent Loop 空闲时
      （或下一轮输入时）才取出来执行，避免多线程同时驱动同一个对话历史。

      更详细的英文说明、图示和用法见同目录 README.md / README.zh.md。）
"""

import glob        # 文件名模式匹配（glob 工具用）
import json        # 读写 .scheduled_tasks.json：持久化任务的序列化
import os          # 环境变量、进程号、平台判断
import re          # 正则：危险命令匹配
import secrets     # 生成随机任务 ID（cron_xxxxxxxx）
import subprocess  # 启动子进程执行 shell 命令
import threading   # 线程：调度线程、队列处理线程，以及各种锁
from dataclasses import asdict, dataclass  # asdict 把 Job 转成 dict 以便存 JSON
from datetime import datetime              # 取当前本地时间，判断是否到点
from pathlib import Path                   # 面向对象的路径操作

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
# 持久化任务落盘的位置：工作目录下的隐藏文件。
# 「durable（持久）」的任务写在这里，下次启动还能恢复；session 级任务只活在内存里。
DURABLE_PATH = WORKDIR / ".scheduled_tasks.json"
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]  # 从环境变量读模型 ID

# 根据操作系统告诉模型它面对的 shell 语法，避免生成错误的命令风格。
ENVIRONMENT_PROMPT = (
    "Windows: the bash tool runs through cmd.exe; use cmd.exe syntax, not Unix "
    "Bash or PowerShell syntax, and prefer dedicated file tools for file operations"
    if os.name == "nt"
    else "Unix-like: the bash tool runs the system shell"
)
# 相比前面几课，这里多了一句：计划在「未来某个本地时间」做的工作要用 schedule_cron。
# 注意是「本地时间」——下面的 cron 匹配用的就是 datetime.now() 的本地时钟。
SYSTEM = (
    f"You are a coding agent at {WORKDIR}. Environment: {ENVIRONMENT_PROMPT}. "
    "Use tools to solve tasks. "
    "Use schedule_cron for work that should start at a future local time."
)


# -- From s04: tool implementations --
# 以下工具实现与 s04 基本一致：bash / read / write / edit / glob。

def run_bash(command: str) -> str:
    # 真正执行 shell 命令；一次性跑完并收集输出（对比 s11 的后台执行版本）。
    try:
        result = subprocess.run(
            command,
            shell=True,       # 交给系统 shell 解释，因此支持管道、重定向等语法
            cwd=WORKDIR,      # 在工作目录下执行
            capture_output=True,
            text=True, errors="replace",  # 输出按文本处理，遇到坏编码不抛异常
            timeout=120,      # 兜底超时，防止命令挂死拖住整个循环
        )
        output = (result.stdout + result.stderr).strip()  # 标准输出与错误合并展示
        if result.returncode != 0:
            # 非零退出码视为出错，把状态码一并回给模型，便于它自查。
            return f"Error: command exited with status {result.returncode}\n{output}"
        return output[:50000] if output else "(no output)"  # 截断超大输出
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int | None = None) -> str:
    # 读取文件内容，可选只读前 limit 行。
    try:
        file_path = (WORKDIR / path).resolve()
        lines = file_path.read_text(encoding="utf-8").splitlines()
        if limit and limit < len(lines):
            # 截断时补一行说明还剩多少行，让模型知道内容被截了。
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as error:
        return f"Error: {error}"


def run_write(path: str, content: str) -> str:
    # 写文件（覆盖式）；父目录不存在就自动创建。
    try:
        file_path = (WORKDIR / path).resolve()
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as error:
        return f"Error: {error}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    # 精确文本替换：只替换第一次出现的位置（count=1），比整文件覆盖安全。
    try:
        file_path = (WORKDIR / path).resolve()
        text = file_path.read_text(encoding="utf-8")
        if old_text not in text:
            return f"Error: text not found in {path}"
        file_path.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {path}"
    except Exception as error:
        return f"Error: {error}"


def run_glob(pattern: str) -> str:
    # 按 glob 模式找文件，** 表示递归匹配。
    try:
        matches = sorted({
            match
            for match in glob.glob(pattern, root_dir=WORKDIR, recursive=True)
            # 安全校验：解析后的绝对路径必须仍在工作目录内，
            # 防止 ../../ 之类的模式把工作目录外的文件列出来。
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR)
        })
        shown = matches[:200]  # 结果过多只展示前 200 条
        if len(matches) > 200:
            shown.append("... (more matches omitted; narrow the pattern)")
        return "\n".join(shown) if shown else "(no matches)"
    except Exception as error:
        return f"Error: {error}"


# 工具清单：发给模型看的 schema（名字 + 描述 + 参数结构）。
TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object",
                      "properties": {"command": {"type": "string"}},
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

# 工具名 -> 实际处理函数 的映射表；execute_tool 靠它分发。
TOOL_HANDLERS = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
}


# -- From s04: hooks and permission checks --
# 钩子机制：在关键节点（提交输入、用工具前/后、回合结束）插入自定义逻辑。

HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}


def register_hook(event: str, callback):
    # 注册一个钩子回调到指定事件上。
    HOOKS[event].append(callback)


def trigger_hooks(event: str, *args):
    # 依次触发某事件的所有回调；谁先返回非 None 就立即中断并返回，
    # 相当于「拦截」语义（PreToolUse 里被用来拒绝工具调用）。
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None


DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]  # 硬性黑名单
# 正则：匹配「命令起始处」的 rm / del（前面是行首或 ; & | ( ) 换行 等分隔符），
# 以避免 "confirm " 这类单词里的 "rm" 被误判。
DESTRUCTIVE_COMMAND_WORD = re.compile(
    r"(?i)(?:^|[;&|()\n])\s*(?:rm|del)(?=\s|$|[;&|()])"
)
DESTRUCTIVE = ["rm ", "> /etc/", "chmod 777"]  # 触发人工确认的关键词


def contains_destructive_command(command: str) -> bool:
    # 判断命令是否形态上像危险删除操作。
    return bool(DESTRUCTIVE_COMMAND_WORD.search(command))


def request_permission(block, reason: str) -> str | None:
    # 向用户征求许可。返回 None 表示放行，返回字符串表示拒绝理由。
    if threading.current_thread() is not threading.main_thread():
        # 关键点：只有主线程能弹交互式提问。定时任务是由后台线程驱动 agent 循环的，
        # 那里没法也没人回答 input()，所以一律拒绝，避免把线程卡死在等待输入上。
        return "Permission denied: scheduled turns cannot request interactive approval"

    print(f"\n\033[33m[permission] {reason}\033[0m")  # 黄色提示
    print(f"   Tool: {block.name}({block.input})")
    choice = input("   Allow? [y/N] ").strip().lower()  # 默认拒绝（直接回车 = N）
    if choice not in ("y", "yes"):
        return "Permission denied by user"
    return None


def permission_hook(block):
    # PreToolUse 钩子：在工具真正执行前做权限/安全检查。
    if block.name == "bash":
        command = block.input.get("command", "")
        for pattern in DENY_LIST:
            if pattern in command:
                # 命中黑名单：直接拒绝，不给商量余地。
                print(f"\n\033[31m[blocked] '{pattern}'\033[0m")  # 红色提示
                return "Permission denied by deny list"
        if contains_destructive_command(command) or any(
            keyword in command for keyword in DESTRUCTIVE
        ):
            # 疑似破坏性命令：转人工确认。
            return request_permission(block, "Potentially destructive command")

    if block.name in ("read_file", "write_file", "edit_file"):
        path = block.input.get("path", "")
        if not (WORKDIR / path).resolve().is_relative_to(WORKDIR):
            # 越出工作目录的文件访问也要人工确认。
            return request_permission(block, "Access outside workspace")
    return None  # 返回 None 表示放行


def log_hook(block):
    # PreToolUse 钩子：简单打印一下调用了什么工具（灰色）。
    preview = str(list(block.input.values())[:2])[:60]  # 取前两个参数值做预览
    print(f"\033[90m[HOOK] {block.name}({preview})\033[0m")
    return None


def large_output_hook(block, output):
    # PostToolUse 钩子：输出过大时提醒一下，帮助发现「一次读了太多东西」。
    if len(str(output)) > 100000:
        print(
            f"\033[33m[HOOK] Large output from {block.name}: "
            f"{len(str(output))} chars\033[0m"
        )
    return None


def context_inject_hook(query: str):
    # UserPromptSubmit 钩子：提交问题时注入环境信息。
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {WORKDIR}\033[0m")
    return None


def summary_hook(messages: list):
    # Stop 钩子：回合结束时统计这次会话一共用掉了多少次工具调用。
    tool_count = sum(
        1
        for message in messages
        for block in (
            message.get("content")
            if isinstance(message.get("content"), list)
            else []
        )
        if isinstance(block, dict) and block.get("type") == "tool_result"
    )
    print(f"\033[90m[HOOK] Stop: session used {tool_count} tool calls\033[0m")
    return None


register_hook("UserPromptSubmit", context_inject_hook)
register_hook("PreToolUse", permission_hook)   # 权限检查放在前面，可拦截
register_hook("PreToolUse", log_hook)          # 日志放在后面，只做展示
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", summary_hook)


# -- New in s12: cron jobs --
# 本课新增部分：定时任务的数据结构、cron 表达式解析/校验、以及调度与队列逻辑。

@dataclass
class CronJob:
    """一个定时任务（可以用 asdict() 直接转成字典存 JSON）。"""
    id: str                        # 任务 ID，形如 cron_a1b2c3d4
    cron: str                      # 5 段式 cron 表达式：分 时 日 月 周
    prompt: str                    # 到点时要交给 agent 执行的提示词
    recurring: bool                # True=周期任务（每次到点都触发）；False=一次性
    durable: bool                  # True=写入磁盘，重启后恢复；False=只活在本次进程
    pending_delivery: bool = False # 是否已经排进队列、等着被 agent 取走（防止同一分钟重复入队）
    last_fired: str | None = None  # 上次触发的「分钟标记」如 "2026-10-16 09:00"


scheduled_jobs: dict[str, CronJob] = {}   # 任务表：id -> 任务对象（全部任务的总账）
cron_queue: list[CronJob] = []            # 待办队列：到点、尚未被 agent 取走的任务
# 保护上面两份共享状态的可重入锁。用 RLock：内部函数会互相调用（如 cancel 里再存盘），
# 同一线程重入加锁不会自己把自己锁死。
cron_lock = threading.RLock()


def _cron_field_matches(field: str, value: int) -> bool:
    # 判断 cron 的「某一段」是否匹配给定的数值。支持 4 种写法：
    #   *     任意值
    #   */n   每 n 步（整除即命中）
    #   a,b,c 枚举任一命中即可
    #   a-b   闭区间
    #   5     精确值
    if field == "*":
        return True
    if field.startswith("*/"):
        return value % int(field[2:]) == 0
    if "," in field:
        # 枚举：拆开后递归匹配，任意一项命中就算命中。
        return any(_cron_field_matches(part.strip(), value)
                   for part in field.split(","))
    if "-" in field:
        start, end = field.split("-", 1)
        return int(start) <= value <= int(end)
    return value == int(field)


def cron_matches(cron_expr: str, moment: datetime) -> bool:
    # 判断「当前时刻」是否命中 cron 表达式。只精确到分钟（秒被忽略）。
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return False  # 段数不对直接不匹配

    minute, hour, day, month, weekday = fields
    # 注意：cron 里 0/7 表示周日，而 Python weekday() 是周一=0…周日=6，
    # 所以要 +1 再对 7 取模，转换成 cron 的编号习惯。
    cron_weekday = (moment.weekday() + 1) % 7
    if not (
        _cron_field_matches(minute, moment.minute)
        and _cron_field_matches(hour, moment.hour)
        and _cron_field_matches(month, moment.month)
    ):
        # 分、时、月必须同时命中，才继续看「日 / 周」。
        return False

    day_matches = _cron_field_matches(day, moment.day)
    weekday_matches = _cron_field_matches(weekday, cron_weekday)
    # 经典的 cron 语义：日 和 周 是「或」的关系（不是与）。
    # 只有两个都写成 * 时才必须都满足（其实此时恒为真）。
    if day == "*" and weekday == "*":
        return True
    if day == "*":
        return weekday_matches
    if weekday == "*":
        return day_matches
    return day_matches or weekday_matches


def _validate_cron_field(field: str, minimum: int, maximum: int) -> str | None:
    # 校验 cron 的单段是否合法。返回 None 表示合法，否则返回错误说明。
    if field == "*":
        return None
    if field.startswith("*/"):
        step = field[2:]
        if not step.isdigit() or int(step) <= 0:
            return f"Invalid step: {field}"  # 步长必须是正整数
        return None
    if "," in field:
        # 枚举：逐个校验，返回第一个错误。
        for part in field.split(","):
            error = _validate_cron_field(part.strip(), minimum, maximum)
            if error:
                return error
        return None
    if "-" in field:
        start, end = field.split("-", 1)
        if not start.isdigit() or not end.isdigit():
            return f"Invalid range: {field}"
        start_value, end_value = int(start), int(end)
        if start_value > end_value:
            return f"Range start is greater than end: {field}"
        if start_value < minimum or end_value > maximum:
            return f"Range {field} is outside [{minimum}-{maximum}]"
        return None
    if not field.isdigit():
        return f"Invalid field: {field}"  # 不是数字、也不是上面几种写法
    value = int(field)
    if value < minimum or value > maximum:
        return f"Value {value} is outside [{minimum}-{maximum}]"
    return None


def validate_cron(cron_expr: str) -> str | None:
    # 校验整个 5 段式表达式。返回 None 表示合法。
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return f"Expected 5 fields, got {len(fields)}"

    # 每段的取值范围：分 0-59、时 0-23、日 1-31、月 1-12、周 0-6。
    field_rules = [
        ("minute", 0, 59),
        ("hour", 0, 23),
        ("day-of-month", 1, 31),
        ("month", 1, 12),
        ("day-of-week", 0, 6),
    ]
    for field, (name, minimum, maximum) in zip(fields, field_rules):
        error = _validate_cron_field(field, minimum, maximum)
        if error:
            return f"{name}: {error}"  # 带上段名，方便定位是哪一段写错了
    return None


def save_durable_jobs():
    # 把所有 durable 任务写进 .scheduled_tasks.json。
    # 注意：调用前应已持有 cron_lock（本函数内部也加锁，RLock 可重入所以没问题）。
    with cron_lock:
        payload = [
            asdict(job)                    # dataclass -> dict
            for job in scheduled_jobs.values()
            if job.durable                 # session 级任务不落盘
        ]
        # 先写临时文件、再用 os.replace 原子替换。
        # 这样即使写到一半崩溃，也不会留下一个半截的 JSON 把旧数据弄坏。
        # 临时名带上 pid 和线程号，避免多线程/多进程同时写时互相覆盖。
        temporary = DURABLE_PATH.with_name(
            f"{DURABLE_PATH.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(temporary, DURABLE_PATH)
        finally:
            temporary.unlink(missing_ok=True)  # 收尾：临时文件已不存在也不报错


def load_durable_jobs():
    # 启动时从磁盘恢复 durable 任务。
    if not DURABLE_PATH.exists():
        return
    try:
        payload = json.loads(DURABLE_PATH.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("expected a JSON list")
    except (OSError, json.JSONDecodeError, ValueError) as error:
        # 文件坏了不致命：提示一下然后空手起跑，不让程序起不来。
        print(f"  [cron] could not load {DURABLE_PATH.name}: {error}")
        return

    loaded = 0
    with cron_lock:
        for item in payload:
            try:
                # 反序列化 + 逐项校验：磁盘上的内容可能被人手改过，
                # 不能直接信任，坏的那一条跳过，其余照常加载。
                job = CronJob(**item)
                error = validate_cron(job.cron)
                if error:
                    raise ValueError(error)
                if not job.id.startswith("cron_"):
                    raise ValueError("invalid job ID")
                if not job.prompt.strip():
                    raise ValueError("prompt cannot be empty")
            except (TypeError, ValueError) as error:
                print(f"  [cron] skipped invalid saved job: {error}")
                continue
            scheduled_jobs[job.id] = job
            if job.pending_delivery:
                # 上次进程退出时这任务还没被交付：重新排进队列，别把它弄丢了。
                cron_queue.append(job)
            loaded += 1
    if loaded:
        print(f"  [cron] loaded {loaded} durable job(s)")


def new_cron_id() -> str:
    # 生成一个没被占用的随机任务 ID。
    for _ in range(100):
        job_id = f"cron_{secrets.token_hex(4)}"  # 4 字节随机 → 8 位十六进制
        if job_id not in scheduled_jobs:
            return job_id
    # 100 次都没撞到空位（现实中几乎不可能），直接报错而不是死循环。
    raise RuntimeError("Could not allocate a cron job ID")


def schedule_job(cron: str, prompt: str, recurring: bool = True,
                 durable: bool = True) -> CronJob | str:
    # 新建并登记一个定时任务。成功返回 CronJob，失败返回错误字符串
    # （用「返回字符串 vs 返回对象」来区分成败，调用方 isinstance 判断）。
    error = validate_cron(cron)
    if error:
        return error
    if not prompt.strip():
        return "Prompt cannot be empty"

    with cron_lock:
        job = CronJob(
            id=new_cron_id(),
            cron=cron,
            prompt=prompt,
            recurring=recurring,
            durable=durable,
        )
        scheduled_jobs[job.id] = job
        try:
            if durable:
                save_durable_jobs()  # 立即落盘，保证「说定了就不丢」
        except Exception:
            # 落盘失败就回滚内存里的登记：宁可没建成功，也不要内存和磁盘不一致。
            scheduled_jobs.pop(job.id, None)
            raise
    print(f"  [cron] scheduled {job.id}: {cron} -> {prompt[:60]}")
    return job


def cancel_job(job_id: str) -> str:
    # 取消一个任务：从任务表里删掉，如果它还在队列里也一并清掉。
    with cron_lock:
        job = scheduled_jobs.get(job_id)
        if job is None:
            return f"Job {job_id} not found"

        previous_queue = list(cron_queue)  # 存一份快照，供回滚用
        scheduled_jobs.pop(job_id)
        cron_queue[:] = [queued for queued in cron_queue if queued.id != job_id]
        try:
            if job.durable:
                save_durable_jobs()
        except Exception:
            # 落盘失败则把内存状态整体还原，保持「内存 = 磁盘」。
            scheduled_jobs[job_id] = job
            cron_queue[:] = previous_queue
            raise
    print(f"  [cron] cancelled {job_id}")
    return f"Cancelled {job_id}"


def _enqueue_due_job(job: CronJob, minute_marker: str | None = None):
    # 把一个「到点了」的任务放进待办队列。
    # 先记下旧值，落盘失败时好回滚（pending_delivery/last_fired 都是为了防重复触发）。
    old_pending = job.pending_delivery
    old_last_fired = job.last_fired
    job.pending_delivery = True
    if minute_marker is not None:
        job.last_fired = minute_marker  # 记下「哪一分钟触发过」，防止同一分钟重复入队
    try:
        if job.durable:
            save_durable_jobs()
    except Exception:
        job.pending_delivery = old_pending
        job.last_fired = old_last_fired
        raise
    cron_queue.append(job)


def poll_due_jobs(moment: datetime):
    # 调度线程每秒调用一次：检查所有任务，命中的就入队。
    minute_marker = moment.strftime("%Y-%m-%d %H:%M")  # 精确到分钟的标记
    with cron_lock:
        # 用 list(...) 拷一份再遍历：遍历中可能有别处改任务表，避免踩坑。
        for job in list(scheduled_jobs.values()):
            try:
                if job.pending_delivery or job.last_fired == minute_marker:
                    continue  # 已排队 / 这一分钟已触发过 → 跳过，保证每任务每分钟最多一次
                if cron_matches(job.cron, moment):
                    _enqueue_due_job(job, minute_marker)
                    print(f"  [cron] due {job.id}: {job.prompt[:60]}")
            except Exception as error:
                # 单个任务出问题不能拖垮调度线程，记下错误继续轮询。
                print(f"  [cron] could not enqueue {job.id}: {error}")


def consume_cron_queue() -> list[CronJob]:
    # 取走队列里所有待办任务，并清空队列（交给 agent 去执行）。
    with cron_lock:
        jobs = list(cron_queue)
        cron_queue.clear()
    return jobs


def acknowledge_cron_jobs(jobs: list[CronJob]):
    # 任务已成功交付给模型后调用，确认收尾：
    #   周期任务 -> 清掉 pending_delivery，等待下一个时间点再次触发；
    #   一次性任务 -> 直接从任务表里删除。
    changed: list[tuple[CronJob, bool]] = []  # (任务, 原来的 pending 状态)，用于回滚
    removed: list[CronJob] = []               # 被删除的一次性任务，用于回滚
    with cron_lock:
        for delivered in jobs:
            current = scheduled_jobs.get(delivered.id)
            if current is None:
                continue  # 期间已被 cancel，跳过
            changed.append((current, current.pending_delivery))
            if current.recurring:
                current.pending_delivery = False
            else:
                removed.append(current)
                scheduled_jobs.pop(current.id)

        try:
            if any(job.durable for job, _ in changed):
                save_durable_jobs()
        except Exception:
            # 落盘失败：把删除的加回去、状态改回原样、重新排回队列，保证不丢任务。
            for job in removed:
                scheduled_jobs[job.id] = job
            for job, pending in changed:
                job.pending_delivery = pending
            queued_ids = {job.id for job in cron_queue}
            for job, _ in changed:
                if job.id not in queued_ids:
                    cron_queue.append(job)
            raise


def restore_cron_jobs(jobs: list[CronJob]):
    # 交付失败时的回退：把任务重新标为「待交付」并放回队列，下一轮再试。
    with cron_lock:
        queued_ids = {job.id for job in cron_queue}
        for delivered in jobs:
            current = scheduled_jobs.get(delivered.id)
            if current is None:
                continue  # 已被 cancel
            current.pending_delivery = True
            if current.id not in queued_ids:
                cron_queue.append(current)
                queued_ids.add(current.id)


def has_cron_queue() -> bool:
    # 队列是否非空（供队列处理线程做快速判断）。
    with cron_lock:
        return bool(cron_queue)


def run_schedule_cron(cron: str, prompt: str, recurring: bool = True,
                      durable: bool = True) -> str:
    # 工具入口：schedule_cron。给模型返回一句人类可读的结果。
    result = schedule_job(cron, prompt, recurring, durable)
    if isinstance(result, str):
        return f"Error: {result}"  # 返回字符串说明是校验失败
    return f"Scheduled {result.id}: {cron} -> {prompt}"


def run_list_crons() -> str:
    # 工具入口：list_crons。列出所有任务及其属性。
    with cron_lock:
        jobs = list(scheduled_jobs.values())
    if not jobs:
        return "No cron jobs."

    lines = []
    for job in jobs:
        frequency = "recurring" if job.recurring else "one-shot"  # 周期 / 一次性
        storage = "durable" if job.durable else "session"          # 落盘 / 仅本进程
        lines.append(
            f"{job.id}: {job.cron} -> {job.prompt[:60]} "
            f"[{frequency}, {storage}]"
        )
    return "\n".join(lines)


def run_cancel_cron(job_id: str) -> str:
    # 工具入口：cancel_cron。
    return cancel_job(job_id)


# 把本课新增的三个工具追加进工具清单。
TOOLS.extend([
    {"name": "schedule_cron",
     "description": "Schedule a prompt with a 5-field cron expression.",
     "input_schema": {"type": "object",
                      "properties": {
                          "cron": {"type": "string"},
                          "prompt": {"type": "string"},
                          "recurring": {"type": "boolean"},
                          "durable": {"type": "boolean"}},
                      "required": ["cron", "prompt"]}},
    {"name": "list_crons", "description": "List scheduled cron jobs.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "cancel_cron", "description": "Cancel a cron job by ID.",
     "input_schema": {"type": "object",
                      "properties": {"job_id": {"type": "string"}},
                      "required": ["job_id"]}},
])

TOOL_HANDLERS.update({
    "schedule_cron": run_schedule_cron,
    "list_crons": run_list_crons,
    "cancel_cron": run_cancel_cron,
})


def execute_tool(block) -> str:
    # 执行一个工具调用：先过 PreToolUse 钩子（可能被拦），再找处理函数执行。
    blocked = trigger_hooks("PreToolUse", block)
    if blocked is not None:
        return str(blocked)  # 钩子返回了拒绝理由，直接当结果回给模型

    handler = TOOL_HANDLERS.get(block.name)
    try:
        output = handler(**block.input) if handler else f"Unknown: {block.name}"
    except Exception as error:
        # 工具内部异常不能炸掉 agent 循环，转成错误文本给模型看。
        output = f"Error: {error}"
    trigger_hooks("PostToolUse", block, output)
    return str(output)


# -- Scheduler and agent loop --
# 调度线程 + agent 循环。

RUNTIME_STOP = threading.Event()               # 停止信号：set() 后各线程的 wait 会立刻返回 True
runtime_threads: list[threading.Thread] = []   # 已启动的后台线程
runtime_started = False                        # 是否已启动（防止重复启动）
runtime_lock = threading.Lock()                # 保护上面两个启动状态的锁
# agent_lock：保证「同一时刻只有一个线程在跑 agent 循环」。
# 主线程（用户输入）和队列处理线程（定时任务）都要抢这把锁。
agent_lock = threading.Lock()
session_history: list = []                     # 对话历史（唯一的上下文，两个线程共享）


def cron_scheduler_loop(stop_event: threading.Event = RUNTIME_STOP):
    # 调度线程主体：每秒醒来一次，检查有没有任务到点。
    # 用 stop_event.wait(1.0) 而不是 time.sleep：一旦要停止，能立刻醒来退出，不必等满 1 秒。
    while not stop_event.wait(1.0):
        poll_due_jobs(datetime.now())  # 用本地时间判断是否命中 cron


def agent_loop(messages: list, context: dict | None = None):
    # agent 主循环。messages 就是共享的 session_history（原地 append）。
    fired = consume_cron_queue()   # 先把到点排队的定时任务取出来
    scheduled_start = len(messages)  # 记住「插入定时消息前」的位置，失败时要回退到这里
    for job in fired:
        # 定时任务以普通 user 消息的形式注入对话，加 [Scheduled] 前缀加以区分。
        messages.append({"role": "user", "content": f"[Scheduled] {job.prompt}"})
        print(f"  [cron] delivered {job.id}: {job.prompt[:60]}")

    waiting_for_ack = list(fired)  # 还没确认收尾的任务
    while True:
        try:
            response = client.messages.create(
                model=MODEL,
                system=SYSTEM,
                messages=messages,
                tools=TOOLS,
                max_tokens=8000,
            )
        except Exception as error:
            if waiting_for_ack:
                # 请求都失败了：把刚注入的定时消息删掉，并把任务放回队列等下次重试，
                # 否则消息会重复累积、任务也算白丢了。
                del messages[scheduled_start:]
                restore_cron_jobs(waiting_for_ack)
            print(f"  [error] {type(error).__name__}: {error}")
            return context

        messages.append({"role": "assistant", "content": response.content})
        if waiting_for_ack:
            # 拿到模型回复就说明「定时任务确实被处理了」，此时才确认收尾
            #（一次性任务会被删除，周期任务重置状态）。这个顺序保证不丢任务。
            try:
                acknowledge_cron_jobs(waiting_for_ack)
            except Exception as error:
                print(f"  [cron] acknowledgement failed: {error}")
            waiting_for_ack = []

        tool_calls = [
            block for block in response.content if block.type == "tool_use"
        ]
        if not tool_calls:
            # 模型没再要求用工具 → 这一轮结束。
            force = trigger_hooks("Stop", messages)
            if force:
                # Stop 钩子若返回内容，就把它当作用户消息追加，让模型继续干活
                #（此处 summary_hook 只打印统计、返回 None，所以一般会走到 return）。
                messages.append({"role": "user", "content": force})
                continue
            return context

        results = []
        for block in tool_calls:
            output = execute_tool(block)
            # 每个工具调用都要回一条 tool_result，且 tool_use_id 必须对应，否则 API 报错。
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": output,
            })
        messages.append({"role": "user", "content": results})


def print_latest_assistant_text(messages: list):
    # 打印最近一条 assistant 消息里的纯文本（工具调用块不打印）。
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        content = message.get("content", "")
        if isinstance(content, str):
            print(content)
        else:
            for block in content:
                # content 里混着文本块和工具调用块，只挑文本块打印。
                if getattr(block, "type", None) == "text":
                    print(block.text)
                elif isinstance(block, dict) and block.get("type") == "text":
                    print(block.get("text", ""))
        return  # 从后往前找到第一条 assistant 就停


def run_agent_turn_locked(user_query: str | None = None):
    # 跑一轮对话。命名带 locked 是提醒：调用方必须已持有 agent_lock。
    if user_query is not None:
        # user_query 为 None 表示这次跑的是定时任务（消息已在 agent_loop 里注入），
        # 不触发 UserPromptSubmit 钩子、也不追加用户消息。
        trigger_hooks("UserPromptSubmit", user_query)
        session_history.append({"role": "user", "content": user_query})
    agent_loop(session_history)
    print_latest_assistant_text(session_history)
    print()


def queue_processor_loop(stop_event: threading.Event = RUNTIME_STOP):
    # 队列处理线程主体：每 0.2 秒看一眼队列，有任务就抢 agent_lock 去执行。
    while not stop_event.wait(0.2):
        # 双重判断：队列为空就别抢锁了；抢不到锁（主线程正在对话）就跳过，
        # 等下一圈再看——这样定时任务只会在用户没在交互时插空执行。
        if not has_cron_queue() or not agent_lock.acquire(blocking=False):
            continue
        try:
            if has_cron_queue():  # 抢到锁后可能队列已被别人清空，再确认一次
                run_agent_turn_locked()
        finally:
            agent_lock.release()  # 无论成功失败都要放锁


def start_runtime_threads():
    # 启动后台线程（幂等：重复调用只生效一次）。
    global runtime_started
    with runtime_lock:
        if runtime_started:
            return
        load_durable_jobs()   # 先恢复磁盘上的持久任务，再开线程
        RUNTIME_STOP.clear()  # 清掉可能残留的停止信号
        runtime_threads.extend([
            threading.Thread(
                target=cron_scheduler_loop,
                name="cron-scheduler",
                daemon=True,   # daemon：主程序退出时自动结束，不留悬挂线程
            ),
            threading.Thread(
                target=queue_processor_loop,
                name="cron-queue-processor",
                daemon=True,
            ),
        ])
        for thread in runtime_threads:
            thread.start()
        runtime_started = True


def stop_runtime_threads():
    # 停止后台线程：发停止信号并等待它们结束。
    global runtime_started
    with runtime_lock:
        if not runtime_started:
            return
        RUNTIME_STOP.set()
        for thread in runtime_threads:
            thread.join(timeout=1)  # 最多等 1 秒，不无限等待
        runtime_threads.clear()
        runtime_started = False


if __name__ == "__main__":
    print("s12: Cron Scheduler - run prompts on a local schedule")
    print("Enter a question, press Enter to send. Type q to quit.\n")
    start_runtime_threads()  # 先让调度线程跑起来，后面才可能收到定时任务
    try:
        while True:
            try:
                # \001/\002 tell Readline the ANSI escapes have zero display width.
                # （同上：\001/\002 告诉 Readline，中间这段 ANSI 转义序列「不占显示宽度」，
                #   否则光标位置会算错。这里是青色的提示符 "s12 >> "。）
                query = input("\001\033[36m\002s12 >> \001\033[0m\002")
            except (EOFError, KeyboardInterrupt):
                break  # Ctrl+C / Ctrl+D 退出
            if query.strip().lower() in ("q", "exit", ""):
                break  # 空输入或 q/exit 也退出
            with agent_lock:
                # 拿锁跑一轮：与队列处理线程互斥，保证对话历史不会同时被两个线程改。
                run_agent_turn_locked(query)
    finally:
        stop_runtime_threads()  # 收尾：停掉后台线程，避免资源泄漏
