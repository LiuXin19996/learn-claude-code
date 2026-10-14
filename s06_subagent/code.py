#!/usr/bin/env python3
"""
s06_subagent.py - Subagents
（子 Agent）

The task tool runs a second agent loop with a fresh message list. Both
loops share the working directory, but only the final text returns to
the parent conversation.
（task 工具会用一个全新的消息列表再跑一遍 Agent 循环。两个循环共用同一个
  工作目录，但只有子 Agent 的最终文本会返回给父对话——中间过程全部丢弃。）

    Parent agent                    Subagent
    +------------------+            +------------------+
    | messages=[...]   |            | messages=[prompt]|
    |                  |   task     |                  |
    | tool: task       | ---------> | own agent loop   |
    |                  |            | base tools only  |
    | tool_result      | <--------- | final text       |
    +------------------+            +------------------+
    （父 Agent 的 messages 里原本有几百轮历史；子 Agent 的 messages 里
      只有一条 prompt。它的工具执行结果都留在自己那边，不污染父对话。
      "base tools only" 指子 Agent 拿不到 task 工具。）

The subagent has no task tool, so it cannot delegate again.
（子 Agent 没有 task 工具，所以它无法再次委派——递归深度天然被限制为 1。）
"""

import os          # 操作系统相关功能：读环境变量、判断平台
import re          # 正则表达式：危险命令模式匹配（s03 起沿用）
import subprocess  # 子进程管理：执行模型生成的 shell 命令
from pathlib import Path  # 面向对象的路径操作：拼路径、解析、判断越界

# readline 提供命令行编辑能力（历史记录、方向键、退格等）。
# 它在 Unix 上通常可用，Windows 上可能不存在，所以用 try/except 包裹。
try:
    import readline
    # 以下四行是 macOS 上 libedit 库的 UTF-8 修复，让终端能正确处理中文。
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
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

WORKDIR = Path.cwd()  # 工作目录：父子两个循环共用同一个（所以文件改动彼此可见）
# 创建 Anthropic 客户端；base_url 默认官方 API，配了环境变量则走自定义端点。
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
# 从环境变量读模型 ID；用 [] 取值，.env 里没配会直接抛 KeyError。
MODEL = os.environ["MODEL_ID"]

# 根据操作系统告诉模型它面对的 shell 语法，父子 Agent 共用同一段环境说明。
ENVIRONMENT_PROMPT = (
    "Windows: the bash tool runs through cmd.exe; use cmd.exe syntax, not Unix "
    "Bash or PowerShell syntax, and prefer dedicated file tools for file operations"
    if os.name == "nt"
    else "Unix-like: the bash tool runs the system shell"
)
# 父 Agent 的系统提示词：除了「用工具干活」，还告诉它可以用 task 做聚焦探索。
SYSTEM = (
    f"You are a coding agent at {WORKDIR}. Environment: {ENVIRONMENT_PROMPT}. "
    "Use task for focused exploration or a self-contained subtask."
)
# 子 Agent 的系统提示词：注意这里**没有**提 task 工具——因为子 Agent 也没有。
# 它只被要求「完成任务并把最终答案简洁地返回」，不需要往上汇报中间过程。
SUB_SYSTEM = (
    f"You are a coding agent at {WORKDIR}. Environment: {ENVIRONMENT_PROMPT}. "
    "Complete the given task, then return a concise final answer."
)


# -- Base tools --
# 五个基础工具实现与 s02-s05 相同（s06 对它们没做功能改动）。

