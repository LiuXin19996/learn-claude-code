#!/usr/bin/env python3
"""
s14: MCP Tools - discover external tools and add them to the agent loop.

Run:  python s14_mcp_plugin/code.py
Need: pip install anthropic python-dotenv + .env with ANTHROPIC_API_KEY

    connect_mcp("docs")
              |
              v
    +------------------+     tools/list     +------------------+
    | Agent Harness    | <----------------- | MCP server       |
    |                  |                    | docs             |
    | built-in tools   |     tools/call     |                  |
    | + MCP tools      | -----------------> | search           |
    +--------+---------+                    | get_version      |
             |                              +------------------+
             v
    +-----------------------------------------------+
    | bash | read | write | edit | glob | connect  |
    | mcp__docs__search | mcp__docs__get_version   |
    +-----------------------------------------------+

（s14：MCP 工具 —— 先连接外部 MCP 服务器，再把它们发现的工具
    动态并入 agent 的工具池。

    与前面几课最大的区别：工具列表不再写死，而是每一轮对话前重新组装。
      1. connect_mcp("docs") 建立连接，拿到服务器的 tools/list 工具清单；
      2. assemble_tool_pool() 把「内置工具 + 所有已连接服务器的工具」
         合成一份，并把外部工具统一改名为 mcp__<服务器>__<工具>；
      3. 模型调用这些外部工具时，execute_tool() 再按名字路由回
         对应 MCPClient 的 handler。

    两条安全约定：
      * 外部工具要不要人工确认，由「宿主配置」MCP_HOST_POLICY 决定，
        不采信服务器自己在 annotations/description 里的自述；
      * 没在策略表里登记的外部工具默认按 confirm 处理——调用前必须人工确认。

    更详细的英文说明、图示和用法见同目录 README.md / README.zh.md。）
"""

import glob          # 按通配符查找文件（glob 工具）
import os            # 读环境变量、判断当前是不是 Windows
import re            # 正则：清洗工具名、识别危险命令
import subprocess    # 执行 shell 命令
from pathlib import Path  # 面向对象的路径操作

try:
    import readline  # 让终端输入支持方向键和历史记录
    readline.parse_and_bind("set bind-tty-special-chars off")
except ImportError:
    # Windows 自带 Python 没有 readline，跳过即可。
    pass

from anthropic import Anthropic  # Anthropic 官方 Python SDK 客户端
from dotenv import load_dotenv   # 从 .env 文件加载环境变量

load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    # 配了自定义端点时移除可能冲突的鉴权变量，避免两种鉴权方式打架。
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()  # 工作目录：所有相对路径和工具执行都以它为基准
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]  # 从环境变量读取模型 ID

# 告诉模型当前宿主的 shell 环境：Windows 走 cmd.exe，其他系统走系统 shell。
ENVIRONMENT_PROMPT = (
    "Windows: the bash tool runs through cmd.exe; use cmd.exe syntax, not Unix "
    "Bash or PowerShell syntax, and prefer dedicated file tools for file operations"
    if os.name == "nt"
    else "Unix-like: the bash tool runs the system shell"
)
# 基础系统提示：说明工作目录、执行环境，以及「用外部工具前先 connect_mcp」的规矩。
BASE_SYSTEM = (
    f"You are a coding agent at {WORKDIR}. Environment: {ENVIRONMENT_PROMPT}. "
    "Use built-in and connected MCP "
    "tools to solve tasks. Call connect_mcp before using a server."
)


# -- From s04: base tools --

def run_bash(command: str) -> str:
    # 在 WORKDIR 下执行 shell 命令，合并 stdout/stderr 返回；
    # 结果最多回给模型 50000 字符，避免一次刷爆上下文。
    try:
        result = subprocess.run(
            command,
            shell=True,              # 交给系统 shell 解释（Windows 上是 cmd.exe）
            cwd=WORKDIR,             # 固定在工作目录里执行
            capture_output=True,     # 捕获输出，不直接打到终端
            text=True, errors="replace",
            timeout=120,             # 超过 120 秒直接杀掉，防止卡死
        )
        output = (result.stdout + result.stderr).strip()
        output = output[:50000] if output else "(no output)"
        if result.returncode:
            # 非零退出码也当作结果返回，让模型自己判断怎么处理。
            return f"Error: command exited with status {result.returncode}\n{output}"
        return output
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except OSError as exc:
        return f"Error: {type(exc).__name__}: {exc}"


