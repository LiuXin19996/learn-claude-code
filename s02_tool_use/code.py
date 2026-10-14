#!/usr/bin/env python3
"""
s02_tool_use.py - Tools
（工具）

The agent loop from s01 does not change. This lesson adds four tools
and a dispatch map:
（s01 里的 Agent 循环完全没有变。本课新增四个工具，以及一张「分发表」：）

    +----------+      +-------+      +--------------------------+
    |   User   | ---> |  LLM  | ---> | Tool Dispatch            |
    |  prompt  |      |       |      | bash       -> run_bash   |
    +----------+      +---+---+      | read_file  -> run_read   |
                          ^          | write_file -> run_write  |
                          |          | edit_file  -> run_edit   |
                          +----------+ glob       -> run_glob   |
                          tool_result+--------------------------+

  + run_read / run_write / run_edit / run_glob
  + TOOL_HANDLERS instead of a hard-coded run_bash call
  + safe_path to keep file tools inside the workspace
（新增 run_read / run_write / run_edit / run_glob 四个文件工具；
   用 TOOL_HANDLERS 分发表取代 s01 里写死的 run_bash 调用；
   用 safe_path 把文件工具限制在工作目录内，防止越界读写。）

Key insight: the loop stays the same; only tool registration and dispatch grow.
（核心洞见：循环本身不变，增长的只是「工具的注册与分发」。）
"""

import os              # 操作系统相关功能：读环境变量、判断平台（os.name）
import subprocess      # 子进程管理：用来真正执行模型生成的 shell 命令
from pathlib import Path  # 面向对象的路径操作：安全的读写、glob、相对路径判断

# readline 提供命令行编辑能力（历史记录、方向键、退格等）。
# 它在 Unix 上通常可用，但在 Windows 上可能不存在，所以用 try/except 包裹。
try:
    import readline
    # 以下四行是 macOS 上 libedit 库的 UTF-8 修复，
    # 让终端能正确处理中文等多字节字符的输入/输出。
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
except ImportError:
    # 没有 readline（例如 Windows 自带的 Python）就直接跳过，不影响主逻辑。
    pass

from anthropic import Anthropic    # Anthropic 官方 Python SDK 的客户端类
from dotenv import load_dotenv     # 从 .env 文件加载环境变量（如 API Key）

# override=True：即使系统里已经存在同名环境变量，也用 .env 里的值覆盖它。
load_dotenv(override=True)

# 如果配了 ANTHROPIC_BASE_URL（走代理/自定义端点），
# 就移除可能打架的 ANTHROPIC_AUTH_TOKEN，避免两种鉴权方式冲突。
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()  # 工作目录（workspace 根）：文件工具的「沙箱边界」就靠它来划定
# 创建 Anthropic 客户端；base_url 默认为官方 API，配了环境变量则走自定义端点。
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
# 从环境变量读取模型 ID；用 [] 取值，若 .env 里没配会直接抛 KeyError。
MODEL = os.environ["MODEL_ID"]

# 根据操作系统告诉模型它面对的 shell 是什么语法，
# 这样它才知道该生成 cmd.exe 还是 Unix shell 的命令。
ENVIRONMENT_PROMPT = (
    "Windows: the bash tool runs through cmd.exe; use cmd.exe syntax, not Unix "
    "Bash or PowerShell syntax, and prefer dedicated file tools for file operations"
    if os.name == "nt"
    else "Unix-like: the bash tool runs the system shell"
)

# 系统提示词：给模型设定身份与行为约束——
# 「你是运行在 WORKDIR 下的编程 Agent，用工具解决问题，直接动手、少解释」。
SYSTEM = (
    f"You are a coding agent at {WORKDIR}. Environment: {ENVIRONMENT_PROMPT}. "
    "Use tools to solve tasks. Act, don't explain."
)


# -- From s01 (unchanged) --
# 以下 bash 工具与 s01 完全相同，唯一区别是 cwd 从 os.getcwd() 换成了 WORKDIR。

