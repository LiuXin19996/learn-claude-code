#!/usr/bin/env python3
"""
s03_permission.py - Permission System
（权限系统）

Three gates inserted before tool execution:
（在工具真正执行之前，插入三道「关卡」（gate）：）

    Gate 1: Hard deny list (rm -rf /, sudo, ...)          【第 1 关】硬黑名单：无条件禁止
    Gate 2: Rule matching (write outside workspace? destructive cmd?)  【第 2 关】规则匹配：越界写？危险命令？
    Gate 3: User approval (pause and wait for confirmation)  【第 3 关】用户审批：暂停下来，等用户确认

    +----------+      +-------+      +--------------+      +---------------+
    |   User   | ---> |  LLM  | ---> | Permission   | ---> | Tool Dispatch |
    |  prompt  |      |       |      | 1. deny list |      | execute       |
    +----------+      +---+---+      | 2. rules     |      +-------+-------+
                          ^          | 3. approval  |              |
                          |          +------+-------+              |
                          |                 | deny                 |
                          |                 v                      v
                          |          +-------------------------------+
                          +----------+ tool_result: denied or output |
                                     +-------------------------------+
    （上图：模型请求的工具先经过权限流水线；被拒绝就原路返回一条
      「denied」结果，通过才真正执行。无论哪种，结果都回流给模型。）

Only one line added to the agent loop:
（相对 s02，Agent 循环只多了一行判断：）

    if not check_permission(block):
        continue

Builds on s02 (multi-tool). Usage:
（在 s02 多工具的基础上继续。用法：）

    python s03_permission/code.py
    Needs: pip install anthropic python-dotenv + ANTHROPIC_API_KEY in .env
"""

import os          # 操作系统相关功能：读环境变量、判断平台
import re          # 正则表达式：用于第 2 关「危险命令」的模式匹配
import subprocess  # 子进程管理：真正执行模型生成的 shell 命令
from pathlib import Path  # 面向对象的路径操作：拼路径、解析、判断是否越界

# readline 提供命令行编辑能力（历史记录、方向键、退格等）。
# 它在 Unix 上通常可用，Windows 上可能不存在，所以用 try/except 包裹。
try:
    import readline
    # 以下四行是 macOS 上 libedit 库的 UTF-8 修复，让终端能正确
    # 处理中文等多字节字符的输入与退格删除。
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
except ImportError:
    # 没有 readline（例如 Windows 自带 Python）就直接跳过，不影响主逻辑。
    pass

from anthropic import Anthropic    # Anthropic 官方 Python SDK 的客户端类
from dotenv import load_dotenv     # 从 .env 文件加载环境变量（如 API Key）

# override=True：即使系统里已有同名环境变量，也用 .env 里的值覆盖。
load_dotenv(override=True)

# 配了 ANTHROPIC_BASE_URL（走代理/自定义端点）时，
# 移除可能打架的 ANTHROPIC_AUTH_TOKEN，避免两种鉴权方式冲突。
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()  # 工作目录：权限规则里判断「是否越界」的基准线
# 创建 Anthropic 客户端；base_url 默认官方 API，配了环境变量则走自定义端点。
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
# 从环境变量读模型 ID；用 [] 取值，.env 里没配会直接抛 KeyError。
MODEL = os.environ["MODEL_ID"]

# 根据操作系统告诉模型它面对的 shell 语法，避免它生成错误的命令风格。
ENVIRONMENT_PROMPT = (
    "Windows: the bash tool runs through cmd.exe; use cmd.exe syntax, not Unix "
    "Bash or PowerShell syntax, and prefer dedicated file tools for file operations"
    if os.name == "nt"
    else "Unix-like: the bash tool runs the system shell"
)

# 系统提示词：相比 s02 多了一句「所有破坏性操作都需要用户批准」，
# 让模型提前知道有些操作会被拦下来，从而自觉地少做危险动作。
SYSTEM = (
    f"You are a coding agent at {WORKDIR}. Environment: {ENVIRONMENT_PROMPT}. "
    "All destructive operations require user approval."
)


# -- From s02: tool implementations --
# 沿用 s02 的五个工具实现。注意两个与 s02 的重要差异：
#   1) run_bash 里 s02 的「危险命令黑名单」被删掉了——它升级成了这里的第 1 关（Gate 1）。
#   2) 文件工具不再调用 safe_path 硬拦截，改成在第 2 关用规则检测越界，
#      然后交给用户审批。也就是说：s03 把「硬阻止」换成了「可解释 + 可确认」。

