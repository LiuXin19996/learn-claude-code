#!/usr/bin/env python3
"""
s05_todo_write.py - TodoWrite
（待办清单）

The model tracks its progress through a TodoManager. After three rounds
without an update, the harness adds a reminder alongside the tool results.
（模型通过一个 TodoManager 来跟踪自己的进度。如果连续三轮都没有更新待办，
  框架就会在工具结果旁边附上一条「提醒」，催它去更新。）

    +----------+      +-------+      +--------------+
    |   User   | ---> |  LLM  | ---> | Tools        |
    |  prompt  |      |       |      | + todo_write |
    +----------+      +---^---+      +------+-------+
                          |                 | update
                          |          +------v----------+
                          |          | TodoManager     |
                          |          | [ ] pending     |
                          |          | [>] in progress |
                          |          | [x] completed   |
                          |          +------+----------+
                          | tool_result     |
                          +-----------------+
    （模型多了一个 todo_write 工具：它把「计划」写进 TodoManager，
      TodoManager 渲染成 [ ]/[>]/[x] 清单再回流给模型。
      这样一来，计划就变成了对话里可以反复看见的「结构化状态」，
      而不是模型脑内一闪而过的念头。）

              rounds_since_todo >= 3 -> add <reminder>
    （连续 3 轮没调用 todo_write，就在工具结果里追加一条 <reminder>。）
"""

import ast         # 解析 Python 字面量：兜底处理不是合法 JSON 的列表写法
import json        # 解析模型传来的 JSON 字符串形式的待办列表
import os          # 操作系统相关功能：读环境变量、判断平台
import re          # 正则表达式：危险命令模式匹配（s03 起沿用）
import subprocess  # 子进程管理：执行模型生成的 shell 命令
from pathlib import Path  # 面向对象的路径操作：拼路径、解析、判断越界

# readline 提供命令行编辑能力（历史记录、方向键、退格等）。
# s05 里只保留了一行配置；Windows 上可能没有 readline，所以用 try/except 包住。
try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
except ImportError:
    # 没有 readline（例如 Windows 自带 Python）直接跳过，不影响主逻辑。
    pass

from anthropic import Anthropic    # Anthropic 官方 Python SDK 的客户端类
from dotenv import load_dotenv     # 从 .env 文件加载环境变量（如 API Key）

# override=True：即使系统里已有同名环境变量，也用 .env 里的值覆盖。
load_dotenv(override=True)

# 配了 ANTHROPIC_BASE_URL（走代理/自定义端点）时，
# 移除可能打架的 ANTHROPIC_AUTH_TOKEN，避免两种鉴权方式冲突。
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()  # 工作目录：文件工具与权限判断的基准路径
# 创建 Anthropic 客户端；base_url 默认官方 API，配了环境变量则走自定义端点。
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
# 从环境变量读模型 ID；用 [] 取值，.env 里没配会直接抛 KeyError。
MODEL = os.environ["MODEL_ID"]

# s05 change: SYSTEM prompt adds planning guidance
# s05 的改动之一：系统提示词里加了「先规划」的引导——
# 要求模型在多步任务开始前先调用 todo_write 列计划，并在过程中更新状态。
ENVIRONMENT_PROMPT = (
    "Windows: the bash tool runs through cmd.exe; use cmd.exe syntax, not Unix "
    "Bash or PowerShell syntax, and prefer dedicated file tools for file operations"
    if os.name == "nt"
    else "Unix-like: the bash tool runs the system shell"
)
SYSTEM = (
    f"You are a coding agent at {WORKDIR}. Environment: {ENVIRONMENT_PROMPT}. "
    "Before starting any multi-step task, use todo_write to plan your steps. "
    "Update status as you go."
)


# -- Tool implementations from s02-s04 --
# 五个老工具原样沿用，本课不修改它们。

def run_bash(command: str) -> str:
    # 执行命令：shell=True 交给系统 shell，cwd=WORKDIR 固定工作目录，
    # capture_output 捕获输出，text=True 按文本返回，
    # errors="replace" 防解码崩溃，timeout=120 防卡死。
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, errors="replace", timeout=120)
        # 合并 stdout/stderr 并去空白；截断到 50000 字符防止撑爆上下文。
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        # 超时：返回提示让模型自行调整策略。
        return "Error: Timeout (120s)"

def run_read(path: str, limit: int | None = None) -> str:
    # 读文件：相对路径拼到 WORKDIR 后 resolve（折叠 ../ 等）。
    try:
        lines = (WORKDIR / path).resolve().read_text(encoding="utf-8").splitlines()
        # limit：只读前 N 行，截断时附一行提示说明还剩多少行。
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"