def run_bash(command: str) -> str:
    # 危险命令黑名单：只要命令里包含这些片段就直接拒绝执行。
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        # 在子进程中执行命令：
        #   shell=True          -> 交给系统 shell 解释（支持管道、重定向）
        #   cwd=WORKDIR         -> 固定在工作目录下执行
        #   capture_output=True -> 捕获 stdout / stderr
        #   text=True           -> 以文本返回而非字节
        #   errors="replace"    -> 无法解码的字符用替代符，避免崩溃
        #   timeout=120         -> 最多跑 120 秒，防止命令卡死
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, errors="replace",
                           timeout=120)
        # stdout + stderr 合并后去掉首尾空白。
        out = (r.stdout + r.stderr).strip()
        # 有输出就返回（截断到 50000 字符，防止超长内容撑爆上下文）；没输出给个占位提示。
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        # 超时单独捕获，返回明确提示让模型知道该换策略。
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        # 命令不存在等系统级错误：把错误信息交回模型，让它自行调整。
        return f"Error: {e}"


# -- New in s02: four tools --
# s02 新增：四个「文件工具」。相比 bash，它们更可控、更可审计，
# 而且带路径校验和长度限制，专门服务于读、写、改、找文件这四类高频操作。

def safe_path(p: str) -> Path:
    # 把用户/模型给的相对路径拼到工作目录上，并解析成绝对路径（含 ../ 的折叠）。
    path = (WORKDIR / p).resolve()
    # 关键安全检查：解析后的路径必须仍在工作目录内，
    # 否则说明有人用 ../../etc/passwd 之类的写法试图「逃逸」出沙箱。
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_read(path: str, limit: int | None = None) -> str:
    # 读文件工具：没有这个工具，模型只能靠 bash 的 cat/type，既不好限制也不好跨平台。
    try:
        # 先过 safe_path 校验，再按 UTF-8 读取并拆成行列表。
        lines = safe_path(path).read_text(encoding="utf-8").splitlines()
        # limit：只读前 N 行，用来避免一次把超大文件全部灌进上下文。
        # 截断时额外附一行提示，告诉模型「后面还有多少行」。
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        # 统一兜底：文件不存在、编码错误、越界等都以 "Error: ..." 文本返回给模型。
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    # 写文件工具：整文件覆盖写入。
    try:
        file_path = safe_path(path)
        # parents=True 会按需创建中间目录，exist_ok=True 让目录已存在时不报错。
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        # 返回写入字节数，方便模型确认「确实写进去了」。
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    # 编辑工具：只替换「第一处」精确匹配的文本，比整体重写更省 token、更安全。
    try:
        file_path = safe_path(path)
        text = file_path.read_text(encoding="utf-8")
        # 找不到 old_text 就明确报错——提醒模型必须提供原文中真实存在的片段。
        if old_text not in text:
            return f"Error: text not found in {path}"
        # 第三个参数 1 表示「只替换一次」，避免误伤文件里其它相同片段。
        file_path.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


def run_glob(pattern: str) -> str:
    # 查找文件工具：按 glob 模式列文件，** 表示递归匹配，让模型先「定位」再「读取」。
    import glob as g
    try:
        # 用集合去重后再排序，保证输出稳定；root_dir 限定搜索范围在工作目录内，
        # 后面的 is_relative_to 再兜底过滤掉任何越界结果（例如符号链接指到外面）。
        matches = sorted({
            match for match in g.glob(
                pattern, root_dir=WORKDIR, recursive=True)
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR)
        })
        # 最多展示 200 条，防止结果过多刷屏、吃掉上下文。
        shown = matches[:200]
        if len(matches) > 200:
            shown.append("... (more matches omitted; narrow the pattern)")
        return "\n".join(shown) if shown else "(no matches)"
    except Exception as e:
        return f"Error: {e}"