def run_read(path: str, limit: int | None = None) -> str:
    # 读取文本文件；limit 只取前 N 行，并在末尾标注还剩多少行没显示。
    try:
        lines = (WORKDIR / path).resolve().read_text(encoding="utf-8").splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as exc:
        # 文件不存在、编码错误等异常都转成文本，交给模型处理。
        return f"Error: {exc}"


def run_write(path: str, content: str) -> str:
    # 写文件：父目录不存在会自动创建；同名文件会被整体覆盖。
    try:
        target = (WORKDIR / path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as exc:
        return f"Error: {exc}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    # 精确替换：old_text 必须在文件里恰好出现 1 次。
    # 0 次说明文本对不上，多次说明可能改错位置，两种情况都拒绝执行。
    try:
        target = (WORKDIR / path).resolve()
        content = target.read_text(encoding="utf-8")
        count = content.count(old_text)
        if count != 1:
            return f"Error: Expected 1 occurrence, found {count}"
        target.write_text(content.replace(old_text, new_text), encoding="utf-8")
        return f"Edited {path}"
    except Exception as exc:
        return f"Error: {exc}"


def run_glob(pattern: str) -> str:
    # 按 glob 模式查找文件，** 表示递归匹配。
    try:
        matches = sorted({
            match
            for match in glob.glob(pattern, root_dir=WORKDIR, recursive=True)
            # 解析后的真实路径必须仍在 WORKDIR 内，防止 ../ 之类的越界匹配。
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR.resolve())
        })
        shown = matches[:200]  # 结果太多时只显示前 200 条，提示模型缩小范围
        if len(matches) > 200:
            shown.append("... (more matches omitted; narrow the pattern)")
        return "\n".join(shown) if shown else "(no matches)"
    except Exception as exc:
        return f"Error: {exc}"


# 工具「声明」：告诉模型每个工具叫什么、干什么、参数长什么样。
# 真正的执行逻辑在 BASE_HANDLERS 里按名字对应。
BASE_TOOLS = [
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
    {"name": "edit_file", "description": "Replace exact text once.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "old_text": {"type": "string"},
                                     "new_text": {"type": "string"}},
                      "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files by glob pattern; ** matches recursively.",
     "input_schema": {"type": "object",
                      "properties": {"pattern": {"type": "string"}},
                      "required": ["pattern"]}},
]

# 工具名 → 执行函数。模型返回 tool_use 时就按这个名字来查表。
BASE_HANDLERS = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
}


# -- New in s14: MCP discovery and dispatch --

class MCPClient:
    """Small in-process stand-in for MCP tools/list and tools/call."""
    # 极简 MCP 客户端：真实项目里这里会走 stdio/HTTP 等协议与独立进程通信，
    # 本课用「进程内注册函数」来模拟，重点放在工具发现、命名和路由上。

    def __init__(self, name: str):
        self.name = name
        # tools：服务器声明的工具清单，相当于 MCP 的 tools/list 结果。
        self.tools: list[dict] = []
        # _handlers：工具名 → 真正执行的函数，相当于 tools/call 的落点。
        self._handlers: dict[str, callable] = {}

    def register(self, tool_defs: list[dict], handlers: dict[str, callable]):
        # 注册一个服务器的全部工具，先做三项校验：
        #   1. 每个工具名必须是非空字符串；
        #   2. 同一服务器内工具名不能重复；
        #   3. 每个声明过的工具都必须有对应 handler，否则调用时无函数可执行。
        names = [tool.get("name") for tool in tool_defs]
        if any(not isinstance(name, str) or not name for name in names):
            raise ValueError("Every MCP tool needs a non-empty name")
        if len(set(names)) != len(names):
            raise ValueError(f"Duplicate MCP tool name on server {self.name!r}")
        missing = [name for name in names if name not in handlers]
        if missing:
            raise ValueError(f"Missing MCP handlers: {', '.join(missing)}")
        self.tools = list(tool_defs)
        self._handlers = dict(handlers)

    def call_tool(self, tool_name: str, args: dict) -> str:
        # 按工具名找到 handler 并执行。
        handler = self._handlers.get(tool_name)
        if not handler:
            # 未知工具不让整个循环崩掉，转成文本错误回给模型。
            return f"MCP error: unknown tool '{tool_name}'"
        try:
            return str(handler(**args))
        except Exception as exc:
            # handler 内部异常同样转成文本，模型能看到原因并自行补救。
            return f"MCP error: {type(exc).__name__}: {exc}"