def run_bash(command: str) -> str:
    try:
        # 在子进程里执行命令：shell=True 交给系统 shell 解释，
        # cwd=WORKDIR 固定工作目录，capture_output 捕获 stdout/stderr，
        # text=True 按文本返回，errors="replace" 避免解码崩溃，
        # timeout=120 防止命令卡死。
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, errors="replace", timeout=120)
        # stdout + stderr 合并去空白；截断到 50000 字符防撑爆上下文。
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        # 超时：返回明确提示，让模型知道该换策略（缩小范围、加过滤条件等）。
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int | None = None) -> str:
    # 读文件：把相对路径拼到 WORKDIR 上并 resolve（折叠掉 ../ 等）。
    try:
        lines = (WORKDIR / path).resolve().read_text(encoding="utf-8").splitlines()
        # limit 只读前 N 行；截断时附一行提示告诉模型还剩多少行。
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        # 文件不存在 / 编码错误等统一以 "Error: ..." 文本返回给模型。
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    # 写文件：整文件覆盖。
    try:
        file_path = (WORKDIR / path).resolve()
        # parents=True 按需创建中间目录，exist_ok=True 目录已存在也不报错。
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        # 返回字节数，方便模型确认写入成功。
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    # 编辑文件：只替换第一处精确匹配，比整文件重写更省 token 也更安全。
    try:
        file_path = (WORKDIR / path).resolve()
        text = file_path.read_text(encoding="utf-8")
        # 找不到原文片段就明确报错，提醒模型必须给出真实存在的文本。
        if old_text not in text:
            return f"Error: text not found in {path}"
        # replace 的第三个参数 1 = 只替换一次，避免误伤其它相同片段。
        file_path.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


def run_glob(pattern: str) -> str:
    # 查找文件：按 glob 模式列文件，** 表示递归匹配。
    import glob as g
    try:
        # 集合去重 + 排序保证输出稳定；root_dir 限定在工作目录内搜索，
        # 后面的 is_relative_to 再兜底过滤越界结果（如指向外部的符号链接）。
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


# -- From s02 (unchanged): tool definitions and dispatch --
# 沿用 s02：工具定义列表（发给模型，模型据此决定调用哪个工具、传什么参数）
# 和「工具名 -> 处理函数」的分发表。

TOOLS = [
    # 每个工具 = name + description + input_schema 三件套。
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

TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob,
}


# -- New in s03: three-gate permission pipeline --
# s03 新增：三道关卡串成的权限流水线。
# 设计要点是从「宽到窄」：先无条件硬拦，再看规则，最后才把判断权交给人。

# Gate 1: Hard deny list - always forbidden
# 第 1 关：硬黑名单——这些命令无论什么上下文都绝对禁止，不需要问任何人。
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=", "> /dev/sda"]

def check_deny_list(command: str) -> str | None:
    # 命中任一关键词就返回拒绝原因；全部没命中返回 None（表示「放行到下一关」）。
    for pattern in DENY_LIST:
        if pattern in command:
            return f"Blocked: '{pattern}' is on the deny list"
    return None


# Gate 2: Rule matching - context-dependent checks
# 第 2 关：规则匹配——是否危险取决于上下文，所以用规则/正则来判断，
# 判定的结果不是「直接拒绝」，而是「需要用户确认」。
DESTRUCTIVE_COMMAND_WORD = re.compile(
    # 这个正则匹配「作为一个独立命令出现」的 rm 或 del：
    #   (?i)                 忽略大小写
    #   (?:^|[;&|()\n])      前面必须是行首或命令分隔符，避免误伤 "firm" 这类子串
    #   \s*                  分隔符后允许有空格
    #   (?:rm|del)           命令本身
    #   (?=\s|$|[;&|()])     后面必须跟空格/行尾/分隔符（前瞻，不消耗字符）
    r"(?i)(?:^|[;&|()\n])\s*(?:rm|del)(?=\s|$|[;&|()])"
)


def contains_destructive_command(command: str) -> bool:
    # 简单包装：正则匹配到就说明含有删除类命令。
    return bool(DESTRUCTIVE_COMMAND_WORD.search(command))


# 规则表：每条规则说明「哪些工具 + 什么条件算可疑 + 给用户看的原因」。
PERMISSION_RULES = [
    # 规则一：文件工具的路径解析后落在 WORKDIR 之外 → 判定为「向工作区外写」。
    # （对应 s02 里 safe_path 的越界检查，只是这里不再直接抛异常，而是触发审批。）
    {"tools": ["read_file", "write_file", "edit_file"],
     "check": lambda args: not (WORKDIR / args.get("path", "")).resolve().is_relative_to(WORKDIR),
     "message": "Writing outside workspace"},
    # 规则二：bash 命令里有删除类指令，或出现 rm / 重定向到 /etc / chmod 777
    # 这类高风险写法 → 判定为「潜在破坏性命令」。
    {"tools": ["bash"],
     "check": lambda args: contains_destructive_command(args.get("command", "")) or
     any(kw in args.get("command", "") for kw in ["rm ", "> /etc/", "chmod 777"]),
     "message": "Potentially destructive command"},
]

