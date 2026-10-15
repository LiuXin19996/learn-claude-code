#!/usr/bin/env python3
"""
s10_task_system.py - Task System
（s10_task_system.py - 任务系统）

    .tasks/
      task_a1b2c3d4.json  {status: completed, blockedBy: []}
      task_e5f6a7b8.json  {status: pending, blockedBy: [task_a1b2c3d4]}
      task_11223344.json  {status: pending, blockedBy: [task_e5f6a7b8]}
    （每个任务 = .tasks/ 目录下的一个 json 文件。
      文件名是 task_ + 8 位十六进制随机 ID，内容记录状态和依赖关系。
      把任务落盘，进程重启后状态也不丢。）

    Dependency graph:
    （依赖图：schema 必须先完成 API 才能开始；API 完成后 tests 才能开始。）

    +-----------+      +-----------+      +-----------+
    | schema    | ---> | API       | ---> | tests     |
    | completed |      | pending   |      | pending   |
    +-----------+      +-----------+      +-----------+

    can_start(API) is true because schema is completed.
    （因为 schema 已经完成，所以 API 可以开始了——它的依赖全部满足了。）

    Task lifecycle:
    （任务生命周期：待办 --认领--> 进行中 --完成--> 已完成。）

    pending --claim_task--> in_progress --complete_task--> completed
"""

import glob        # 文件名模式匹配（glob 工具用）
import json        # 序列化：把任务读写为 json 文件
import os          # 环境变量、平台判断
import re          # 正则：校验任务 ID、危险命令匹配
import secrets     # 生成密码学安全的随机数（用于任务 ID，避免碰撞）
import subprocess  # 执行模型生成的 shell 命令
from dataclasses import asdict, dataclass  # 定义 Task 数据类、把对象转成字典
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
# 系统提示词里强调两条任务系统的使用约定：
#   1) 用任务工具来跟踪依赖关系和进度；
#   2) 先把所有任务节点建出来，等 create_task 返回**运行时生成的真实 ID** 之后，
#      再用 update_task 拿这些确切的 ID 去补依赖——因为 ID 是随机生成的，事前无法预知。
SYSTEM = (
    f"You are a coding agent at {WORKDIR}. Environment: {ENVIRONMENT_PROMPT}. "
    "Use task tools to track dependencies and progress. Create all task nodes "
    "first. After create_task returns runtime-generated IDs, use update_task "
    "with those exact IDs to add dependencies."
)


# -- New in s10: persistent task records --
# s10 新增：持久化的任务记录，这是本课的核心。
# 任务状态存在磁盘上（进程重启不丢），任务之间可以声明依赖关系，
# 依赖没全部完成之前不允许开始——这样就能按正确顺序推进多步工作。

TASKS_DIR = WORKDIR / ".tasks"  # 任务文件存放目录
TASK_ID_PATTERN = re.compile(r"^task_[0-9a-f]{8}$")  # 合法 ID 的形状：task_ + 8 位十六进制


@dataclass
class Task:
    # 一个任务的数据结构。@dataclass 会自动生成 __init__、__repr__ 等样板方法。
    id: str               # 任务 ID，形如 task_a1b2c3d4
    subject: str          # 任务标题（一句话说明要做什么）
    description: str      # 详细描述
    status: str           # 状态：pending / in_progress / completed
    owner: str | None     # 认领者；None 表示还没人认领
    blockedBy: list[str]  # 前置依赖：这些任务都完成了，本任务才能开始


