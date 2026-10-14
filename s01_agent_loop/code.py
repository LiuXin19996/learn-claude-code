#!/usr/bin/env python3
"""
s01_agent_loop.py - The Agent Loop
（Agent 循环）

The entire secret of an AI coding agent in one pattern:
（AI 编程 Agent 的全部秘密，浓缩成一个模式（pattern）：）

    while True:
        response = LLM(messages, tools)
        if response contains no tool_use:
            break
        execute tools
        append results

    +----------+      +-------+      +---------+
    |   User   | ---> |  LLM  | ---> |  Tool   |
    |  prompt  |      |       |      | execute |
    +----------+      +---+---+      +----+----+
                          ^               |
                          |   tool_result |
                          +---------------+
                          (loop continues)

This is the core loop: feed tool results back to the model
until the model decides to stop. Later chapters add policy,
hooks, and lifecycle controls around it.
（这就是核心循环：把工具执行结果不断地喂回给模型，直到模型决定
「不再调用工具」为止。后面的章节会在这个循环之上叠加策略（policy）、
钩子（hooks）和生命周期控制（lifecycle controls）。）

Usage:
    pip install anthropic python-dotenv
    ANTHROPIC_API_KEY=... python s01_agent_loop/code.py
"""

import os          # 操作系统相关功能：读环境变量、获取当前目录等
import subprocess  # 子进程管理：用来真正执行模型生成的 shell 命令

# readline 提供命令行编辑能力（历史记录、方向键、退格等）。
# 它在 Unix 上通常可用，但在 Windows 上可能不存在，所以用 try/except 包裹。
try:
    import readline
    # #143 UTF-8 backspace fix for macOS libedit
    # 下面这几行是 macOS 上 libedit 库的 UTF-8 退格修复，
    # 让终端能正确处理中文字符的输入/输出与退格删除。
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
except ImportError:
    # 如果系统没有 readline（例如 Windows），直接忽略即可，不影响主逻辑。
    pass

from anthropic import Anthropic          # Anthropic 官方 Python SDK 的客户端类
from dotenv import load_dotenv           # 从 .env 文件加载环境变量（如 API Key）

# override=True：如果系统里已经存在同名环境变量，仍用 .env 里的值覆盖它。
load_dotenv(override=True)

# 如果设置了 ANTHROPIC_BASE_URL（用于走自定义/代理的 API 端点），
# 就移除可能冲突的 ANTHROPIC_AUTH_TOKEN，避免鉴权方式打架。
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# 创建 Anthropic 客户端。base_url 默认指向官方 API；
# 如果设置了 ANTHROPIC_BASE_URL，则使用自定义端点。
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))

# 从环境变量读取模型 ID（例如 claude-sonnet-5 等）。
# 注意：这里用 [] 取值，若 .env 里没配 MODEL_ID 会直接报 KeyError。
MODEL = os.environ["MODEL_ID"]

# 根据操作系统，告诉模型它所在的 shell 环境是什么语法，
# 这样模型才知道该生成 cmd.exe 语法还是 Unix shell 语法。
# os.name == "nt" 表示 Windows。
ENVIRONMENT_PROMPT = (
    "Windows: the bash tool runs through cmd.exe; use cmd.exe syntax, not Unix "
    "Bash or PowerShell syntax"
    if os.name == "nt"
    else "Unix-like: the bash tool runs the system shell"
)

# 系统提示词（system prompt）：给模型设定「人设」和「约束」。
# 它告诉模型：你是运行在某个目录下的编程 Agent，用 bash 来解决问题，直接行动、少废话。
SYSTEM = (
    f"You are a coding agent at {os.getcwd()}. Environment: {ENVIRONMENT_PROMPT}. "
    "Use bash to solve tasks. Act, don't explain."
)

# -- Tool definition: just bash --
# 工具定义（tool definition）：本阶段只提供一个「bash」工具。
# 这个结构遵循 Anthropic API 的 tools 参数格式：
#   name          -> 工具名
#   description   -> 给模型看的工具说明
#   input_schema  -> 工具入参的 JSON Schema（这里只要求一个 command 字符串）
TOOLS = [{
    "name": "bash",
    "description": "Run a shell command.",
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}]


# -- Tool execution --
# 工具执行函数：真正在本地 shell 里运行模型生成的命令，并返回结果字符串。
def run_bash(command: str) -> str:
    # 危险命令黑名单（非常简单的安全防护，s03 会引入更完善的权限控制）。
    # 只要命令里包含这些片段，就直接拒绝执行。
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"

    try:
        # subprocess.run 在子进程中执行命令：
        #   shell=True          -> 通过系统 shell 解释命令（支持管道、重定向等）
        #   cwd=os.getcwd()     -> 在当前工作目录下执行
        #   capture_output=True -> 捕获标准输出和标准错误
        #   text=True           -> 以文本（而非字节）形式返回输出
        #   errors="replace"    -> 遇到无法解码的字符时用替代符，避免崩溃
        #   timeout=120         -> 最多运行 120 秒，防止命令卡死
        r = subprocess.run(command, shell=True, cwd=os.getcwd(),
                           capture_output=True, text=True, errors="replace", timeout=120)
        # 把 stdout 和 stderr 拼在一起，去掉首尾空白。
        out = (r.stdout + r.stderr).strip()
        # 有输出就返回（最多截断到 50000 字符，避免超长内容撑爆上下文），
        # 没有输出就返回一个占位说明。
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        # 命令超时（超过 120 秒）——单独捕获，给出明确提示。
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        # 命令不存在或其它系统级错误——把错误信息返回给模型，让它自行调整。
        return f"Error: {e}"