def check_rules(tool_name: str, args: dict) -> str | None:
    # 逐条匹配规则：工具名在规则的 tools 列表里、且条件成立，就返回原因。
    # 没有任何规则命中，返回 None 表示「无需人工确认」。
    for rule in PERMISSION_RULES:
        if tool_name in rule["tools"] and rule["check"](args):
            return rule["message"]
    return None


# Gate 3: User approval - wait for confirmation after rule match
# 第 3 关：用户审批——只有前两关「没拦住但觉得可疑」时才会走到这里。
def ask_user(tool_name: str, args: dict, reason: str) -> str:
    # 黄色打印触发原因，并完整展示工具名和参数，让用户看清到底要执行什么。
    print(f"\n\033[33m[permission] {reason}\033[0m")
    print(f"   Tool: {tool_name}({args})")
    # 阻塞等待用户输入；注意默认是「拒绝」——直接回车 = N = 不放行（fail-safe）。
    choice = input("   Allow? [y/N] ").strip().lower()
    return "allow" if choice in ("y", "yes") else "deny"


# Pipeline: all three gates chained
# 流水线：把三道关卡按顺序串起来，返回 True 表示允许执行，False 表示拒绝。
def check_permission(block) -> bool:
    # 只有 bash 才过第 1 关（黑名单是针对命令字符串的，文件工具没有 command 参数）。
    if block.name == "bash":
        reason = check_deny_list(block.input.get("command", ""))
        if reason:
            # 红色打印拦截信息；直接返回 False，连问都不问用户。
            print(f"\n\033[31m[blocked] {reason}\033[0m")
            return False
    # 第 2 关：所有工具都过规则匹配。
    reason = check_rules(block.name, block.input)
    if reason:
        # 命中规则 → 第 3 关：暂停下来问用户，用户不同意就拒绝。
        decision = ask_user(block.name, block.input, reason)
        if decision == "deny":
            return False
    return True


# -- Agent loop: same as s02, with check_permission() inserted --
# Agent 循环与 s02 一致，只是在「执行」之前插入了 check_permission() 这道闸门。

def agent_loop(messages: list):
    # messages 是完整对话历史，函数就地修改它、无返回值。
    while True:
        # 【第 1 步】把对话历史 + 工具定义发给模型。
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        # 【第 2 步】模型回复（含 text / tool_use 块）追加为 assistant 消息。
        messages.append({"role": "assistant", "content": response.content})

        # 【第 3 步】筛出 tool_use 块；一个都没有说明模型已给出最终答案，结束循环。
        tool_calls = [
            block for block in response.content if block.type == "tool_use"
        ]
        if not tool_calls:
            return

        # 【第 4 步】逐个执行工具调用（执行前先过权限流水线），收集结果。
        results = []
        for block in tool_calls:
            # 青色打印工具名，便于观察循环过程。
            print(f"\033[36m> {block.name}\033[0m")

            # s03 change: run through permission pipeline before executing
            # s03 的关键改动：执行之前先过权限流水线。
            # 被拒绝时不执行工具，但仍然要回填一条 tool_result，
            # 因为 API 要求每个 tool_use 都必须有对应的 tool_result。
            if not check_permission(block):
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": "Permission denied."})
                continue

            # 查分发表拿处理函数；**block.input 按关键字展开参数字典。
            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"
            # 只打印前 200 字符，避免刷屏。
            print(str(output)[:200])
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})

        # 【第 5 步】把工具结果作为 user 消息送回，循环回到顶部继续推理。
        messages.append({"role": "user", "content": results})


# 程序入口：只有直接运行本文件时才执行下面这段。
if __name__ == "__main__":
    print("s03: Permission")
    print("Enter a question, press Enter to send. Type q to quit.\n")

    history = []  # 整段对话历史，传给 agent_loop 后由它就地推进
    while True:   # 外层交互循环：反复读用户输入，直到用户退出
        try:
            # \001/\002 tell Readline the ANSI escapes have zero display width.
            # \001/\002 是 readline 的「不可见定界符」：夹在中间的 ANSI 转义码
            # 不占显示宽度，这样青色提示符才不会让光标错位。
            query = input("\001\033[36m\002s03 >> \001\033[0m\002")
        except (EOFError, KeyboardInterrupt):
            # Ctrl+D（EOF）或 Ctrl+C（中断）时退出。
            break
        # 输入 q / exit / 空行都退出。
        if query.strip().lower() in ("q", "exit", ""):
            break
        # 用户输入入历史，交给 agent_loop 干活（期间可能弹出多次审批提示）。
        history.append({"role": "user", "content": query})
        agent_loop(history)
        # 打印模型最后的文字回复（跳过 tool_use 块）。
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(block.text)
        # 空行，让下一条提示符清爽地开始。
        print()