class TaskStore:
    # 任务的「仓库」：负责在 Task 对象和磁盘上的 json 文件之间来回转换，
    # 并统一做路径安全校验和内容校验。

    def __init__(self, directory: Path):
        self.directory = directory

    def _root(self, create: bool = False) -> Path:
        # 取得任务目录的绝对路径，并校验它没有跑出工作目录。
        if create:
            self.directory.mkdir(parents=True, exist_ok=True)  # 需要时创建目录
        root = self.directory.resolve()
        # 防目录穿越：任务目录必须落在 workspace 之内。
        if not root.is_relative_to(WORKDIR.resolve()):
            raise ValueError("Task store escapes the workspace")
        return root

    def _path(self, task_id: str, create_root: bool = False) -> Path:
        # 把任务 ID 解析成对应的 json 文件路径，同时做安全校验。
        # 校验 1：ID 必须严格匹配 task_ + 8 位十六进制，
        #   这样 ".."、"a/b" 之类的输入会被直接拒绝，杜绝目录穿越。
        if not isinstance(task_id, str) or not TASK_ID_PATTERN.fullmatch(task_id):
            raise ValueError(f"Invalid task ID: {task_id!r}")
        root = self._root(create=create_root)
        path = (root / f"{task_id}.json").resolve()
        # 校验 2：解析后仍须落在任务目录之内。
        if not path.is_relative_to(root):
            raise ValueError(f"Invalid task ID: {task_id!r}")
        return path

    def exists(self, task_id: str) -> bool:
        # 该任务文件是否已存在。
        return self._path(task_id).is_file()

    def create(self, subject: str, description: str = "") -> Task:
        # 新建任务：生成随机 ID、写成 json 文件、返回 Task 对象。
        subject = subject.strip()
        if not subject:
            raise ValueError("Task subject cannot be empty")

        self._root(create=True)
        # 最多重试 100 次以应对 ID 碰撞（8 位十六进制实际几乎不会撞）。
        for _ in range(100):
            task = Task(
                id=f"task_{secrets.token_hex(4)}",  # 4 字节 = 8 位十六进制随机数
                subject=subject,
                description=description,
                status="pending",  # 新任务默认「待办」
                owner=None,        # 还没有认领者
                blockedBy=[],      # 暂时没有依赖
            )
            try:
                # "x" 模式：文件已存在会抛 FileExistsError（借此发现 ID 碰撞），
                # 同时保证绝不覆盖已有任务文件。
                with self._path(task.id, create_root=True).open(
                    "x", encoding="utf-8"
                ) as handle:
                    json.dump(asdict(task), handle, indent=2)
                return task
            except FileExistsError:
                continue  # ID 撞了，换一个再试
        raise RuntimeError("Could not allocate a unique task ID")

    def _depends_on(self, task_id: str, target_id: str) -> bool:
        """Return whether task_id transitively depends on target_id."""
        # 判断 task_id 是否「直接或间接」依赖 target_id（用于检测循环依赖）。
        # 做法：从 task_id 出发，沿着 blockedBy 一层层往下找（对依赖图做深度优先遍历）。
        pending = [task_id]  # 待探索队列
        visited = set()      # 已访问集合，防止有环时死循环
        while pending:
            current = pending.pop()
            if current == target_id:
                return True  # 找到了：确实（间接）依赖 target_id
            if current in visited:
                continue
            visited.add(current)
            pending.extend(self.load(current).blockedBy)
        return False

    def update_dependencies(self, task_id: str,
                            add_blocked_by: list[str]) -> Task:
        # 给一个任务补充前置依赖。
        if not isinstance(add_blocked_by, list):
            raise ValueError("addBlockedBy must be a list of task IDs")

        task = self.load(task_id)
        # 只有「待办且无人认领」的任务才能改依赖；
        # 已经开始/完成的任务再改依赖会让状态失去意义。
        if task.status != "pending" or task.owner is not None:
            raise ValueError(
                f"Task {task_id} dependencies can only be updated while "
                "pending and unowned"
            )

        # dict.fromkeys 既去重又保持顺序：同一个依赖传入多次也只算一个。
        dependencies = list(dict.fromkeys(add_blocked_by))
        for dependency in dependencies:
            if dependency == task_id:
                raise ValueError("Task cannot depend on itself")  # 不能依赖自己
            if not self.exists(dependency):
                raise ValueError(f"Dependency not found: {dependency}")  # 依赖必须已存在
            # 【防环】若「被依赖者」反过来又依赖本任务，就构成了环，必须拒绝。
            # 只对还没加过的依赖做检查（已存在的说明之前验证过）。
            if dependency not in task.blockedBy and self._depends_on(
                dependency, task_id
            ):
                raise ValueError(
                    f"Dependency cycle detected: {task_id} -> {dependency}"
                )

        # 把新依赖并进去（跳过已经存在的，避免重复）。
        task.blockedBy.extend(
            dependency for dependency in dependencies
            if dependency not in task.blockedBy
        )
        self.save(task)
        return task

    def save(self, task: Task) -> None:
        # 把 Task 对象写回磁盘（覆盖对应的 json 文件）。
        self._path(task.id, create_root=True).write_text(
            json.dumps(asdict(task), indent=2),
            encoding="utf-8",
        )

    def load(self, task_id: str) -> Task:
        # 从磁盘读回一个任务，并做基本校验。
        data = json.loads(self._path(task_id).read_text(encoding="utf-8"))
        task = Task(**data)
        # 防篡改：文件里记录的 id 必须和请求的 id 一致。
        if task.id != task_id:
            raise ValueError(f"Task file ID does not match {task_id}")
        # 状态必须是三个合法值之一。
        if task.status not in ("pending", "in_progress", "completed"):
            raise ValueError(f"Invalid task status: {task.status}")
        return task

    def list(self) -> list[Task]:
        # 列出全部任务（按文件名排序，保证输出稳定）。
        if not self.directory.exists():
            return []
        root = self._root()
        return [self.load(path.stem)
                for path in sorted(root.glob("task_*.json"))]