def run_write(path: str, content: str) -> str:
    # 写文件：整文件覆盖；parents=True 按需建目录，exist_ok=True 忽略已存在。
    try:
        file_path = (WORKDIR / path).resolve()
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        # 返回字节数，方便模型确认写入成功。
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    # 编辑文件：只替换第一处精确匹配（最后的参数 1），比整体重写更安全省 token。
    try:
        file_path = (WORKDIR / path).resolve()
        text = file_path.read_text(encoding="utf-8")
        # 找不到原文片段就报错，提醒模型必须给出真实存在的文本。
        if old_text not in text:
            return f"Error: text not found in {path}"
        file_path.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"

def run_glob(pattern: str) -> str:
    # 查找文件：集合去重 + 排序保证输出稳定；** 表示递归匹配。
    import glob as g
    try:
        matches = sorted({
            match for match in g.glob(
                pattern, root_dir=WORKDIR, recursive=True)
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR)
        })
        # 最多 200 条，防止刷屏和吃掉上下文。
        shown = matches[:200]
        if len(matches) > 200:
            shown.append("... (more matches omitted; narrow the pattern)")
        return "\n".join(shown) if shown else "(no matches)"
    except Exception as e:
        return f"Error: {e}"


# -- New in s05: structured state the model updates --
# s05 新增：一份「由模型自己更新的结构化状态」——待办清单。
# 关键思路：不把进度留在模型的隐式记忆里，而是外化成列表，
# 每一轮都随工具结果回流，模型因此不容易「走丢」。

class TodoManager:
    # 待办管理器：负责校验 + 保存 + 渲染清单。
    def __init__(self):
        # 内部状态就是一个 dict 列表：每项含 content 和 status。
        self.items: list[dict] = []

    def update(self, todos: list | str) -> str:
        # 模型有时直接给列表，有时给一个 JSON 字符串，所以先归一化。
        if isinstance(todos, str):
            try:
                todos = json.loads(todos)
            except json.JSONDecodeError:
                # 不是严格 JSON（比如用了单引号）时，再试 Python 字面量解析。
                try:
                    todos = ast.literal_eval(todos)
                except (SyntaxError, ValueError) as e:
                    raise ValueError("todos must be a list or JSON array string") from e

        # 以下是一连串「校验」：把错误挡在存储之前，并用清晰的报错
        # 让模型知道该怎么改（这正是「工具返回错误而非崩溃」的思路）。
        if not isinstance(todos, list):
            raise ValueError("todos must be a list")
        if len(todos) > 20:
            # 上限 20 条，防止清单膨胀到把上下文吃光。
            raise ValueError("Max 20 todos allowed")

        validated = []
        in_progress_count = 0
        for index, todo in enumerate(todos):
            if not isinstance(todo, dict):
                raise ValueError(f"todos[{index}] must be an object")

            content = str(todo.get("content", "")).strip()
            # status 缺省为 pending，并统一转小写便于比较。
            status = str(todo.get("status", "pending")).lower()
            if not content:
                raise ValueError(f"todos[{index}] requires content")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"todos[{index}] has invalid status '{status}'")
            if status == "in_progress":
                in_progress_count += 1
            # 只保留需要的两个字段，丢掉模型可能多传的其它键。
            validated.append({"content": content, "status": status})

        if in_progress_count > 1:
            # 强制「一次只做一件事」：多于一个进行中就直接报错。
            raise ValueError("Only one todo can be in_progress at a time")

        # 全部校验通过才整体替换旧状态（要么全成功、要么保持原样）。
        self.items = validated
        return self.render()

    def render(self) -> str:
        # 把内部状态渲染成给模型看的文本清单。
        if not self.items:
            return "No todos."

        lines = []
        for todo in self.items:
            # 状态到标记的映射：pending [ ]、in_progress [>]、completed [x]。
            marker = {
                "pending": "[ ]",
                "in_progress": "[>]",
                "completed": "[x]",
            }[todo["status"]]
            lines.append(f"{marker} {todo['content']}")

        # 末尾附一个完成度统计，给模型一个明确的进度锚点。
        # 注意 sum(bool) 会把 True 当 1 累加，所以直接数出已完成条数。
        done = sum(todo["status"] == "completed" for todo in self.items)
        lines.append(f"\n({done}/{len(self.items)} completed)")
        return "\n".join(lines)


# 模块级单例：整个会话共用一份待办清单（状态保存在循环之外，所以跨轮次有效）。
TODO = TodoManager()