# 已连接的服务器：name → MCPClient。
# 连接是可累加的——连上 docs 后再连 deploy，两边的工具都会进入工具池。
mcp_clients: dict[str, MCPClient] = {}
# 外部工具的授权策略：完整工具名（mcp__server__tool）→ allow / confirm。
# 每轮组装工具池时重新填充，未登记的工具默认 confirm。
mcp_tool_policies: dict[str, str] = {}
# 模型侧工具名只允许字母、数字、下划线和短横线，其余字符统一换成下划线。
_DISALLOWED_CHARS = re.compile(r"[^a-zA-Z0-9_-]")

# Authorization comes from host configuration, never server descriptions.
# 授权来自宿主配置，而不是服务器在 description/annotations 里的自述：
# 服务器说自己是「只读」不能作为放行依据。未登记的组合一律 confirm。
MCP_HOST_POLICY = {
    ("docs", "search"): "allow",
    ("docs", "get_version"): "allow",
    ("deploy", "status"): "allow",
    ("deploy", "trigger"): "confirm",
}


def normalize_mcp_name(name: str) -> str:
    """Replace characters outside the model tool-name alphabet."""
    # 把服务器名/工具名清洗成模型接受的字符集。
    normalized = _DISALLOWED_CHARS.sub("_", name)
    if not normalized:
        # 清洗后为空就没法拼出合法工具名，直接报错而不是静默跳过。
        raise ValueError("MCP names cannot normalize to an empty string")
    return normalized


def _mock_server_docs() -> MCPClient:
    # 模拟一个文档服务器：search 搜索文档、get_version 取 API 版本。
    # annotations 只是服务器自述，真正的权限仍由 MCP_HOST_POLICY 决定。
    server = MCPClient("docs")
    server.register(
        tool_defs=[
            {
                "name": "search",
                "description": "Search the documentation.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
                "annotations": {"readOnlyHint": True},
            },
            {
                "name": "get_version",
                "description": "Get the documentation API version.",
                "inputSchema": {"type": "object", "properties": {}},
                "annotations": {"readOnlyHint": True},
            },
        ],
        handlers={
            "search": lambda query: f"[docs] Found 3 results for '{query}'",
            "get_version": lambda: "[docs] API v2.1.0",
        },
    )
    return server


def _mock_server_deploy() -> MCPClient:
    # 模拟一个部署服务器：trigger 会触发部署（破坏性），status 只查询状态。
    server = MCPClient("deploy")
    server.register(
        tool_defs=[
            {
                "name": "trigger",
                "description": "Trigger a deployment.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"service": {"type": "string"}},
                    "required": ["service"],
                },
                "annotations": {"destructiveHint": True},
            },
            {
                "name": "status",
                "description": "Check deployment status.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"service": {"type": "string"}},
                    "required": ["service"],
                },
                "annotations": {"readOnlyHint": True},
            },
        ],
        handlers={
            "trigger": lambda service: f"[deploy] Triggered: {service}",
            "status": lambda service: f"[deploy] {service}: running (v1.4.2)",
        },
    )
    return server


# 可连接的服务器工厂表：connect_mcp 只接受这里登记过的名字。
MOCK_SERVERS = {
    "docs": _mock_server_docs,
    "deploy": _mock_server_deploy,
}