# -- New in s02: tool definitions (one tool in s01, five in s02) --
# s02 新增：工具定义列表——s01 只有 1 个工具，s02 变成 5 个。
# 这些 schema 会随请求一起发给模型，模型据此决定「要不要调用、传什么参数」。

TOOLS = [
    # 每个工具都是「name + description + input_schema」三件套：
    #   description  让模型知道这个工具是干什么的
    #   input_schema 约束参数的 JSON Schema，required 表示必填字段
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

# -- New in s02: dispatch map (replaces s01's hard-coded run_bash call) --
# s02 新增：分发表——「工具名 -> 处理函数」的字典映射，
# 取代 s01 里写死的 run_bash 调用，循环因此不用再认识任何具体工具。

TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob,
}


# -- The agent loop keeps the same shape as s01; only dispatch changes --
# Agent 循环的形状与 s01 完全一致，唯一变化的是「分发」这一步：
#   s01: output = run_bash(block.input["command"])
#   s02: output = TOOL_HANDLERS[block.name](**block.input)

def agent_loop(messages: list):
    # messages 是完整对话历史，函数就地修改它、无返回值（与 s01 相同）。
    while True:
        # 【第 1 步】把对话历史 + 5 个工具定义发给模型。
        #   max_tokens=8000 限制本次回复最多生成多少 token。
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        # 【第 2 步】把模型回复（可能含 text / tool_use 块）作为 assistant 消息追加进历史。
        messages.append({"role": "assistant", "content": response.content})

        # 【第 3 步】筛出所有 tool_use 块；一个都没有，说明模型已给出最终回答，结束循环。
        tool_calls = [
            block for block in response.content if block.type == "tool_use"
        ]
        if not tool_calls:
            return

        # 【第 4 步】逐个执行工具调用并收集结果。
        results = []
        for block in tool_calls:
            # 用黄色打印「工具名」，便于观察循环过程（s01 打印的是具体命令）。
            print(f"\033[33m> {block.name}\033[0m")
            # 查分发表拿到对应处理函数；查不到就返回 Unknown，而不是直接崩溃。
            handler = TOOL_HANDLERS.get(block.name)
            # **block.input 把模型给的参数字典按关键字展开，例如 run_read(path="a.txt")。
            output = handler(**block.input) if handler else f"Unknown: {block.name}"
            # 只打印前 200 字符，避免结果太长刷屏。
            print(str(output)[:200])
            # 每条结果都按 Anthropic 的 tool_result 格式回填：
            #   tool_use_id 关联到请求它的那个 tool_use 块（block.id）
            #   content     是工具的实际输出
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})

        # 【第 5 步】把工具结果作为 user 消息送回，循环回到顶部继续下一轮推理。
        messages.append({"role": "user", "content": results})


# 程序入口：只有直接运行本文件时才执行下面这段。
if __name__ == "__main__":
    print("s02: Tool Use - four tools added to s01")
    print("Enter a question, press Enter to send. Type q to quit.\n")

    history = []  # 保存整段对话历史，传给 agent_loop 后由它就地推进
    while True:   # 外层交互循环：反复读用户输入，直到用户退出
        try:
            # \001/\002 tell Readline the ANSI escapes have zero display width.
            # \001/\002 是 readline 的「不可见定界符」：夹在中间的 ANSI 转义码
            # 不占显示宽度，这样青色提示符才不会让光标位置错乱。
            query = input("\001\033[36m\002s02 >> \001\033[0m\002")
        except (EOFError, KeyboardInterrupt):
            # Ctrl+D（EOF）或 Ctrl+C（中断）时退出。
            break
        # 输入 q / exit / 空行都退出。
        if query.strip().lower() in ("q", "exit", ""):
            break
        # 把用户输入加入历史，再交给 agent_loop 干活（可能触发多轮工具调用）。
        history.append({"role": "user", "content": query})
        agent_loop(history)
        # 打印模型最后的文字回复（跳过 tool_use 块）。
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(block.text)
        # 空行，让下一条提示符清爽地开始。
        print()