def run_bash(command: str) -> str:
    # 执行命令：shell=True 交给系统 shell，cwd=WORKDIR 固定工作目录，
    # capture_output 捕获输出，text=True 按文本返回，
    # errors="replace" 防解码崩溃，timeout=120 防卡死。
    try:
        result = subprocess.run(
            command, shell=True, cwd=WORKDIR,
            capture_output=True, text=True, errors="replace", timeout=120,
        )
        # 合并 stdout/stderr 并去空白；截断到 50000 字符防止撑爆上下文。
        output = (result.stdout + result.stderr).strip()
        return output[:50000] if output else "(no output)"
    except subprocess.TimeoutExpired:
        # 超时：返回提示让模型自行调整策略。
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int | None = None) -> str:
    # 读文件：相对路径拼到 WORKDIR 后 resolve（折叠 ../ 等）。
    try:
        lines = (WORKDIR / path).resolve().read_text(encoding="utf-8").splitlines()
        # limit：只读前 N 行；截断时附提示说明还剩多少行。
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
    import glob
    try:
        matches = sorted({
            match for match in glob.glob(
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


# 基础工具清单 = s02-s05 的那五个（不含 todo_write，也不含 task）。
# 命名改成 BASE_ 前缀，是为了后面能派生出「父工具集」和「子工具集」两份。
BASE_TOOLS = [
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
]

# 基础工具的分发表（工具名 -> 处理函数）。
BASE_HANDLERS = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
}


# -- Hooks --
# 钩子系统与 s04/s05 相同（含 s03 的权限逻辑）。
# 注意：HOOKS 是全局的，所以**父子两个 Agent 的工具调用都会过同一套钩子**——
# 子 Agent 想执行 rm 之类的高危命令，同样会弹出来问你。

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
    """PreToolUse: block denied operations and ask about risky ones."""
    # 工具执行前：bash 先查硬黑名单，再查危险规则；文件工具检查是否越界。
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
            print("\n\033[33m[permission] Potentially destructive command\033[0m")
            print(f"   Tool: {block.name}({block.input})")
            # 默认拒绝：直接回车 = N（fail-safe）。
            choice = input("   Allow? [y/N] ").strip().lower()
            if choice not in ("y", "yes"):
                return "Permission denied by user"

    if block.name in ("read_file", "write_file", "edit_file"):
        # 文件工具：路径解析后落在工作目录之外，就打断询问用户。
        path = block.input.get("path", "")
        if not (WORKDIR / path).resolve().is_relative_to(WORKDIR):
            print("\n\033[33m[permission] Access outside workspace\033[0m")
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
    """UserPromptSubmit: log the working directory."""
    # 用户输入送进 LLM 前触发（这里只是打印工作目录）。
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {WORKDIR}\033[0m")
    return None


def summary_hook(messages: list):
    """Stop: print the number of tool results in this message list."""
    # Stop 钩子：统计该消息列表里的工具调用次数（数 tool_result 块的个数）。
    # 注意它接收的是 messages 参数——父子循环各自触发时传的是各自的历史。
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


# 注册全部钩子（五条，与 s04/s05 相同）。
register_hook("UserPromptSubmit", context_inject_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", summary_hook)


# -- Shared tool execution --
# s06 的小重构：把「权限检查 + 执行 + 事后钩子」抽成一个函数，
# 父子两个循环都调用它，避免这段逻辑写两遍。
def execute_tool(block, handlers: dict) -> str:
    # 执行前过 PreToolUse 钩子；被拦就把它返回的原因当作结果。
    blocked = trigger_hooks("PreToolUse", block)
    if blocked:
        return str(blocked)

    # 从传入的 handlers 里查处理函数——这正是父子差异的注入点：
    # 父传 TOOL_HANDLERS（含 task），子传 SUB_HANDLERS（不含 task）。
    handler = handlers.get(block.name)
    try:
        # 工具抛异常不打断循环，转成文本交给模型（例如路径校验报错）。
        output = handler(**block.input) if handler else f"Unknown: {block.name}"
    except Exception as e:
        output = f"Error: {e}"

    # 执行后触发 PostToolUse 钩子（可拿到输出做告警/审计）。
    trigger_hooks("PostToolUse", block, output)
    return str(output)


# -- New in s06: a nested agent loop with fresh messages --
# s06 新增：一个用全新消息列表的「嵌套」Agent 循环——这就是子 Agent。

# 子 Agent 的工具集：从基础工具复制一份（list/dict 浅拷贝）。
# 关键点：它里面没有 task，所以子 Agent 无法再派出子 Agent，递归到此为止。
SUB_TOOLS = list(BASE_TOOLS)
SUB_HANDLERS = dict(BASE_HANDLERS)


def extract_text(content) -> str:
    # 从模型回复的 content 块列表里抽出纯文本，丢掉 tool_use 块。
    # 用 getattr 而非下标，因为这里是 SDK 对象（不是 dict）。
    if not isinstance(content, list):
        return str(content)
    return "\n".join(
        getattr(block, "text", "")
        for block in content
        if getattr(block, "type", None) == "text"
    )


def run_subagent(prompt: str) -> str:
    # 这是一个「工具处理函数」，但它内部自己跑了一整套 Agent 循环。
    print("\n\033[35m[Subagent started]\033[0m")
    # 全新、独立的对话历史：只有一条 prompt。
    # 父对话的历史不会传进来——这就是上下文隔离，也是子 Agent 存在的意义。
    messages = [{"role": "user", "content": prompt}]

    # 注意是 for _ in range(30) 而非 while True：
    # 子 Agent 不死不休的代价很高（烧 token 且父 Agent 在阻塞等待），
    # 所以给它一个硬上限，最多 30 轮就必须收工。
    for _ in range(30):
        response = client.messages.create(
            model=MODEL,
            system=SUB_SYSTEM,   # 用子 Agent 自己的系统提示词
            messages=messages,
            tools=SUB_TOOLS,     # 只给基础工具
            max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})

        tool_calls = [
            block for block in response.content if block.type == "tool_use"
        ]
        if not tool_calls:
            # 子 Agent 也支持 Stop 钩子强行续命（与父循环逻辑一致）。
            force = trigger_hooks("Stop", messages)
            if force:
                messages.append({"role": "user", "content": force})
                continue
            print("\033[35m[Subagent done]\033[0m")
            # 只把「最终文本」返回给父 Agent；中间的工具输出全部丢弃。
            # 抽取为空时给个占位说明，避免父 Agent 收到空字符串不知所措。
            return extract_text(response.content) or "(no summary)"

        results = []
        for block in tool_calls:
            # 子 Agent 的工具调用同样走权限钩子，安全性不打折。
            output = execute_tool(block, SUB_HANDLERS)
            # 灰色缩进打印，方便在终端上区分「父在做」还是「子在探索」。
            print(f"  \033[90m[sub] {block.name}: {output[:100]}\033[0m")
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": output,
            })
        messages.append({"role": "user", "content": results})

    # 跑到 30 轮还没给出最终答案：返回一句说明，而不是让父 Agent 卡死。
    print("\033[35m[Subagent stopped]\033[0m")
    return "Subagent stopped after 30 turns without a final answer."


# task 工具的定义：模型看到它就知道「可以派一个子 Agent 去干这件事」。
TASK_TOOL = {
    "name": "task",
    "description": "Run a subagent with fresh conversation context and return its final text.",
    "input_schema": {
        "type": "object",
        "properties": {"prompt": {"type": "string", "minLength": 1}},
        "required": ["prompt"],
    },
}

# 父 Agent 的工具集 = 基础工具 + task；分发表同理多一项。
# 下面这两行就是「父子差异」的最终体现：
#   * 展开（*、**）而不是修改原对象，保证 BASE_TOOLS/BASE_HANDLERS 不被污染，
#     子 Agent 才能拿到干净的那一份。
#   * "task": run_subagent 把「跑一个 Agent 循环」注册成了普通工具，
#     所以主循环完全不需要知道子 Agent 的存在。
TOOLS = [*BASE_TOOLS, TASK_TOOL]
TOOL_HANDLERS = {**BASE_HANDLERS, "task": run_subagent}


# -- Parent agent loop --
# 父 Agent 循环：结构与 s04/s05 相同，只是工具执行换成了共享的 execute_tool()。

def agent_loop(messages: list):
    # messages 是父对话历史，函数就地修改它、无返回值。
    while True:
        # 【第 1 步】把父对话历史 + 全部工具（含 task）发给模型。
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=TOOLS,
            max_tokens=8000,
        )
        # 【第 2 步】模型回复追加为 assistant 消息。
        messages.append({"role": "assistant", "content": response.content})

        # 【第 3 步】筛出 tool_use 块；没有则触发 Stop 钩子后收尾。
        tool_calls = [
            block for block in response.content if block.type == "tool_use"
        ]
        if not tool_calls:
            # Stop 钩子返回非 None 时，当作 user 消息塞回去继续循环。
            force = trigger_hooks("Stop", messages)
            if force:
                messages.append({"role": "user", "content": force})
                continue
            return

        # 【第 4 步】逐个执行工具。如果其中一个是 task，
        # execute_tool 会去调 run_subagent —— 于是一个完整的子循环在这里展开，
        # 等它返回最终文本后，父 Agent 只看到一条普通的 tool_result。
        results = []
        for block in tool_calls:
            output = execute_tool(block, TOOL_HANDLERS)
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": output,
            })
        # 【第 5 步】把结果作为 user 消息送回，循环回到顶部继续推理。
        messages.append({"role": "user", "content": results})


# 程序入口：只有直接运行本文件时才执行下面这段。
if __name__ == "__main__":
    print("s06: Subagent - fresh messages, final text returns")
    print("Enter a question, press Enter to send. Type q to quit.\n")

    history = []  # 父对话历史，传给 agent_loop 后由它就地推进
    while True:   # 外层交互循环：反复读用户输入，直到用户退出
        try:
            # \001/\002 tell Readline the ANSI escapes have zero display width.
            # \001/\002 是 readline 的「不可见定界符」：夹在中间的 ANSI 转义码
            # 不占显示宽度，这样青色提示符才不会让光标错位。
            query = input("\001\033[36m\002s06 >> \001\033[0m\002")
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