def connect_mcp(name: str) -> str:
    # 连接服务器：已连接就提示；未知名字返回可选项。
    # 成功后登记到 mcp_clients，下一轮 assemble_tool_pool() 就能看到它的工具。
    if name in mcp_clients:
        return f"MCP server '{name}' already connected"
    factory = MOCK_SERVERS.get(name)
    if not factory:
        return f"Unknown server '{name}'. Available: {', '.join(MOCK_SERVERS)}"
    server = factory()
    mcp_clients[name] = server
    names = ", ".join(tool["name"] for tool in server.tools)
    print(f"  [mcp] connected: {name} -> {names}")
    return (
        f"Connected to MCP server '{name}'. "
        f"Discovered {len(server.tools)} tools: {names}"
    )


def run_connect_mcp(name: str) -> str:
    # 给 BUILTIN_HANDLERS 用的入口，保持和其它工具 handler 一样的调用形式。
    return connect_mcp(name)


# connect_mcp 本身也是一个工具：模型必须先调它，才能发现外部工具。
# enum 限定只能连 docs / deploy，避免模型臆造服务器名。
CONNECT_TOOL = {
    "name": "connect_mcp",
    "description": "Connect to an MCP server and discover its tools.",
    "input_schema": {
        "type": "object",
        "properties": {"name": {"type": "string", "enum": ["docs", "deploy"]}},
        "required": ["name"],
    },
}

# 宿主自带、永远可用的工具：基础工具 + connect_mcp。
BUILTIN_TOOLS = [*BASE_TOOLS, CONNECT_TOOL]
BUILTIN_HANDLERS = {**BASE_HANDLERS, "connect_mcp": run_connect_mcp}


def assemble_tool_pool() -> tuple[list[dict], dict[str, callable]]:
    """Combine built-in tools with every connected server tool."""
    # 每轮对话前重新组装工具池：内置工具 + 所有已连接 MCP 服务器的工具。
    # 之所以每轮都重新组装，是因为模型可能在本轮中途 connect_mcp，
    # 下一轮它就应该能「看见」并使用新发现的工具。
    global mcp_tool_policies
    tools = list(BUILTIN_TOOLS)
    handlers = dict(BUILTIN_HANDLERS)
    policies: dict[str, str] = {}
    # 记录「工具名 → 来源」，用于发现改名/清洗之后的重名冲突。
    origins = {
        tool["name"]: f"built-in tool {tool['name']!r}"
        for tool in tools
    }

    for server_name, server in mcp_clients.items():
        safe_server = normalize_mcp_name(server_name)
        for tool_def in server.tools:
            raw_name = tool_def["name"]
            safe_tool = normalize_mcp_name(raw_name)
            # 统一加前缀 mcp__<服务器>__<工具>：
            # 既避免与内置工具重名，也让权限策略能看出工具来自哪个服务器。
            prefixed = f"mcp__{safe_server}__{safe_tool}"
            if len(prefixed) > 64:
                # 模型侧工具名有长度上限，超了就是配置问题，直接报错。
                raise ValueError(f"MCP tool name is longer than 64 characters: {prefixed}")
            origin = f"MCP tool {server_name!r}/{raw_name!r}"
            if prefixed in origins:
                # 清洗后撞名会让一个名字路由到两个工具，必须显式拒绝。
                raise ValueError(
                    "MCP tool name collision after normalization: "
                    f"{prefixed!r} maps both {origins[prefixed]} and {origin}"
                )
            schema = tool_def.get("inputSchema", {})
            # MCP 的 inputSchema 必须是顶层 object，才符合模型工具的 input_schema 约定。
            if not isinstance(schema, dict) or schema.get("type", "object") != "object":
                raise ValueError(f"Invalid input schema for {origin}")
            origins[prefixed] = origin
            tools.append({
                "name": prefixed,
                "description": tool_def.get("description", ""),
                "input_schema": schema,
            })
            # 注意 lambda 的默认参数：把当前的 client/tool 绑定进闭包。
            # 如果直接引用循环变量，所有工具最后都会指向最后一个服务器/工具。
            handlers[prefixed] = (
                lambda *, client=server, tool=raw_name, **kwargs:
                client.call_tool(tool, kwargs)
            )
            # 策略按「服务器 + 原始工具名」查宿主配置；没登记的一律 confirm。
            policies[prefixed] = MCP_HOST_POLICY.get(
                (server_name, raw_name), "confirm"
            )

    mcp_tool_policies = policies
    return tools, handlers