TASKS = TaskStore(TASKS_DIR)  # 模块级单例：整个进程共用一个任务仓库


def create_task(subject: str, description: str = "") -> Task:
    return TASKS.create(subject, description)


def update_task(task_id: str, addBlockedBy: list[str]) -> Task:
    return TASKS.update_dependencies(task_id, addBlockedBy)


def load_task(task_id: str) -> Task:
    return TASKS.load(task_id)


def list_tasks() -> list[Task]:
    return TASKS.list()


def get_task(task_id: str) -> str:
    # 以格式化后的 JSON 文本返回任务详情。
    return json.dumps(asdict(load_task(task_id)), indent=2)


def incomplete_dependencies(task: Task) -> list[str]:
    # 找出这个任务「还没完成」的前置依赖。
    # 依赖任务读不出来时（被删 / ID 非法）也算作未完成，采取保守策略。
    incomplete = []
    for dependency in task.blockedBy:
        try:
            if load_task(dependency).status != "completed":
                incomplete.append(dependency)
        except (FileNotFoundError, ValueError):
            incomplete.append(dependency)
    return incomplete


def can_start(task_id: str) -> bool:
    # 任务能否开始：前置依赖全部已完成即可。
    return not incomplete_dependencies(load_task(task_id))


def claim_task(task_id: str, owner: str = "agent") -> str:
    # 认领任务：把 pending 改成 in_progress，并记录认领者。
    task = load_task(task_id)
    if task.status != "pending":
        return f"Task {task_id} is {task.status}, cannot claim"  # 只有待办任务能认领
    dependencies = incomplete_dependencies(task)
    if dependencies:
        return f"Blocked by: {dependencies}"  # 依赖没完成，不允许开始
    task.owner = owner
    task.status = "in_progress"
    TASKS.save(task)
    print(f"  [claim] {task.subject} -> in_progress (owner: {owner})")
    return f"Claimed {task.id} ({task.subject})"


def complete_task(task_id: str, owner: str = "agent") -> str:
    # 完成任务：把 in_progress 改成 completed，并报告因此被「解锁」的下游任务。
    task = load_task(task_id)
    if task.status != "in_progress":
        return f"Task {task_id} is {task.status}, cannot complete"
    if task.owner != owner:
        return f"Task {task_id} is owned by {task.owner}, not {owner}"  # 只能完成自己认领的

    # 先记下「完成之前」就已经可开始的任务，
    # 稍后用差集就能算出「因本次完成而被解锁」的任务。
    ready_before = {
        candidate.id
        for candidate in list_tasks()
        if candidate.status == "pending"
        and candidate.blockedBy
        and can_start(candidate.id)
    }
    task.status = "completed"
    TASKS.save(task)
    # 现在满足：待办 + 有依赖 + 之前不能开始 + 现在能开始了 —— 就是刚被解锁的。
    unblocked = [candidate.subject for candidate in list_tasks()
                 if candidate.status == "pending"
                 and candidate.blockedBy
                 and candidate.id not in ready_before
                 and can_start(candidate.id)]
    print(f"  [complete] {task.subject}")
    message = f"Completed {task.id} ({task.subject})"
    if unblocked:
        message += f"\nUnblocked: {', '.join(unblocked)}"
        print(f"  [unblocked] {', '.join(unblocked)}")
    return message


# -- From s04: tool implementations --
# 以下五个是与 s04 相同的基础工具。

def run_bash(command: str) -> str:
    # 执行 shell 命令：固定工作目录、捕获输出、超时 120 秒防卡死。
    try:
        result = subprocess.run(
            command,
            shell=True,           # 交给系统 shell 解析（支持管道、重定向等）
            cwd=WORKDIR,          # 固定在工作目录下执行
            capture_output=True,  # 捕获 stdout/stderr
            text=True, errors="replace",  # 按文本返回；解码失败也不崩
            timeout=120,
        )
        output = (result.stdout + result.stderr).strip()
        return output[:50000] if output else "(no output)"  # 截断，防撑爆上下文
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int | None = None) -> str:
    # 读文件：相对路径拼到工作目录后 resolve（折叠 ../ 等）。
    try:
        lines = (WORKDIR / path).resolve().read_text(encoding="utf-8").splitlines()
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


def run_create_task(subject: str, description: str = "") -> str:
    # 工具入口：创建任务，并把「运行时生成的 ID」回给模型。
    task = create_task(subject, description)
    print(f"  [create] {task.subject}")
    return f"Created {task.id}: {task.subject}"