def run_todo_write(todos: list | str) -> str:
    # 工具入口：只负责调用管理器并把渲染结果同时打印给用户。
    try:
        output = TODO.update(todos)
    except ValueError as e:
        # 校验失败不抛异常，而是把错误文本回给模型，让它自己修正重试。
        return f"Error: {e}"
    # 黄色打印清单，让人也能看到 Agent 的当前计划。
    print(f"\n\033[33m## Current Tasks\033[0m\n{output}")
    return output

# 工具定义：前五个沿用 s02-s04，末尾新增 todo_write。
# 它的 input_schema 用 maxItems/minLength/enum 把约束写进 schema，
# 相当于在「调用之前」就有一层约束，而 TodoManager 里的校验是第二层兜底。
TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern; ** matches recursively.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
    # s05: new tool
    {"name": "todo_write", "description": "Create and manage a task list for your current coding session.",
     "input_schema": {"type": "object", "properties": {"todos": {"type": "array", "maxItems": 20, "items": {"type": "object", "properties": {"content": {"type": "string", "minLength": 1}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}, "required": ["content", "status"]}}}, "required": ["todos"]}},
]

# 分发表多了一个条目：todo_write -> run_todo_write。
TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob, "todo_write": run_todo_write,
}


# -- Hook system from s04 --
# 钩子系统与 s04 相同（含 s03 的权限逻辑），本课不再改动。

# 四个事件各对应一个回调列表。
HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}

def register_hook(event: str, callback):
    # 注册：把回调追加到对应事件的列表。
    HOOKS[event].append(callback)

def trigger_hooks(event: str, *args):
    # 触发：依次调用回调；返回非 None 表示拦截，立即停止并返回该结果。
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None

DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]
DESTRUCTIVE_COMMAND_WORD = re.compile(
    # 匹配作为独立命令出现的 rm/del（前置边界避免误伤 "firm" 这类子串）。
    r"(?i)(?:^|[;&|()\n])\s*(?:rm|del)(?=\s|$|[;&|()])"
)
DESTRUCTIVE = ["rm ", "> /etc/", "chmod 777"]


def contains_destructive_command(command: str) -> bool:
    return bool(DESTRUCTIVE_COMMAND_WORD.search(command))


def permission_hook(block):
    """PreToolUse: s03 permission logic, registered as an s04 hook."""
    # 工具执行前：bash 先查硬黑名单，再查危险规则；文件工具检查越界。
    if block.name == "bash":
        command = block.input.get("command", "")
        for pattern in DENY_LIST:
            if pattern in command:
                # 红色打印拦截原因；返回非 None 即阻止执行。
                print(f"\n\033[31m[blocked] '{pattern}'\033[0m")
                return "Permission denied by deny list"
        if contains_destructive_command(command) or any(
            keyword in command for keyword in DESTRUCTIVE
        ):
            # 黄色提示 + 展示完整参数，让用户看清要执行什么。
            print(f"\n\033[33m[permission] Potentially destructive command\033[0m")
            print(f"   Tool: {block.name}({block.input})")
            # 默认拒绝：直接回车 = N（fail-safe）。
            choice = input("   Allow? [y/N] ").strip().lower()
            if choice not in ("y", "yes"):
                return "Permission denied by user"
    if block.name in ("read_file", "write_file", "edit_file"):
        # 文件工具：路径解析后落在工作目录之外，就打断询问用户。
        path = block.input.get("path", "")
        if not (WORKDIR / path).resolve().is_relative_to(WORKDIR):
            print(f"\n\033[33m[permission] Access outside workspace\033[0m")
            print(f"   Tool: {block.name}({block.input})")
            choice = input("   Allow? [y/N] ").strip().lower()
            if choice not in ("y", "yes"):
                return "Permission denied by user"
    return None

def log_hook(block):
    """PreToolUse: log every tool call."""
    # 只观测不干预：灰色打印工具名与前两个参数（截断 60 字符），返回 None 放行。
    args_preview = str(list(block.input.values())[:2])[:60]
    print(f"\033[90m[HOOK] {block.name}({args_preview})\033[0m")
    return None

def large_output_hook(block, output):
    """PostToolUse: warn on large output."""
    # 执行后告警：输出超过 10 万字符就提醒一句，不修改结果。
    if len(str(output)) > 100000:
        print(f"\033[33m[HOOK] Large output from {block.name}: {len(str(output))} chars\033[0m")
    return None