def assemble_system_prompt() -> str:
    # 系统提示里追加已连接服务器名单，让模型知道当前有哪些外部能力可用。
    if not mcp_clients:
        return BASE_SYSTEM
    return BASE_SYSTEM + "\n\nConnected MCP servers: " + ", ".join(mcp_clients)


# -- From s04: hooks and permission checks --

# 四个钩子事件：用户提交、工具调用前、工具调用后、一轮结束。
HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}
# 绝对禁止的命令片段：出现即拒绝，不给确认机会。
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]
# 匹配「命令开头或分隔符之后的 rm/del」；用词边界避免把 firmware 之类误判成删除。
DESTRUCTIVE_COMMAND_WORD = re.compile(
    r"(?i)(?:^|[;&|()\n])\s*(?:rm|del)(?=\s|$|[;&|()])"
)
# 其他高危写法：命中后需要人工确认（不是直接拒绝）。
DESTRUCTIVE = ["rm ", "> /etc/", "chmod 777"]


def contains_destructive_command(command: str) -> bool:
    # 判断命令里是否出现了独立的 rm / del 命令。
    return bool(DESTRUCTIVE_COMMAND_WORD.search(command))


def register_hook(event: str, callback):
    # 注册钩子：同一个事件可以挂多个回调。
    HOOKS[event].append(callback)


def trigger_hooks(event: str, *args):
    # 依次执行回调；只要某个回调返回非 None，就把它当成「拦截结果」直接返回。
    # PreToolUse 正是靠这个机制拒绝危险的工具调用。
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None


def permission_hook(block):
    # PreToolUse 钩子：按工具类型分三道路障。
    # 1) 内置 bash：命中 deny list 直接拒绝；疑似破坏性命令弹窗确认。
    if block.name == "bash":
        command = block.input.get("command", "")
        for pattern in DENY_LIST:
            if pattern in command:
                return f"Permission denied by deny list: {pattern}"
        if contains_destructive_command(command) or any(
            keyword in command for keyword in DESTRUCTIVE
        ):
            print(f"\n[permission] {block.name}({block.input})")
            if input("Allow? [y/N] ").strip().lower() not in {"y", "yes"}:
                return "Permission denied by user"

    # 2) 文件工具：路径解析后若跑出 WORKDIR，同样需要用户确认。
    if block.name in {"read_file", "write_file", "edit_file"}:
        raw_path = block.input.get("path", "")
        if not (WORKDIR / raw_path).resolve().is_relative_to(WORKDIR.resolve()):
            print(f"\n[permission] {block.name}({block.input})")
            if input("Allow? [y/N] ").strip().lower() not in {"y", "yes"}:
                return "Permission denied by user"

    # 3) 外部 MCP 工具：按宿主策略处理。
    #    allow 直接放行；confirm（包括未登记的）一律弹窗确认。
    if block.name.startswith("mcp__"):
        policy = mcp_tool_policies.get(block.name, "confirm")
        if policy != "allow":
            print(f"\n[permission] External tool {block.name}({block.input})")
            if input("Allow? [y/N] ").strip().lower() not in {"y", "yes"}:
                return "Permission denied by user"
    return None


def log_hook(block):
    # 每次工具调用打印一行预览，方便观察 agent 正在做什么。
    preview = str(list(block.input.values())[:2])[:60]
    print(f"[hook] {block.name}({preview})")
    return None


def large_output_hook(block, output):
    # 工具返回结果过大时提示一下，防止一大坨输出悄悄灌进上下文。
    if len(str(output)) > 100000:
        print(f"[hook] Large output from {block.name}: {len(str(output))} chars")
    return None