def run_update_task(task_id: str, addBlockedBy: list[str]) -> str:
    # 工具入口：用 ID 给任务补依赖。
    task = update_task(task_id, addBlockedBy)
    dependencies = ", ".join(task.blockedBy) or "(none)"
    print(f"  [update] {task.subject} blockedBy: {dependencies}")
    return f"Updated {task.id} blockedBy: {dependencies}"


def run_list_tasks() -> str:
    # 工具入口：列出全部任务，带状态标记、认领者和依赖。
    tasks = list_tasks()
    if not tasks:
        return "No tasks. Use create_task to add some."
    lines = []
    for task in tasks:
        # 状态 -> 复选框记号（[ ] 待办 / [>] 进行中 / [x] 已完成）
        marker = {
            "pending": "[ ]",
            "in_progress": "[>]",
            "completed": "[x]",
        }.get(task.status, "[?]")
        dependencies = (
            f" (blockedBy: {', '.join(task.blockedBy)})"
            if task.blockedBy else ""
        )
        owner = f" [{task.owner}]" if task.owner else ""
        lines.append(
            f"{marker} {task.id}: {task.subject} "
            f"[{task.status}]{owner}{dependencies}"
        )
    return "\n".join(lines)


def run_get_task(task_id: str) -> str:
    return get_task(task_id)


def run_claim_task(task_id: str) -> str:
    return claim_task(task_id, owner="agent")


def run_complete_task(task_id: str) -> str:
    return complete_task(task_id, owner="agent")


# 工具定义：前五个是基础工具，后六个是本课新增的任务系统工具
# （create_task / update_task / list_tasks / get_task / claim_task / complete_task）。
# input_schema 里的 pattern 与前面的 TASK_ID_PATTERN 一致，
# 让模型在调用前就被约束成传合法 ID。
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
    {"name": "create_task", "description": "Create a task and return its runtime-generated ID.",
     "input_schema": {"type": "object", "properties": {"subject": {"type": "string"}, "description": {"type": "string"}}, "required": ["subject"], "additionalProperties": False}},
    {"name": "update_task", "description": "Add dependencies using IDs returned by create_task.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "string", "pattern": "^task_[0-9a-f]{8}$"}, "addBlockedBy": {"type": "array", "items": {"type": "string", "pattern": "^task_[0-9a-f]{8}$"}, "minItems": 1}}, "required": ["task_id", "addBlockedBy"], "additionalProperties": False}},
    {"name": "list_tasks", "description": "List tasks with status, owner, and dependencies.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_task", "description": "Get a task by ID.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
    {"name": "claim_task", "description": "Claim a pending task whose dependencies are complete.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
    {"name": "complete_task", "description": "Complete the task claimed by this agent.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
]

# 分发表：工具名 -> 处理函数；execute_tool 靠它找到对应实现。
TOOL_HANDLERS = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
    "create_task": run_create_task,
    "update_task": run_update_task,
    "list_tasks": run_list_tasks,
    "get_task": run_get_task,
    "claim_task": run_claim_task,
    "complete_task": run_complete_task,
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


def context_hook(query: str):
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
register_hook("UserPromptSubmit", context_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", summary_hook)


def execute_tool(block) -> str:
    # 执行单个工具调用：先过 PreToolUse 钩子，再查分发表执行，最后过 PostToolUse。
    blocked = trigger_hooks("PreToolUse", block)
    if blocked:
        return str(blocked)  # 被拦截：把拦截原因当作结果返回

    handler = TOOL_HANDLERS.get(block.name)
    try:
        # **block.input 按关键字展开参数字典；未知工具给出提示。
        output = handler(**block.input) if handler else f"Unknown: {block.name}"
    except Exception as error:
        output = f"Error: {error}"  # 工具抛异常不打断循环，转成文本交给模型

    trigger_hooks("PostToolUse", block, output)
    return str(output)


# -- Agent loop --

def agent_loop(messages: list):
    while True:
        # 调用模型（带上工具定义和系统提示词）。
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
    print("s10: Task System - dependencies and task state")
    print("Enter a question, press Enter to send. Type q to quit.\n")

    history = []  # 对话历史，由 agent_loop 就地增删
    while True:   # 外层交互循环：反复读用户输入，直到用户退出
        try:
            # \001/\002 tell Readline the ANSI escapes have zero display width.
            # （\001/\002 告诉 readline 中间的 ANSI 转义码不占显示宽度，提示符才不会错位）
            query = input("\001\033[36m\002s10 >> \001\033[0m\002")
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