def context_inject_hook(query: str):
    """UserPromptSubmit: log working directory."""
    # 用户输入送进 LLM 前触发（这里只是打印工作目录，实际项目可用于注入上下文）。
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {WORKDIR}\033[0m")
    return None

def summary_hook(messages: list):
    """Stop: print tool call count."""
    # Stop 钩子：统计本次会话的工具调用次数（数 tool_result 块的个数）。
    # 只有 dict 形态的块才算——assistant 消息里存的是 SDK 对象而非 dict。
    tool_count = sum(1 for m in messages
                     for b in (m.get("content") if isinstance(m.get("content"), list) else [])
                     if isinstance(b, dict) and b.get("type") == "tool_result")
    print(f"\033[90m[HOOK] Stop: session used {tool_count} tool calls\033[0m")
    return None

# 注册全部钩子（五条，与 s04 相同）。
register_hook("UserPromptSubmit", context_inject_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", summary_hook)


# -- Agent loop with the reminder counter --
# 带「提醒计数器」的 Agent 循环：这是 s05 相对 s04 的真正改动所在。

def agent_loop(messages: list):
    # 计数器：距离上一次调用 todo_write 已经过了多少轮（初始为 0）。
    rounds_since_todo = 0
    while True:
        # 【第 1 步】把对话历史 + 6 个工具（含 todo_write）发给模型。
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        # 【第 2 步】模型回复追加为 assistant 消息。
        messages.append({"role": "assistant", "content": response.content})

        # 【第 3 步】筛出 tool_use 块；没有则触发 Stop 钩子后收尾。
        tool_calls = [
            block for block in response.content if block.type == "tool_use"
        ]
        if not tool_calls:
            # Stop 钩子返回非 None 时，把它当作 user 消息塞回去继续循环。
            force = trigger_hooks("Stop", messages)
            if force:
                messages.append({"role": "user", "content": force})
                continue
            return

        # 【第 4 步】逐个执行工具调用。
        results = []
        used_todo = False  # 记录本轮是否调用过 todo_write
        for block in tool_calls:
            # 执行前先过 PreToolUse 钩子（权限 + 日志）。
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                # 被拦下：不执行工具，但必须回填 tool_result（API 要求一一对应）。
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": str(blocked)})
                continue

            handler = TOOL_HANDLERS.get(block.name)
            try:
                # 相比 s04，这里多了 try/except：工具抛异常也不会打断整个循环，
                # 而是把异常文本当输出交回模型（例如 TodoManager 的校验报错）。
                output = handler(**block.input) if handler else f"Unknown: {block.name}"
            except Exception as e:
                output = f"Error: {e}"

            # 执行后触发 PostToolUse 钩子（可拿到输出做告警/审计）。
            trigger_hooks("PostToolUse", block, output)

            if block.name == "todo_write":
                used_todo = True

            # 结果统一转成字符串再回填，保证 content 一定是文本。
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": str(output)})

        # 【第 5 步】更新提醒计数器：本轮用过 todo_write 就清零，否则 +1。
        rounds_since_todo = 0 if used_todo else rounds_since_todo + 1
        if rounds_since_todo >= 3:
            # 连续 3 轮没更新待办 → 在工具结果里追加一条 text 块作为提醒。
            # 注意这是在「user 消息」的 content 数组里塞 text 块：
            # 对模型来说它就像随结果一起进来的批注，能温和地把注意力拉回计划上。
            results.append({"type": "text",
                            "text": "<reminder>Update your todos.</reminder>"})
            rounds_since_todo = 0  # 提醒过就重置，避免每轮都唠叨

        # 把结果（可能含提醒）作为 user 消息送回，循环回到顶部继续推理。
        messages.append({"role": "user", "content": results})


# 程序入口：只有直接运行本文件时才执行下面这段。
if __name__ == "__main__":
    print("s05: TodoWrite - plan before execution")
    print("Enter a question, press Enter to send. Type q to quit.\n")

    history = []  # 整段对话历史，传给 agent_loop 后由它就地推进
    while True:   # 外层交互循环：反复读用户输入，直到用户退出
        try:
            # \001/\002 tell Readline the ANSI escapes have zero display width.
            # \001/\002 是 readline 的「不可见定界符」：夹在中间的 ANSI 转义码
            # 不占显示宽度，这样青色提示符才不会让光标错位。
            query = input("\001\033[36m\002s05 >> \001\033[0m\002")
        except (EOFError, KeyboardInterrupt):
            # Ctrl+D（EOF）或 Ctrl+C（中断）时退出。
            break
        # 输入 q / exit / 空行都退出。
        if query.strip().lower() in ("q", "exit", ""):
            break
        # 用户输入入历史之前先触发 UserPromptSubmit 钩子。
        trigger_hooks("UserPromptSubmit", query)
        history.append({"role": "user", "content": query})
        agent_loop(history)
        # 打印模型最后的文字回复（跳过 tool_use 块）。
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(block.text)
        # 空行，让下一条提示符清爽地开始。
        print()