def context_hook(query: str):
    # UserPromptSubmit 钩子：提示当前工作目录。
    print(f"[hook] UserPromptSubmit: working in {WORKDIR}")
    return None


def summary_hook(messages: list):
    # Stop 钩子：统计本次会话一共发生了多少次工具调用。
    tool_count = sum(
        1
        for message in messages
        # 只有 content 是列表时才可能包含 tool_result 块。
        for block in (
            message.get("content")
            if isinstance(message.get("content"), list)
            else []
        )
        if isinstance(block, dict) and block.get("type") == "tool_result"
    )
    print(f"[hook] Stop: session used {tool_count} tool calls")
    return None


# 注册顺序就是执行顺序：permission_hook 先判权限，log_hook 再打印日志。
register_hook("UserPromptSubmit", context_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", summary_hook)


def execute_tool(block, handlers: dict[str, callable]) -> str:
    # 执行一次工具调用，分三步：
    #   1. 先跑 PreToolUse 钩子，返回非空就当作被拒绝；
    #   2. 按工具名查 handler（可能是内置工具，也可能是 MCP 工具）；
    #   3. 执行完再跑 PostToolUse 钩子（只观察，不影响返回结果）。
    blocked = trigger_hooks("PreToolUse", block)
    if blocked:
        return str(blocked)
    handler = handlers.get(block.name)
    if not handler:
        # 工具池是动态的，模型可能调用已不存在（或名字拼错）的工具。
        return f"Unknown tool: {block.name}"
    try:
        output = str(handler(**block.input))
    except Exception as exc:
        # 工具异常转成文本返回给模型，让它自行决定怎么补救。
        output = f"Error: {type(exc).__name__}: {exc}"
    trigger_hooks("PostToolUse", block, output)
    return output


# -- Agent loop with a dynamic tool pool --

def agent_loop(messages: list):
    # Agent 主循环：每一轮都重新组装工具池和系统提示，
    # 这样本轮的 connect_mcp 能让下一轮看到并使用新发现的工具。
    while True:
        try:
            tools, handlers = assemble_tool_pool()
            response = client.messages.create(
                model=MODEL,
                system=assemble_system_prompt(),
                messages=messages,
                tools=tools,
                max_tokens=8000,
            )
        except Exception as exc:
            # 模型调用失败：把错误写成一条 assistant 文本，触发 Stop 钩子后结束本轮。
            messages.append({
                "role": "assistant",
                "content": [{
                    "type": "text",
                    "text": f"[Error] {type(exc).__name__}: {exc}",
                }],
            })
            trigger_hooks("Stop", messages)
            return

        messages.append({"role": "assistant", "content": response.content})
        tool_calls = [
            block for block in response.content if block.type == "tool_use"
        ]
        if not tool_calls:
            # 没有工具调用 = 模型这一轮说完了，触发 Stop 钩子并结束。
            trigger_hooks("Stop", messages)
            return

        results = []
        for block in tool_calls:
            print(f"> {block.name}")
            output = execute_tool(block, handlers)
            print(output[:300])  # 终端只显示前 300 字符，完整输出仍回给模型
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": output,
            })
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    print("s14: MCP tools")
    print("Enter a question, press Enter to send. Type q to quit.\n")
    history = []  # 整个会话的对话历史（Lead 的上下文）

    while True:
        try:
            query = input("s14 >> ")
        except (EOFError, KeyboardInterrupt):
            # Ctrl+D / Ctrl+C 都当作退出。
            break
        if query.strip().lower() in {"q", "exit", ""}:
            break
        trigger_hooks("UserPromptSubmit", query)
        history.append({"role": "user", "content": query})
        agent_loop(history)
        # 打印本轮最后一条 assistant 消息里的文本部分（tool_use 块不打印）。
        for block in history[-1].get("content", []):
            if getattr(block, "type", None) == "text":
                print(block.text)
            elif isinstance(block, dict) and block.get("type") == "text":
                print(block.get("text", ""))
        print()