# -- The core pattern: a while loop that calls tools until the model stops --
# 核心模式：一个 while 循环，反复调用模型，直到模型不再要求调用工具为止。
#
# 参数 messages 是完整的对话历史（由外层调用方维护，函数会就地修改它）。
# 注意：这个函数没有返回值，它通过修改 messages 列表把对话推进到「结束」状态。
def agent_loop(messages: list):
    while True:
        # 【第 1 步】把当前对话历史 + 工具定义一起发给模型，让模型生成回复。
        #   system=SYSTEM   -> 系统提示词
        #   messages        -> 对话历史
        #   tools=TOOLS     -> 可用的工具列表
        #   max_tokens=8000 -> 限制本次回复最多生成的 token 数
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )

        # Append assistant turn
        # 【第 2 步】把模型的回复作为 assistant 角色追加进对话历史。
        # response.content 是一个 content block 列表，里面可能包含：
        #   - text 块（普通文字回答）
        #   - tool_use 块（模型请求调用某个工具）
        messages.append({"role": "assistant", "content": response.content})

        # If the model didn't call a tool, we're done
        # 【第 3 步】从回复中筛选出所有 tool_use 块。
        # 如果模型没有调用任何工具（tool_calls 为空），
        # 说明对话已经「收敛」了，直接结束循环。
        tool_calls = [
            block for block in response.content if block.type == "tool_use"
        ]
        if not tool_calls:
            return

        # Execute each tool call, collect results
        # 【第 4 步】逐个执行模型请求的工具调用，收集执行结果。
        results = []
        for block in tool_calls:
            # 用黄色（ANSI 转义码 \033[33m）打印即将执行的命令，便于观察循环过程。
            print(f"\033[33m$ {block.input['command']}\033[0m")
            # 真正执行命令，得到输出字符串。
            output = run_bash(block.input["command"])
            # 只打印前 200 个字符，避免刷屏。
            print(output[:200])
            # 每条结果都遵循 Anthropic 的 tool_result 格式：
            #   type        -> "tool_result"
            #   tool_use_id -> 对应哪个 tool_use 块（用 block.id 关联）
            #   content     -> 工具的实际输出
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": output,
            })

        # Feed tool results back, loop continues
        # 【第 5 步】把工具结果作为 user 角色消息追加回对话历史，
        # 然后回到 while 顶部（第 1 步），让模型看到结果后继续推理。
        # 这正是上图中「tool_result 回流 -> 循环继续」的那条回边。
        messages.append({"role": "user", "content": results})


# -- Entry point --
# 程序入口：只有直接运行本文件时才会执行下面这段代码。
if __name__ == "__main__":
    print("s01: Agent Loop")
    print("Enter a question, press Enter to send. Type q to quit.\n")

    # history 保存整段对话历史，所有消息都会累积在这里，
    # 传给 agent_loop 后由它就地推进。
    history = []

    # 外层交互循环：反复读取用户输入，直到用户退出。
    while True:
        try:
            # \001/\002 tell Readline the ANSI escapes have zero display width.
            # \001/\002 是 readline 的「不可见定界符」，
            # 用来告诉 readline 夹在中间的 ANSI 转义码不占显示宽度，
            # 这样彩色提示符才不会导致光标位置错乱。
            # \033[36m ... \033[0m 则是「青色文字 + 恢复默认颜色」。
            query = input("\001\033[36m\002s01 >> \001\033[0m\002")
        except (EOFError, KeyboardInterrupt):
            # 用户按 Ctrl+D（EOF）或 Ctrl+C（中断）时退出。
            break

        # 输入 q / exit / 空行，就退出程序。
        if query.strip().lower() in ("q", "exit", ""):
            break

        # 把用户输入作为 user 消息加入对话历史，
        # 然后调用 agent_loop 让模型去干活（可能触发多次工具调用）。
        history.append({"role": "user", "content": query})
        agent_loop(history)

        # Print the model's final text response
        # agent_loop 结束后，打印模型的最终文字回复。
        # history[-1]["content"] 是最后一次 assistant 回复的内容块列表。
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                # 只打印 text 类型的块（跳过 tool_use 块）。
                if getattr(block, "type", None) == "text":
                    print(block.text)
        # 换行，让下一条提示符清爽地开始。
        print()
