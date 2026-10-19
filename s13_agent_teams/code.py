#!/usr/bin/env python3
"""
s13: Agent Teams - persistent teammates with shared tasks and mailboxes.

Run:  python s13_agent_teams/code.py
Need: pip install anthropic python-dotenv + .env with ANTHROPIC_API_KEY

    +------+  spawn(task_id)  +----------+  result  +------+
    | Lead | ---------------> |   WORK   | -------> | IDLE |
    +--+---+                  +----+-----+          +--+---+
       ^                           |                   |
       | team events               | tools             | wait
       |                           v                   v
    +--+-----------+          +----------+        +----------+
    | MessageBus   |          | Task cwd | <----- | Mailbox  |
    +--------------+          +----------+  claim +----------+

    .tasks/       shared task records and dependencies
    .mailboxes/   messages, results, and protocol responses
    .worktrees/   optional task-bound working directories

（s13：Agent 团队 —— 常驻队友 + 共享任务板 + 文件信箱。

    Lead（主 agent）和每个 teammate 都是独立线程、各有独立对话历史，
    它们之间不共享内存里的消息列表，只通过三样东西协作：
      1. .tasks/    任务板：每条任务一个 JSON 文件，带依赖关系 blockedBy；
      2. .mailboxes/ 信箱：每个 agent 一个 .jsonl 文件，发消息就是追加一行；
      3. .worktrees/ 可选的任务专属工作目录（避免两个队友改同一批文件冲突）。

    队友的生命周期如图：WORK（干活）→ 出结果交给 Lead → IDLE（待命）。
    IDLE 时不是干等：会先等消息，等不到就自己去任务板「捡」一个可开工的任务。

    两条重要规矩：
      * 一个队友同一时刻只能认领一个任务（避免上下文被搅混）；
      * 队友不能弹交互式确认框（没有人工盯着它），需要危险操作时
        只能上报 Lead（见 check_permission(prompt_user=False)）。

    更详细的英文说明、图示和用法见同目录 README.md / README.zh.md。）
"""

try:
    import fcntl     # 文件锁（flock）：让「同一台机器上的多个进程」也能互斥操作任务板
except ImportError:
    fcntl = None     # Windows 没有 fcntl：退化成只用进程内的线程锁（与 s15 一致）
import json          # 任务文件与信箱消息的序列化
import os            # 环境变量、进程号、原子替换文件
import queue         # Windows 上没有 select-on-stdin，用队列接收后台线程读到的输入
import random        # 生成协议请求号 req_000123
import re            # 正则：任务 ID / 目录名 / 队友名 / 危险命令匹配
import secrets       # 生成随机任务 ID（task_xxxxxxxx）
import select        # 带超时地探测 stdin 是否可读（CLI 主循环用）
import subprocess    # 执行 shell 命令、调用 git
import sys           # 访问 stdin，供 select 使用
import threading     # 线程：每个队友一条线程；各种锁与条件变量
import time          # 时间戳、等待超时
from contextlib import contextmanager   # 把「加锁/解锁」写成一个 with 上下文管理器
from dataclasses import dataclass, asdict, field  # field(default_factory=...) 用于默认创建时间
from pathlib import Path                # 面向对象的路径操作

try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
except ImportError:
    # Windows 自带 Python 没有 readline，跳过即可。
    pass

from anthropic import Anthropic  # Anthropic 官方 Python SDK 客户端
from dotenv import load_dotenv   # 从 .env 文件加载环境变量

load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    # 配了自定义端点时移除可能冲突的 ANTHROPIC_AUTH_TOKEN，避免两种鉴权打架。
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()  # 工作目录：所有路径校验与 git 操作的基准
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]  # 从环境变量读模型 ID

# -- Task System --
# 任务板：所有任务以「一条任务一个 JSON 文件」的形式存在 .tasks/ 下。
# 用文件而不是内存，是为了让任务状态跨线程、跨进程、甚至跨重启都看得见。

TASKS_DIR = WORKDIR / ".tasks"
TASKS_ROOT = TASKS_DIR.resolve()  # 解析成绝对路径，后面做「不许跳出目录」的校验
TASK_ID_PATTERN = re.compile(r"^task_[0-9a-f]{8}$")  # 任务 ID 形状：task_ + 8 位十六进制
task_lock = threading.RLock()        # 线程内的互斥锁（可重入）
TASK_LOCK_PATH = TASKS_DIR / ".lock"  # 跨进程互斥用的锁文件
# 线程局部存储：记录「当前线程」进入 task_store_lock 的嵌套深度和持有的文件句柄。
# 用 thread-local 而不是普通全局变量，多个线程各自持有自己的句柄才不会互相踩。
_task_store_state = threading.local()

# owner -> {"task_id": str, "cwd": Path}. A teammate gets one assignment at
# a time, and every filesystem tool resolves its cwd through this registry.
# （owner → {"task_id": ..., "cwd": ...}：队友同一时刻只有一个「当前任务」。
#   所有文件/命令工具都通过这张登记表解析自己的工作目录，
#   这样任务绑定了 worktree 时，工具就自动落在那个独立目录里干活。）
teammate_assignments: dict[str, dict[str, object]] = {}
# owner -> 版本号。每次任务交接（认领/完成/异常退出）就 +1，
# 用来让「上一轮任务期间发出的审批」自动作废（见 advance_assignment_version）。
assignment_versions: dict[str, int] = {}


@contextmanager
def task_store_lock():
    """Serialize task mutations across threads and host processes."""
    # 序列化任务变更：既要防同一进程内的多线程打架，也要防同机多进程打架。
    # 双层锁：
    #   * task_lock（RLock）——只管本进程内的线程；
    #   * fcntl.flock(LOCK_EX) 锁 .lock 文件——管跨进程（比如你同时开了两个 s13）。
    #     Windows 没有 fcntl，此时只剩进程内锁（单开本脚本没问题，多开不再互斥）。
    # depth 计数让它可重入：嵌套调用时只在最外层真正加/解文件锁。
    with task_lock:
        depth = getattr(_task_store_state, "depth", 0)
        if depth == 0:
            # 只有最外层才去拿文件锁；句柄存在 thread-local 里，等最外层退出时释放。
            TASKS_DIR.mkdir(parents=True, exist_ok=True)
            handle = TASK_LOCK_PATH.open("a+", encoding="utf-8")
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)  # 独占锁：拿不到就阻塞等待
            _task_store_state.handle = handle
        _task_store_state.depth = depth + 1
        try:
            yield
        finally:
            # finally 保证即使 with 体内抛异常也会解锁、关文件。
            _task_store_state.depth -= 1
            if _task_store_state.depth == 0:
                handle = _task_store_state.handle
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
                del _task_store_state.handle


def advance_assignment_version(owner: str):
    """Invalidate old approvals without clearing an explicit plan requirement."""
    # 推进版本号，让「上一个任务期间」的审批失效，但不会误清掉「明确要求出方案」的状态。
    #
    # 为什么需要？队友可能完成任务 A 后立刻认领任务 B，
    # 此时它手里若还留着针对 A 的审批结果，绝不能拿来当作 B 的通行证。
    # 做法：版本号 +1，老的审批记录里存的是旧版本号，自然对不上。
    # 又因为「要求出方案」这件事会跨任务保留（gates 保持 required），
    # 这里只把 approved 打回 required，而不是清成 not_required。
    with task_lock:
        assignment_versions[owner] = assignment_versions.get(owner, 0) + 1
        # 下面几个全局量定义在文件更靠后的位置，这里用 globals() 按名字取，
        # 避免「函数必须先于变量定义」的顺序问题（取不到就是 None，跳过即可）。
        gates = globals().get("plan_gates")
        request_ids = globals().get("plan_request_ids")
        team = globals().get("team_lock")
        if team is not None:
            team.acquire()  # 按固定顺序（先 task_lock 再 team_lock）加锁，避免死锁
        try:
            if (isinstance(gates, dict) and owner in gates
                    and gates[owner] != "not_required"):
                gates[owner] = "required"  # 旧批准作废，但「必须出方案」保留
            if isinstance(request_ids, dict):
                request_ids.pop(owner, None)  # 丢掉指向旧方案请求的编号
        finally:
            if team is not None:
                team.release()


@dataclass
class Task:
    # 一条共享任务。字段名与 JSON 保持一致，所以用 asdict() 直接落盘。
    id: str
    subject: str
    description: str
    status: str          # pending | in_progress | completed
    owner: str | None
    blockedBy: list[str]  # 依赖的任务 ID 列表：全 completed 才能开工
    worktree: str | None = None  # 绑定到的工作目录名（None 表示就用 WORKDIR）


def _task_path(task_id: str) -> Path:
    # 把任务 ID 换成文件路径，同时做两层安全校验。
    if not isinstance(task_id, str) or not TASK_ID_PATTERN.fullmatch(task_id):
        # 形状不对就拒绝：这样 ".."、"/" 之类的注入根本进不来。
        raise ValueError(f"Invalid task ID: {task_id!r}")
    path = (TASKS_DIR / f"{task_id}.json").resolve()
    if (not TASKS_ROOT.is_relative_to(WORKDIR.resolve())
            or not path.is_relative_to(TASKS_ROOT)):
        # 再确认解析后的真实路径仍落在 .tasks/ 内（防符号链接绕出去）。
        raise ValueError(f"Invalid task ID: {task_id!r}")
    return path


def create_task(subject: str, description: str = "") -> Task:
    # 新建任务，初始状态 pending、无 owner、无依赖。
    subject = subject.strip()
    if not subject:
        raise ValueError("Task subject cannot be empty")
    with task_store_lock():
        for _ in range(100):
            task = Task(
                id=f"task_{secrets.token_hex(4)}",
                subject=subject,
                description=description,
                status="pending",
                owner=None,
                blockedBy=[],
            )
            try:
                # 以 "x" 模式创建：文件已存在就报错。靠这个原子性来避免 ID 撞车。
                with _task_path(task.id).open("x", encoding="utf-8") as handle:
                    json.dump(asdict(task), handle, indent=2)
                return task
            except FileExistsError:
                continue  # 撞了就换个随机 ID 重试
    raise RuntimeError("Could not allocate a unique task ID")


def _task_depends_on(task_id: str, target_id: str) -> bool:
    """Return whether task_id transitively depends on target_id."""
    # task_id 是否（直接或间接）依赖 target_id —— 建边前用它查环。
    pending = [task_id]
    visited = set()
    while pending:
        current = pending.pop()
        if current == target_id:
            return True
        if current in visited:
            continue  # 防重复访问（依赖图可能有菱形结构）
        visited.add(current)
        pending.extend(load_task(current).blockedBy)
    return False


def update_task(task_id: str, addBlockedBy: list[str]) -> Task:
    """Add dependency edges after create_task has returned real task IDs."""
    # 追加依赖边。依赖只能指向 create_task 真实返回过的 ID，因为要在校验里读文件。
    if not isinstance(addBlockedBy, list):
        raise ValueError("addBlockedBy must be a list of task IDs")

    with task_store_lock():
        task = load_task(task_id)
        if task.status != "pending" or task.owner is not None:
            # 已经开工或已被认领的任务，不允许再改依赖：否则可能凭空产生环。
            raise ValueError(
                f"Task {task_id} dependencies can only be updated while "
                "pending and unowned"
            )

        dependencies = list(dict.fromkeys(addBlockedBy))  # 去重且保持原顺序
        for dependency in dependencies:
            if dependency == task_id:
                raise ValueError("Task cannot depend on itself")
            if not _task_path(dependency).is_file():
                raise ValueError(f"Dependency not found: {dependency}")
            if dependency not in task.blockedBy and _task_depends_on(
                dependency, task_id
            ):
                # 关键：若「新依赖」反过来依赖本任务，就会形成环 —— 谁都开不了工。
                raise ValueError(
                    f"Dependency cycle detected: {task_id} -> {dependency}"
                )

        task.blockedBy.extend(
            dependency for dependency in dependencies
            if dependency not in task.blockedBy
        )
        save_task(task)
        return task


def save_task(task: Task):
    # 原子写：先写临时文件，再 os.replace 覆盖目标文件。
    with task_store_lock():
        path = _task_path(task.id)
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )  # 临时名带 pid + 线程号，避免并发写互相覆盖
        try:
            temporary.write_text(
                json.dumps(asdict(task), indent=2), encoding="utf-8"
            )
            os.replace(temporary, path)  # 同目录内的替换是原子操作
        finally:
            temporary.unlink(missing_ok=True)  # 收尾清理（失败时也别留垃圾）


def load_task(task_id: str) -> Task:
    # 读一条任务，并校验内容自洽（防止有人手改坏了文件）。
    with task_lock:
        data = json.loads(_task_path(task_id).read_text(encoding="utf-8"))
        task = Task(**data)
        if task.id != task_id:
            raise ValueError(f"Task file ID does not match {task_id}")
        if task.status not in {"pending", "in_progress", "completed"}:
            raise ValueError(f"Invalid task status: {task.status}")
        return task


def list_tasks() -> list[Task]:
    # 列出全部任务（按文件名排序 ≈ 按创建顺序）。
    with task_lock:
        if not TASKS_DIR.exists():
            return []
        if not TASKS_ROOT.is_relative_to(WORKDIR.resolve()):
            raise ValueError("Tasks directory escapes workspace")
        return [load_task(path.stem)
                for path in sorted(TASKS_DIR.glob("task_*.json"))]


def get_task(task_id: str) -> str:
    """Return full task details as JSON."""
    # 返回任务完整详情，JSON 文本直接交给模型读。
    task = load_task(task_id)
    return json.dumps(asdict(task), indent=2)


def can_start(task_id: str) -> bool:
    """Check if all blockedBy dependencies are completed.
    Missing dependencies are treated as blocked."""
    # 依赖全部完成才能开工；依赖文件不存在也按「阻塞」处理 —— 失败要保守。
    task = load_task(task_id)
    for dep_id in task.blockedBy:
        try:
            dep_path = _task_path(dep_id)
        except ValueError:
            return False  # ID 非法 → 当作阻塞，不放行
        if not dep_path.exists():
            return False
        if load_task(dep_id).status != "completed":
            return False
    return True


def _owner_in_progress(owner: str) -> Task | None:
    # 找出这个 owner 当前正在做的任务（用于「一人同时只能一个任务」的约束）。
    return next((task for task in list_tasks()
                 if task.status == "in_progress" and task.owner == owner), None)


def _incomplete_dependencies(task: Task) -> list[str]:
    # 列出还没做完的依赖，用于给模型一句可读的「被谁挡住了」。
    incomplete = []
    for dep_id in task.blockedBy:
        try:
            dep_path = _task_path(dep_id)
        except ValueError:
            incomplete.append(dep_id)
            continue
        if not dep_path.exists() or load_task(dep_id).status != "completed":
            incomplete.append(dep_id)
    return incomplete


def claim_task(task_id: str, owner: str = "agent") -> str:
    """Atomically claim one task and bind the owner's filesystem cwd."""
    # 原子地认领一个任务，并把 owner 的工具工作目录绑定到这个任务。
    with task_store_lock():
        task = load_task(task_id)
        if task.status != "pending":
            return f"Task {task_id} is {task.status}, cannot claim"
        if task.owner:
            return f"Task {task_id} is already owned by {task.owner}"
        assignment = teammate_assignments.get(owner)
        if assignment:
            # 手里还攥着上一个任务的目录租约：必须先结束那一轮，才能接新活。
            return (f"Owner {owner} must finish the current work turn for "
                    f"{assignment['task_id']} before claiming another task")
        current = _owner_in_progress(owner)
        if current:
            return (f"Owner {owner} must complete {current.id} before "
                    "claiming another task")
        if not can_start(task_id):
            return f"Blocked by: {_incomplete_dependencies(task)}"
        cwd, error = task_worktree_cwd(task)
        if error:
            # 任务绑定的 worktree 坏了（没注册/分支不对/目录不在）就拒绝开工——
            # 宁可不开工，也不要跑到错误的目录里去改文件。
            return f"Cannot claim {task_id}: {error}"
        task.owner = owner
        task.status = "in_progress"
        save_task(task)
        teammate_assignments[owner] = {"task_id": task.id, "cwd": cwd}
        advance_assignment_version(owner)  # 新任务 = 新版本，旧审批自动失效
    print(f"  [claim] {task.subject} -> in_progress (owner: {owner})")
    return f"Claimed {task.id} ({task.subject})"


def complete_task(task_id: str, owner: str = "agent") -> str:
    """Complete an assignment only when the caller owns it."""
    # 只有任务的 owner 本人才能把它标记为完成。
    with task_store_lock():
        task = load_task(task_id)
        if task.status != "in_progress":
            return f"Task {task_id} is {task.status}, cannot complete"
        if task.owner != owner:
            return (f"Task {task_id} is owned by {task.owner}, "
                    f"not {owner}; cannot complete")
        gate = globals().get("plan_gates", {}).get(owner, "not_required")
        if gate in {"required", "pending", "rejected"}:
            # 方案还没被 Lead 批准（或已被否决/要求重做）时不许收工。
            return f"Task {task_id} cannot complete while plan status is {gate}"
        assignment = teammate_assignments.get(owner)
        if not assignment or assignment.get("task_id") != task.id:
            # 登记表里没有对应记录（比如进程重启后残留的状态）：补登记一次，
            # 顺便校验 worktree 仍然可用。
            cwd, error = task_worktree_cwd(task)
            if error:
                return f"Task {task_id} cannot complete: {error}"
            teammate_assignments[owner] = {"task_id": task.id, "cwd": cwd}
        task.status = "completed"
        save_task(task)
        # 顺手看看：完成这一步之后，有哪些原本被阻塞的任务现在可以开工了？
        unblocked = [t.subject for t in list_tasks()
                     if t.status == "pending" and t.blockedBy and can_start(t.id)]
    print(f"  [complete] {task.subject}")
    msg = f"Completed {task.id} ({task.subject})"
    if unblocked:
        # 把「解锁了哪些任务」告诉模型，帮助它安排下一步。
        msg += f"\nUnblocked: {', '.join(unblocked)}"
        print(f"  [unblocked] {', '.join(unblocked)}")
    return msg


# -- Task-bound Worktrees --
# 任务专属工作目录：用 git worktree 开一份独立检出，
# 这样两个队友改同一仓库时不会互相踩，各自在自己的目录里提交。

WORKTREES_DIR = WORKDIR / ".worktrees"
WORKTREES_ROOT = WORKTREES_DIR.resolve()
VALID_WORKTREE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def validate_worktree_name(name: str) -> str | None:
    # 名字限制得比较严：必须以字母/数字开头，只含字母数字点下划线连接符，最长 64。
    if not isinstance(name, str) or not VALID_WORKTREE_NAME.fullmatch(name):
        return ("worktree name must be 1-64 letters, digits, dots, "
                "underscores, or dashes, and start with a letter or digit")
    if name in {".", ".."} or ".." in name:
        return "worktree name cannot contain '..'"  # 再兜一层，杜绝路径穿越
    return None


def _worktree_path(name: str) -> Path:
    # 名字 → 绝对路径，并确认没跑出 .worktrees/ 之外，且不等于该目录本身。
    path = (WORKTREES_DIR / name).resolve()
    if (not WORKTREES_ROOT.is_relative_to(WORKDIR.resolve())
            or not path.is_relative_to(WORKTREES_ROOT)
            or path == WORKTREES_ROOT):
        raise ValueError(f"Worktree path escapes directory: {name!r}")
    return path


def _worktree_branch(name: str) -> str:
    # 每个 worktree 一个专属分支，统一加 wt/ 前缀方便识别。
    return f"wt/{name}"


def _run_git(args: list[str], cwd: Path | None = None) -> tuple[bool, str]:
    """Run Git without shell interpolation and preserve machine output."""
    # 调用 git：参数以列表传入、不经过 shell，因此不存在命令注入；
    # 并且完整保留输出、不做截断——内部解析（如 porcelain 格式）靠它。
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd or WORKDIR,
            capture_output=True, text=True, errors="replace", timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        # git 不存在或超时：转成 (False, 错误信息)，让调用方统一处理。
        return False, f"{type(exc).__name__}: {exc}"
    output = (result.stdout + result.stderr).strip()
    return result.returncode == 0, output or "(no output)"


def run_git(args: list[str], cwd: Path | None = None) -> tuple[bool, str]:
    """Run Git and bound only the text returned to the model."""
    # 对外版本：只在「返回给模型看」的文本上做长度限制，内部逻辑仍用完整输出。
    ok, output = _run_git(args, cwd)
    return ok, output[:5000]  # 截断到 5000 字符，防止一大坨日志灌进上下文


def _registered_worktrees() -> tuple[dict[Path, dict[str, str]], str | None]:
    # 解析 `git worktree list --porcelain`：返回 {绝对路径: 各字段} 以及错误信息。
    ok, output = _run_git(["worktree", "list", "--porcelain"])
    if not ok:
        return {}, f"cannot read Git worktree registry: {output}"
    entries: dict[Path, dict[str, str]] = {}
    current: dict[str, str] = {}
    # porcelain 格式是「空行分隔的记录块」，末尾补一个空字符串以便收尾同一个逻辑。
    for line in output.splitlines() + [""]:
        if not line:
            raw_path = current.get("worktree")
            if raw_path:
                entries[Path(raw_path).resolve()] = current
            current = {}
            continue
        key, _, value = line.partition(" ")  # 形如 "branch refs/heads/wt/foo"
        current[key] = value
    return entries, None


def _registered_worktree(name: str) -> tuple[Path | None, str | None]:
    # 校验某个 worktree：路径合法 + git 已注册 + 目录真实存在 + 分支符合预期。
    try:
        path = _worktree_path(name)
    except ValueError as exc:
        return None, str(exc)
    entries, error = _registered_worktrees()
    if error:
        return None, error
    if path not in entries:
        return None, f"worktree '{name}' is not registered with Git"
    if not path.is_dir():
        return None, f"worktree '{name}' is missing at {path}"
    expected_branch = f"refs/heads/{_worktree_branch(name)}"
    if entries[path].get("branch") != expected_branch:
        # 分支被人手动切走了 → 拒绝使用，否则后续操作会作用在错误的线上。
        return None, (f"worktree '{name}' is not registered on expected "
                      f"branch '{_worktree_branch(name)}'")
    return path, None


def task_worktree_cwd(task: Task) -> tuple[Path, str | None]:
    """Resolve a task cwd, failing closed for broken worktree bindings."""
    # 解析任务的工具工作目录；worktree 绑定有问题时「失败即拒绝」（fail closed）：
    # 返回错误并退回 WORKDIR，也绝不悄悄在错误的目录里干活。
    if not task.worktree:
        return WORKDIR, None
    path, error = _registered_worktree(task.worktree)
    return (path or WORKDIR), error


def assignment_cwd(owner: str) -> Path:
    # 取某个 owner 当前该用的工作目录（含一致性校验）。
    with task_lock:
        assignment = teammate_assignments.get(owner)
        task = _owner_in_progress(owner)
        if task and (not assignment or assignment.get("task_id") != task.id):
            # 登记表缺失或过期：以任务板为准重建登记（自愈）。
            cwd, error = task_worktree_cwd(task)
            if error:
                raise ValueError(error)
            assignment = {"task_id": task.id, "cwd": cwd}
            teammate_assignments[owner] = assignment
        elif not assignment:
            return WORKDIR  # 没在干任何任务 → 就待在主工作目录
        task = load_task(str(assignment["task_id"]))
        if task.status not in {"in_progress", "completed"} or task.owner != owner:
            # 任务已被取消/易主 → 这个目录租约失效，禁止继续用它改文件。
            raise ValueError(f"Assignment for {owner} is no longer active")
        cwd, error = task_worktree_cwd(task)
        if error:
            raise ValueError(error)
        if cwd.resolve() != Path(assignment["cwd"]).resolve():
            # 登记时记的目录和现在算出来的不一致 → 保守拒绝（可能被人动过）。
            raise ValueError(f"Assignment cwd changed for task {task.id}")
        return cwd


def release_completed_assignment(owner: str) -> bool:
    """Release a completed cwd lease only at a model turn boundary."""
    # 只在「模型一轮结束」这个安全时刻释放已完成的目录租约，
    # 保证一轮之内工具用的目录始终一致。
    with task_lock:
        assignment = teammate_assignments.get(owner)
        if not assignment:
            return False
        task = load_task(str(assignment["task_id"]))
        if task.status != "completed" or task.owner != owner:
            return False  # 任务还没完成 → 租约继续留着
        teammate_assignments.pop(owner, None)
        advance_assignment_version(owner)
        if owner in globals().get("plan_gates", {}):
            globals()["plan_gates"][owner] = "not_required"  # 任务结束，方案要求清零
        return True


def release_teammate_assignment(owner: str):
    """Return abandoned teammate work to the task board on thread exit."""
    # 队友线程退出时善后：把它没做完的任务退回任务板，别让任务永远卡在「进行中」。
    with task_lock:
        try:
            task = _owner_in_progress(owner)
            if task:
                task.status = "pending"   # 退回待认领
                task.owner = None
                save_task(task)
        finally:
            # 无论成功失败都要清掉本地状态，防止脏登记卡住下一次认领。
            teammate_assignments.pop(owner, None)
            advance_assignment_version(owner)
            if owner in globals().get("plan_gates", {}):
                globals()["plan_gates"][owner] = "not_required"


def create_worktree(name: str, task_id: str) -> str:
    """Create and bind a dedicated worktree after all inputs validate."""
    # 先做完全部校验，再真正创建并绑定 worktree。
    error = validate_worktree_name(name)
    if error:
        return f"Error: {error}"
    try:
        path = _worktree_path(name)
        task_path = _task_path(task_id)
    except ValueError as exc:
        return f"Error: {exc}"
    branch = _worktree_branch(name)

    with task_lock:
        if not task_path.exists():
            return f"Error: Task {task_id} not found"
        task = load_task(task_id)
        if task.status != "pending" or task.owner is not None:
            # 只允许给「还没开工」的任务建目录，避免换个目录继续改同一件事。
            return f"Error: Task {task_id} must be pending and unowned"
        if task.worktree:
            return f"Error: Task {task_id} already uses worktree '{task.worktree}'"
        if any(t.worktree == name for t in list_tasks() if t.id != task_id):
            return f"Error: Worktree '{name}' is already bound to another task"
        if path.exists():
            return f"Error: Worktree path already exists: {path}"

        ok, root = run_git(["rev-parse", "--show-toplevel"])
        if not ok or Path(root).resolve() != WORKDIR.resolve():
            # worktree 必须建在「仓库根目录」这个前提下，否则相对路径全乱套。
            return "Error: Working directory must be the root of a Git repository"
        ok, branch_check = run_git(["check-ref-format", "--branch", branch])
        if not ok:
            return f"Error: Invalid worktree branch '{branch}': {branch_check}"
        exists, _ = run_git(["show-ref", "--verify", "--quiet",
                             f"refs/heads/{branch}"])
        if exists:
            return f"Error: Branch '{branch}' already exists"  # 不覆盖已有分支
        entries, registry_error = _registered_worktrees()
        if registry_error:
            return f"Error: {registry_error}"
        if path in entries:
            return f"Error: Worktree path is already registered: {path}"

        WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
        ok, result = run_git(["worktree", "add", "-b", branch,
                              str(path), "HEAD"])
        if not ok:
            # 创建失败时可能已经留下「半个成品」（目录/分支/注册记录），
            # 这里只做「检测 + 如实报告」，绝不自动删除任何 git 数据，
            # 把处置权交给人和 Lead。
            entries, registry_error = _registered_worktrees()
            branch_exists, _ = run_git(
                ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"]
            )
            artifacts = []
            if path.exists():
                artifacts.append(f"checkout path '{path}'")
            if registry_error is None and path in entries:
                artifacts.append("registered Git worktree")
            if branch_exists:
                artifacts.append(f"branch '{branch}'")
            if artifacts:
                return (
                    "Partial operation: git worktree add reported an error "
                    f"after leaving {', '.join(artifacts)}. Task {task_id} "
                    "remains unbound and no Git data was deleted. Run "
                    f"`git worktree list`, inspect '{path}' and '{branch}', "
                    "then keep or remove those artifacts manually after "
                    f"preserving any work. Git error: {result}"
                )
            return f"Git error: {result}"

        try:
            task.worktree = name
            save_task(task)  # 目录建好了，最后把绑定关系写进任务
        except Exception as exc:
            # 目录已建但绑定失败：同样如实报告、保留数据，不擅自回滚删除。
            return (f"Partial success: Worktree '{name}' was created at "
                    f"{path} on branch '{branch}', but task binding failed: "
                    f"{exc}. Git data was retained for manual recovery.")

    print(f"  \033[33m[worktree] created: {name} at {path}\033[0m")
    return f"Worktree '{name}' created at {path} for task {task_id}"


def remove_worktree(name: str, discard_changes: bool = False) -> str:
    """Remove a registered checkout while always retaining its branch."""
    # 删除一份工作检出，但**永远保留分支**——分支里可能有还没合并的提交。
    error = validate_worktree_name(name)
    if error:
        return f"Error: {error}"

    with task_lock:
        path, error = _registered_worktree(name)
        if error:
            return f"Error: {error}"
        bound = [task for task in list_tasks() if task.worktree == name]
        if not bound:
            return f"Error: Worktree '{name}' is not bound to a task"
        active = [task for task in bound if task.status != "completed"]
        if active:
            # 任务没完成就不许拆目录，否则队友正干到一半会很懵。
            return (f"Error: Worktree '{name}' is bound to active task "
                    f"{active[0].id}; complete it before removal")
        leased = [owner for owner, assignment in teammate_assignments.items()
                  if Path(assignment["cwd"]).resolve() == path.resolve()]
        if leased:
            # 还有人正拿这个目录当 cwd → 等这一轮结束再删。
            return (f"Error: Worktree '{name}' is still in use by "
                    f"{', '.join(sorted(leased))}; wait for the turn to end")
        ok, status = run_git(
            ["status", "--porcelain", "--ignored"], cwd=path
        )
        if not ok:
            return f"Error: Cannot verify worktree '{name}' status: {status}"
        if status != "(no output)" and not discard_changes:
            # 有未提交改动（含被忽略文件）时默认拒绝，要求显式 discard_changes=True。
            changed = len([line for line in status.splitlines() if line.strip()])
            return (f"Error: Worktree '{name}' has {changed} uncommitted "
                    "change(s); preserve or discard them manually")

        args = ["worktree", "remove"]
        if discard_changes:
            args.append("--force")  # 只有明确要求时才强制
        args.append(str(path))
        ok, result = run_git(args)
        if not ok:
            return f"Git error: {result}"

        try:
            for task in bound:
                task.worktree = None  # 解除绑定，回到默认工作目录
                save_task(task)
        except Exception as exc:
            # 目录删了但解绑失败：如实报告，数据（分支）已保留。
            return (f"Partial success: Worktree '{name}' was removed and "
                    f"branch '{_worktree_branch(name)}' retained, but task "
                    f"unbinding failed: {exc}. Manual recovery is required.")

    print(f"  [worktree] removed: {name}; branch retained")
    return f"Worktree '{name}' removed; branch '{_worktree_branch(name)}' retained"


# -- System Prompt --

# 根据操作系统告诉模型它面对的 shell 语法，避免生成错误的命令风格。
ENVIRONMENT_PROMPT = (
    "Windows: the bash tool runs through cmd.exe; use cmd.exe syntax, not Unix "
    "Bash or PowerShell syntax, and prefer dedicated file tools for file operations"
    if os.name == "nt"
    else "Unix-like: the bash tool runs the system shell"
)
# 系统提示词拆成几段再拼起来，方便单独阅读和调整：
#   identity  —— 角色与运行环境
#   tools     —— 可用工具清单
#   tasks     —— 任务板的用法规矩（先建节点拿真实 ID，再补依赖；只有 Lead 能改依赖）
#   teams     —— 团队协作规矩（先提方案等用户确认，再 spawn；建 worktree 的时机；
#                spawn 后结束本轮而不是轮询队友状态；收尾时要关掉队友）
#   workspace —— 工作目录
PROMPT_SECTIONS = {
    "identity": f"You are a coding agent. Act, don't explain. Environment: {ENVIRONMENT_PROMPT}.",
    "tools": "Available tools: bash, read_file, write_file, edit_file, glob, "
             "create_task, update_task, list_tasks, get_task, claim_task, "
             "complete_task, "
             "spawn_teammate, list_teammates, send_message, request_shutdown, "
             "request_plan, review_plan, create_worktree.",
    "tasks": (
        "Create all task nodes first. Only after create_task returns "
        "runtime-generated IDs, use update_task with those exact IDs to add "
        "dependencies. Only the Lead changes task dependencies."
    ),
    "teams": (
        "When parallel work would help, first propose a small team with clear "
        "responsibilities and wait for the user's confirmation. Do not call "
        "spawn_teammate before the user confirms. After confirmation, delegate "
        "independent work by creating a Task for each parallel change. Pass "
        "task_id to spawn_teammate when assigning ready work, then "
        "create a task-bound worktree only when a separate working directory "
        "would prevent conflicting edits. A teammate must complete its current "
        "Task before claiming another. A worktree changes tool default cwd "
        "only; it is not a sandbox. Worktree removal stays with the host or "
        "user. After spawning a teammate, end the current turn instead of "
        "polling its status; the runtime will deliver team events and wake the "
        "Lead. React to those events, and shut teammates down when "
        "coordination is complete."
    ),
    "workspace": f"Working directory: {WORKDIR}",
}

SYSTEM = "\n\n".join(PROMPT_SECTIONS.values())


# -- Base Tools --
# 基础文件/命令工具。与前面几课不同的是多了 cwd 参数：
# 实际用哪个目录由「当前任务绑定」决定，而不是写死 WORKDIR。

def safe_path(p: str, cwd: Path | None = None) -> Path:
    # 把相对路径解析到给定基准目录下，并确保没有跳出该目录。
    base = (cwd or WORKDIR).resolve()
    path = (base / p).resolve()
    if not path.is_relative_to(base):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str, cwd: Path | None = None) -> str:
    # 在指定目录执行 shell 命令（默认 WORKDIR）。
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=cwd or WORKDIR,
            capture_output=True,
            text=True, errors="replace",
            timeout=120,
        )
        output = (result.stdout + result.stderr).strip()
        output = output[:50000] if output else "(no output)"
        if result.returncode:
            return f"Error: command exited with status {result.returncode}\n{output}"
        return output
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except OSError as exc:
        return f"Error: {type(exc).__name__}: {exc}"


def run_read(path: str, limit: int | None = None,
             cwd: Path | None = None) -> str:
    # 读文件，可选只读前 limit 行。
    try:
        lines = safe_path(path, cwd).read_text(encoding="utf-8").splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str, cwd: Path | None = None) -> str:
    # 写文件（父目录不存在就创建）。
    try:
        fp = safe_path(path, cwd)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str,
             cwd: Path | None = None) -> str:
    # 精确替换：这里要求 old_text 必须**唯一**（出现次数 != 1 就报错），
    # 比 s12 只替换第一处更严格，避免改错地方。
    try:
        target = safe_path(path, cwd)
        content = target.read_text(encoding="utf-8")
        count = content.count(old_text)
        if count != 1:
            return f"Error: Expected 1 occurrence, found {count}"
        target.write_text(content.replace(old_text, new_text), encoding="utf-8")
        return f"Edited {path}"
    except Exception as exc:
        return f"Error: {exc}"


def run_glob(pattern: str, cwd: Path | None = None) -> str:
    # 按 glob 模式找文件，结果以「相对基准目录」的形式展示。
    try:
        base = (cwd or WORKDIR).resolve()
        matches = [
            str(path.relative_to(base))
            for path in sorted(base.glob(pattern))
            if path.resolve().is_relative_to(base)  # 过滤掉指向目录外的符号链接
        ]
        shown = matches[:200]
        if len(matches) > 200:
            shown.append("... (more matches omitted; narrow the pattern)")
        return "\n".join(shown) or "No files found"
    except Exception as exc:
        return f"Error: {exc}"


def _agent_cwd() -> tuple[Path | None, str | None]:
    # Lead 自己的工具也要按「当前认领的任务」决定目录，所以同样走登记表。
    try:
        return assignment_cwd("agent"), None
    except (FileNotFoundError, ValueError) as exc:
        return None, f"Error: Invalid task assignment: {exc}"


def run_agent_bash(command: str) -> str:
    cwd, error = _agent_cwd()
    return error or run_bash(command, cwd)  # 有错就先返回错误，不做无效操作


def run_agent_read(path: str, limit: int | None = None) -> str:
    cwd, error = _agent_cwd()
    return error or run_read(path, limit, cwd)


def run_agent_write(path: str, content: str) -> str:
    cwd, error = _agent_cwd()
    return error or run_write(path, content, cwd)


def run_agent_edit(path: str, old_text: str, new_text: str) -> str:
    cwd, error = _agent_cwd()
    return error or run_edit(path, old_text, new_text, cwd)


def run_agent_glob(pattern: str) -> str:
    cwd, error = _agent_cwd()
    return error or run_glob(pattern, cwd)


# -- Task Tools --
# 给 Lead 用的任务工具包装层：把内部异常转成模型能读的错误文本。

def run_create_task(subject: str, description: str = "") -> str:
    task = create_task(subject, description)
    print(f"  \033[34m[create] {task.subject}\033[0m")  # 蓝色：新建任务
    return f"Created {task.id}: {task.subject}"


def run_update_task(task_id: str, addBlockedBy: list[str]) -> str:
    try:
        task = update_task(task_id, addBlockedBy)
    except ValueError as exc:
        return f"Error: {exc}"
    except FileNotFoundError:
        return f"Error: Task {task_id} not found"
    dependencies = ", ".join(task.blockedBy) or "(none)"
    print(f"  \033[34m[update] {task.subject} blockedBy: {dependencies}\033[0m")
    return f"Updated {task.id} blockedBy: {dependencies}"


def run_list_tasks() -> str:
    # 把任务板渲染成带图标的清单：[ ] 待办 / [~] 进行中 / [x] 已完成。
    tasks = list_tasks()
    if not tasks:
        return "No tasks. Use create_task to add some."
    lines = []
    for t in tasks:
        icon = {"pending": "[ ]", "in_progress": "[~]",
                "completed": "[x]"}.get(t.status, "[?]")
        deps = f" (blockedBy: {', '.join(t.blockedBy)})" if t.blockedBy else ""
        owner = f" [{t.owner}]" if t.owner else ""
        worktree = f" (worktree: {t.worktree})" if t.worktree else ""
        lines.append(f"  {icon} {t.id}: {t.subject} "
                     f"[{t.status}]{owner}{deps}{worktree}")
    return "\n".join(lines)


def run_get_task(task_id: str) -> str:
    try:
        return get_task(task_id)
    except ValueError as exc:
        return f"Error: {exc}"
    except FileNotFoundError:
        return f"Error: Task {task_id} not found"


def run_claim_task(task_id: str) -> str:
    # Lead 认领任务时 owner 固定叫 "agent"。
    try:
        return claim_task(task_id, owner="agent")
    except ValueError as exc:
        return f"Error: {exc}"
    except FileNotFoundError:
        return f"Error: Task {task_id} not found"


def run_complete_task(task_id: str) -> str:
    try:
        return complete_task(task_id, owner="agent")
    except ValueError as exc:
        return f"Error: {exc}"
    except FileNotFoundError:
        return f"Error: Task {task_id} not found"


# -- MessageBus and Team Protocols --
# 信箱（MessageBus）：每个 agent 一个 .jsonl 文件，一行一条 JSON 消息。
# 「读」是破坏性的——读完就把文件删掉，等价于「取走并清空收件箱」。


MAILBOX_DIR = WORKDIR / ".mailboxes"
MAILBOX_ROOT = MAILBOX_DIR.resolve()
VALID_AGENT_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
RESERVED_TEAMMATE_NAMES = {"lead", "agent"}  # 这两个名字归运行时自己用


def is_valid_agent_name(name: str) -> bool:
    return bool(VALID_AGENT_NAME.fullmatch(name))


class MessageBus:
    """Thread-safe file mailboxes with destructive reads."""
    # 线程安全的文件信箱，读取即清空（取走消息并删掉信箱文件）。

    def __init__(self):
        self._lock = threading.RLock()
        # 条件变量：等待方可以「阻塞等到有新消息」，而不是忙等轮询。
        self._changed = threading.Condition(self._lock)

    def _path(self, agent: str) -> Path:
        # 收件人名字 → 信箱文件路径，同样做名字校验 + 目录约束。
        if not is_valid_agent_name(agent):
            raise ValueError(f"Invalid mailbox recipient: {agent!r}")
        path = (MAILBOX_DIR / f"{agent}.jsonl").resolve()
        if not path.is_relative_to(MAILBOX_ROOT):
            raise ValueError(f"Mailbox path escapes directory: {agent!r}")
        return path

    def _read_unlocked(self, agent: str) -> list[dict]:
        # 读走全部消息，然后把文件删掉（破坏性读）。调用方需已持锁。
        inbox = self._path(agent)
        if not inbox.exists():
            return []
        msgs = [json.loads(line) for line in inbox.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        inbox.unlink()
        return msgs

    def send(self, from_agent: str, to_agent: str, content: str,
             msg_type: str = "message", metadata: dict | None = None):
        # 发消息 = 往对方信箱文件末尾追加一行 JSON。
        # 用「追加」而不是「读-改-写」，天然避免写冲突。
        msg = {"from": from_agent, "to": to_agent,
               "content": content, "type": msg_type,
               "ts": time.time(), "metadata": metadata or {}}
        with self._changed:
            MAILBOX_DIR.mkdir(parents=True, exist_ok=True)
            with self._path(to_agent).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(msg, ensure_ascii=True) + "\n")
            self._changed.notify_all()  # 唤醒所有在等待消息的线程
        print(f"  [bus] {from_agent} -> {to_agent}: "
              f"({msg_type}) {content[:50]}")

    def read_inbox(self, agent: str) -> list[dict]:
        with self._lock:
            return self._read_unlocked(agent)

    def peek(self, agent: str) -> bool:
        # 只看「有没有消息」，不取走（CLI 用它判断要不要唤醒 Lead）。
        with self._lock:
            inbox = self._path(agent)
            return inbox.exists() and inbox.stat().st_size > 0

    def wait_for_messages(self, agent: str,
                          timeout: float | None = None) -> list[dict]:
        """Block until the agent has messages or timeout expires."""
        # 阻塞等待新消息，或到超时返回空列表。
        # 用 monotonic 时钟算截止时间，不受系统时间被调整的影响。
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._changed:
            while not self.peek(agent):
                remaining = (None if deadline is None
                             else deadline - time.monotonic())
                if remaining is not None and remaining <= 0:
                    return []  # 超时：返回空，调用方再决定要不要去捡任务
                self._changed.wait(remaining)
            return self._read_unlocked(agent)


BUS = MessageBus()  # 全局唯一的消息总线

# working | waiting_approval | idle | stopping
# 队友状态表：working 干活中 / waiting_approval 等 Lead 批方案 /
#            idle 待命 / stopping 正在关闭。Lead 靠它知道现在有哪些队友。
active_teammates: dict[str, str] = {}
# 方案闸门：not_required 不需要审批 / required 必须交方案 / pending 已交待批
#          / approved 已批准 / rejected 被否决
plan_gates: dict[str, str] = {}
# 每个队友「当前这一份」方案请求的编号（用于忽略过期的审批回复）。
plan_request_ids: dict[str, str] = {}
team_lock = threading.RLock()  # 保护上面三张表


@dataclass
class ProtocolState:
    # 一次协议交互的状态记录（请求-响应模式：关闭请求 / 方案审批）。
    request_id: str
    type: str          # "shutdown" 或 "plan_approval"
    sender: str
    target: str
    status: str        # pending / approved / rejected
    payload: str
    work_version: int | None = None  # 发出请求时的任务版本号
    task_id: str | None = None       # 发出请求时绑定的任务
    created_at: float = field(default_factory=time.time)


pending_requests: dict[str, ProtocolState] = {}


def new_request_id() -> str:
    # 生成一个没被占用的请求编号（req_ + 6 位数字）。
    while True:
        request_id = f"req_{random.randint(0, 999999):06d}"
        if request_id not in pending_requests:
            return request_id


def match_response(response_type: str, request_id: str, approve: bool,
                   from_agent: str, to_agent: str) -> bool:
    """Match one protocol response to one pending request."""
    # 把一条「回复」与一条「待处理请求」对上号：类型、方向、状态都要匹配。
    with team_lock:
        state = pending_requests.get(request_id)
        if not state:
            print(f"  [protocol] unknown request_id: {request_id}")
            return False
        # 请求类型 → 期望的回复类型
        expected = {
            "shutdown": "shutdown_response",
            "plan_approval": "plan_approval_response",
        }[state.type]
        if response_type != expected:
            print(f"  [protocol] expected {expected}, got {response_type}")
            return False
        if from_agent != state.target or to_agent != state.sender:
            # 方向和原来的请求必须相反：原来 A→B 提问，只能 B→A 回答。
            print(f"  [protocol] {request_id} responder mismatch")
            return False
        if state.status != "pending":
            print(f"  [protocol] {request_id} already {state.status}")
            return False  # 一次性：已处理过的请求不接受第二次回复
        state.status = "approved" if approve else "rejected"
    print(f"  [protocol] {request_id} -> {state.status}")
    return True


def consume_lead_inbox() -> list[dict]:
    """Consume Lead events and update protocol state before model delivery."""
    # 取走 Lead 的信箱；在交给模型之前，先用这些回复更新协议状态机。
    msgs = BUS.read_inbox("lead")
    for msg in msgs:
        metadata = msg.get("metadata", {})
        request_id = metadata.get("request_id", "")
        if request_id and msg.get("type", "").endswith("_response"):
            # 形如 plan_approval_response / shutdown_response 的回复先入库。
            match_response(msg["type"], request_id,
                           metadata.get("approve", False),
                           msg.get("from", ""), msg.get("to", ""))
    return msgs


def format_team_events(msgs: list[dict]) -> str:
    # 把一批事件渲染成一段文本，作为 user 消息注入 Lead 的对话。
    lines = []
    for msg in msgs:
        metadata = msg.get("metadata", {})
        request_id = metadata.get("request_id")
        suffix = f" request_id={request_id}" if request_id else ""
        lines.append(
            f"[{msg['type']}{suffix}] {msg['from']}: {msg['content']}"
        )
    return "[Team events]\n" + "\n".join(lines)


def _last_assistant_text(content) -> str:
    # 从模型返回的 content 里取出第一段纯文本（忽略工具调用块）。
    for block in content:
        if getattr(block, "type", None) == "text":
            return block.text.strip()
        if isinstance(block, dict) and block.get("type") == "text":
            return str(block.get("text", "")).strip()
    return ""


def current_work_identity(owner: str) -> tuple[int, str | None]:
    # 取 (任务版本号, 当前任务 ID)。审批时会连同这一对一起核对，
    # 确保「批准的是这个任务的这份方案」。
    with task_lock:
        assignment = teammate_assignments.get(owner)
        task_id = str(assignment["task_id"]) if assignment else None
        return assignment_versions.get(owner, 0), task_id


def _teammate_submit_plan(from_name: str, plan: str) -> str:
    # 队友提交方案：登记一条 pending 请求 + 关上闸门，然后发消息给 Lead。
    with task_lock:
        assignment = teammate_assignments.get(from_name)
        task_id = str(assignment["task_id"]) if assignment else None
        work_version = assignment_versions.get(from_name, 0)
        with team_lock:
            if plan_gates.get(from_name) == "pending":
                return "A plan is already waiting for review."  # 不许重复提交
            request_id = new_request_id()
            pending_requests[request_id] = ProtocolState(
                request_id=request_id,
                type="plan_approval",
                sender=from_name,
                target="lead",
                status="pending",
                payload=plan,
                work_version=work_version,  # 记下「哪个版本的任务」
                task_id=task_id,
            )
            plan_gates[from_name] = "pending"
            plan_request_ids[from_name] = request_id
            active_teammates[from_name] = "waiting_approval"
    BUS.send(from_name, "lead", plan, "plan_approval_request",
             {"request_id": request_id})
    return f"Plan submitted ({request_id}). Wait for Lead's decision."


def _run_teammate_tool(name: str, block, handlers: dict) -> str:
    # 队友执行工具的统一入口（与 Lead 的 execute_tool 分开，规则更严）。
    gate = plan_gates.get(name, "not_required")
    if block.name in {"bash", "write_file", "edit_file"}:
        # 会改动工作区的三类工具：必须先过方案闸门。
        if gate != "approved":
            if gate != "not_required":
                return (f"Blocked: plan status is {gate}. Submit or revise the "
                        "plan and wait for approval before changing the workspace.")
        blocked = check_permission(block, prompt_user=False)  # 队友问不了人
        if blocked:
            return blocked
    handler = handlers.get(block.name)
    if not handler:
        return f"Unknown tool: {block.name}"
    # 权限检查已经在上面按需做过，这里跳过 permission_hook，避免重复检查/重复弹窗。
    trigger_hooks("PreToolUse", block, skip_permission=True)
    try:
        output = str(handler(**block.input))
    except Exception as exc:
        output = f"Error: {type(exc).__name__}: {exc}"
    trigger_hooks("PostToolUse", block, output)
    return output


def apply_plan_response(name: str, msg: dict) -> tuple[bool, str]:
    """Apply only the Lead response for this teammate's current plan."""
    # 只接受「Lead 对本人当前这份方案」的回复，其余一律忽略。
    metadata = msg.get("metadata", {})
    request_id = metadata.get("request_id", "")
    work_version, task_id = current_work_identity(name)
    with team_lock:
        state = pending_requests.get(request_id)
        expected_id = plan_request_ids.get(name)
        # 一长串交叉校验：方向 + 编号是本人当前那份 + 请求类型/双方身份
        # + 任务版本与任务 ID 都没变 + 状态确实是已批/已否 + approve 字段自洽。
        valid = (
            msg.get("from") == "lead"
            and msg.get("to") == name
            and request_id == expected_id
            and state is not None
            and state.type == "plan_approval"
            and state.sender == name
            and state.target == "lead"
            and state.work_version == work_version
            and state.task_id == task_id
            and state.status in {"approved", "rejected"}
            and metadata.get("approve", False)
            == (state.status == "approved")
        )
        if not valid:
            return False, "[Ignored plan response: request mismatch]"
        plan_gates[name] = state.status  # 闸门跟着结论走
        active_teammates[name] = "working"
        plan_request_ids.pop(name, None)
        outcome = state.status
    return True, f"[Plan {outcome}] {msg['content']}"


def apply_shutdown_request(name: str, msg: dict) -> tuple[bool, str]:
    """Accept only a pending shutdown request sent by Lead to this teammate."""
    # 只接受 Lead 发给本人、且仍在 pending 的关闭请求。
    request_id = msg.get("metadata", {}).get("request_id", "")
    with team_lock:
        state = pending_requests.get(request_id)
        valid = (
            msg.get("from") == "lead"
            and msg.get("to") == name
            and state is not None
            and state.type == "shutdown"
            and state.sender == "lead"
            and state.target == name
            and state.status == "pending"
            and active_teammates.get(name) != "stopping"  # 别重复关
        )
        if not valid:
            return False, "[Ignored shutdown request: request mismatch]"
        active_teammates[name] = "stopping"
    return True, request_id


def _teammate_send_message(from_name: str, to: str, content: str) -> str:
    # 队友之间发消息：只允许发给 lead 或仍活着的队友。
    with team_lock:
        if to != "lead" and to not in active_teammates:
            return f"Agent '{to}' is not active"
    BUS.send(from_name, to, content)
    return f"Sent to {to}"


# -- Idle Task Discovery --
# 待命时的「自动捡活」：没消息就去看任务板有没有现成的可开工任务。

IDLE_SCAN_INTERVAL = 2.0  # 等消息最多等 2 秒，超时就去扫任务板


def scan_unclaimed_tasks() -> list[Task]:
    """Return ready tasks whose optional worktree binding is usable."""
    # 返回可以开工的任务：无人认领、依赖已满足，且绑定的 worktree 可用。
    with task_lock:
        ready = []
        for task in list_tasks():
            if (task.status != "pending" or task.owner is not None
                    or not can_start(task.id)):
                continue
            _, error = task_worktree_cwd(task)
            if not error:
                ready.append(task)
        return ready


def claim_next_task(name: str) -> Task | None:
    """Claim the first still-available task, never a second assignment."""
    # 认领第一个还能拿到的任务；已经有任务在手就返回 None，绝不并发接两个。
    with task_lock:
        if teammate_assignments.get(name) or _owner_in_progress(name):
            return None
    for task in scan_unclaimed_tasks():
        # 逐个尝试：并发环境下别人可能刚抢走，所以要看返回是否真是 "Claimed"。
        result = claim_task(task.id, owner=name)
        if result.startswith("Claimed "):
            return load_task(task.id)
    return None


# -- Teammate Runtime --


class TeammateRuntime:
    """One persistent teammate with separate messages and WORK/IDLE phases."""
    # 一个常驻队友：独立对话历史 + WORK/IDLE 两个阶段循环。

    def __init__(self, name: str, role: str, prompt: str,
                 task_id: str | None, require_plan: bool):
        self.name = name
        # 队友的专属系统提示词：说明身份、收尾方式、以及「工作目录不是沙箱」。
        self.system = (
            f"You are '{name}', a {role}. Use tools to complete the assigned "
            "Task, then call complete_task and report a concise result. "
            "If the first user message contains [Assigned task], that Task is "
            "already claimed; do not call claim_task for it again. "
            "When asked for a plan, call submit_plan and wait for approval "
            "before bash or file changes. File and shell tools use the Task's "
            "working directory; that directory is not a sandbox. The runtime "
            "delivers your final text to Lead. Use send_message only for "
            "intermediate coordination, and address the coordinator as 'lead'."
        )
        self.messages = [{"role": "user", "content": prompt}]
        if task_id:
            # 初始任务已经由 spawn 时代为认领了，这里把任务信息拼进首条消息。
            task = load_task(task_id)
            cwd = assignment_cwd(name)
            self.messages[0]["content"] += (
                f"\n\n[Assigned task {task.id}] {task.subject}\n"
                f"{task.description}\nWork directory: {cwd}"
            )
        if require_plan:
            self.messages[0]["content"] += (
                "\n\n[Plan required] Submit a plan and wait for Lead approval "
                "before changing files or using bash."
            )
        # 队友能用的工具集合（比 Lead 少：不能建任务、不能管别人）。
        self.handlers = {
            "bash": self.bash,
            "read_file": self.read,
            "write_file": self.write,
            "edit_file": self.edit,
            "glob": self.glob,
            "send_message": lambda to, content: _teammate_send_message(
                name, to, content),
            "submit_plan": lambda plan: _teammate_submit_plan(name, plan),
            "list_tasks": run_list_tasks,
            "claim_task": self.claim,
            "complete_task": self.complete,
        }

    def current_cwd(self) -> tuple[Path | None, str | None]:
        # 队友的工具目录：没认领任务就不许动工作区。
        if self.name not in teammate_assignments:
            return None, "Error: Claim a Task before using workspace tools."
        try:
            return assignment_cwd(self.name), None
        except (FileNotFoundError, ValueError) as exc:
            return None, f"Error: Invalid task assignment: {exc}"

    def bash(self, command: str) -> str:
        cwd, error = self.current_cwd()
        return error or run_bash(command, cwd=cwd)

    def read(self, path: str, limit: int | None = None) -> str:
        cwd, error = self.current_cwd()
        return error or run_read(path, limit=limit, cwd=cwd)

    def write(self, path: str, content: str) -> str:
        cwd, error = self.current_cwd()
        return error or run_write(path, content, cwd=cwd)

    def edit(self, path: str, old_text: str, new_text: str) -> str:
        cwd, error = self.current_cwd()
        return error or run_edit(path, old_text, new_text, cwd=cwd)

    def glob(self, pattern: str) -> str:
        cwd, error = self.current_cwd()
        return error or run_glob(pattern, cwd=cwd)

    def claim(self, task_id: str) -> str:
        try:
            return claim_task(task_id, owner=self.name)
        except ValueError as exc:
            return f"Error: {exc}"
        except FileNotFoundError:
            return f"Error: Task {task_id} not found"

    def complete(self, task_id: str) -> str:
        try:
            return complete_task(task_id, owner=self.name)
        except ValueError as exc:
            return f"Error: {exc}"
        except FileNotFoundError:
            return f"Error: Task {task_id} not found"

    def handle_inbox(self, inbox: list[dict]) -> bool:
        """Append work messages and return True for a valid shutdown."""
        # 处理收件箱：把消息追加进对话；若收到有效关闭请求则返回 True。
        work_messages = []
        for msg in inbox:
            msg_type = msg.get("type", "message")
            if msg_type == "shutdown_request":
                accepted, notice = apply_shutdown_request(self.name, msg)
                if not accepted:
                    work_messages.append(notice)  # 身份不符的关闭请求当普通消息看
                    continue
                # 先回执再退出，让 Lead 明确知道关闭已完成。
                BUS.send(self.name, "lead", "Shutdown acknowledged.",
                         "shutdown_response",
                         {"request_id": notice, "approve": True})
                return True
            if msg_type == "plan_approval_response":
                _, notice = apply_plan_response(self.name, msg)
                work_messages.append(notice)  # 无论采纳与否都让模型看到结论
                continue
            if msg_type == "plan_request":
                work_messages.append(f"[Plan required] {msg['content']}")
                continue
            work_messages.append(
                f"[Message from {msg['from']}] {msg['content']}"
            )
        if work_messages:
            # 多轮之间用「一条 user 消息」批量注入，保持 messages 结构合法。
            self.messages.append({"role": "user",
                                  "content": "\n".join(work_messages)})
        return False

    def work(self) -> str:
        """Run one model turn. Return continue, idle, or stop."""
        # 跑一轮模型对话，返回下个状态：continue 继续 / idle 待命 / stop 结束。
        if self.handle_inbox(BUS.read_inbox(self.name)):
            return "stop"  # 收到关闭请求：本轮就此收尾
        with team_lock:
            active_teammates[self.name] = "working"
        try:
            response = client.messages.create(
                model=MODEL,
                system=self.system,
                messages=self.messages,
                tools=TEAMMATE_TOOLS,
                max_tokens=8000,
            )
        except Exception as exc:
            # 模型调用失败：把错误报给 Lead 然后停止，别让线程默默卡住。
            BUS.send(self.name, "lead",
                     f"{type(exc).__name__}: {exc}", "error")
            return "stop"

        self.messages.append({"role": "assistant",
                              "content": response.content})
        tool_calls = [
            block for block in response.content if block.type == "tool_use"
        ]
        if tool_calls:
            results = []
            for block in tool_calls:
                output = _run_teammate_tool(
                    self.name, block, self.handlers
                )
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": output})
            self.messages.append({"role": "user", "content": results})
            return "continue"  # 还要继续用工具

        # 没有工具调用 = 这一轮说完了。把结论交给 Lead。
        summary = _last_assistant_text(response.content)
        gate = plan_gates.get(self.name, "not_required")
        if gate != "pending" and summary:
            BUS.send(self.name, "lead", summary, "result")  # 最终结论作为 result 事件
        if gate == "pending":
            # 方案还没批完：停在等审批状态，等 Lead 回复再继续。
            with team_lock:
                active_teammates[self.name] = "waiting_approval"
        else:
            # 正常收工：释放目录租约、进入待命，并通知 Lead 自己空闲了。
            release_completed_assignment(self.name)
            with team_lock:
                active_teammates[self.name] = "idle"
            BUS.send(self.name, "lead", "Waiting for more work.",
                     "idle_notification")
        return "idle"

    def wait_for_work(self) -> bool:
        """Wait for a message or atomically claim the next ready Task."""
        # 待命：先等消息（最长 IDLE_SCAN_INTERVAL 秒），没消息就去任务板原子地捡一个可开工任务。
        while True:
            inbox = BUS.wait_for_messages(self.name, IDLE_SCAN_INTERVAL)
            if inbox:
                before = len(self.messages)
                if self.handle_inbox(inbox):
                    return False  # 收到关闭请求
                if len(self.messages) > before:
                    return True  # 有新消息进了对话 → 回去干活
                continue  # 全是无需处理的协议消息 → 继续等

            task = claim_next_task(self.name)
            if not task:
                continue  # 没活可捡 → 接着等
            cwd = assignment_cwd(self.name)
            self.messages.append({
                "role": "user",
                "content": (
                    f"[Auto-claimed task {task.id}] {task.subject}\n"
                    f"{task.description}\nWork directory: {cwd}"
                ),
            })
            print(f"  [idle] {self.name} claimed {task.id}: {task.subject}")
            return True

    def run(self):
        # 队友线程主循环：continue 就一直干；idle 就等活；stop 就退出。
        try:
            state = "continue"
            while state != "stop":
                if state == "idle" and not self.wait_for_work():
                    break  # 等到关闭请求
                state = self.work()
        except Exception as exc:
            # 兜底：线程里任何未预期异常都要报给 Lead，不能无声死掉。
            try:
                BUS.send(self.name, "lead",
                         f"{type(exc).__name__}: {exc}", "error")
            except Exception:
                pass
        finally:
            # 收尾：把没做完的任务退回任务板，并清掉本人在各种表里的痕迹。
            try:
                release_teammate_assignment(self.name)
            except Exception as exc:
                try:
                    BUS.send(
                        self.name, "lead",
                        f"Assignment cleanup failed: {type(exc).__name__}: {exc}",
                        "error",
                    )
                except Exception:
                    pass
            with team_lock:
                active_teammates.pop(self.name, None)
                plan_gates.pop(self.name, None)
                plan_request_ids.pop(self.name, None)
                teammate_threads.pop(self.name, None)
            print(f"  [teammate] {self.name} finished")


# 已启动的队友线程（用于观测；也是「队友是否还在」的一个参照）。
teammate_threads: dict[str, threading.Thread] = {}


def spawn_teammate_thread(name: str, role: str, prompt: str,
                          task_id: str | None = None,
                          require_plan: bool = False) -> str:
    """Claim an initial Task, then start one persistent teammate."""
    # 先替队友认领初始任务，成功后再启动线程。
    if not is_valid_agent_name(name):
        return ("Invalid teammate name: use 1-64 letters, digits, "
                "underscores, or dashes")
    if name.lower() in RESERVED_TEAMMATE_NAMES:
        return f"Invalid teammate name: '{name}' is reserved by the runtime"
    with team_lock:
        if any(existing.casefold() == name.casefold()
               for existing in active_teammates):
            # 大小写不敏感地查重，避免出现 "Alice" 和 "alice" 两个队友。
            return f"Teammate '{name}' already exists"
        # 先占位再启动：这样并发的两次 spawn 不会同时通过查重。
        active_teammates[name] = "working"
        plan_gates[name] = "required" if require_plan else "not_required"
        assignment_versions[name] = 0

    if task_id:
        try:
            claimed = claim_task(task_id, owner=name)
        except (FileNotFoundError, ValueError) as exc:
            claimed = f"Error: {exc}"
        if not claimed.startswith("Claimed "):
            # 认领失败 → 回滚刚才的占位，不做「起了个没活的队友」这种事。
            with team_lock:
                active_teammates.pop(name, None)
                plan_gates.pop(name, None)
                assignment_versions.pop(name, None)
            return f"Cannot spawn teammate '{name}': {claimed}"

    runtime = TeammateRuntime(name, role, prompt, task_id, require_plan)
    thread = threading.Thread(target=runtime.run, daemon=True)  # daemon：主程序退出即结束
    with team_lock:
        teammate_threads[name] = thread
    thread.start()
    print(f"  [teammate] {name} spawned as {role}")
    assigned = f" for {task_id}" if task_id else " without an initial Task"
    return (
        f"Teammate '{name}' spawned as {role}{assigned}. "
        "End this turn; the runtime will deliver its events."
        # 提醒 Lead：本轮就到此为止，别去轮询队友——事件会通过信箱送上来。
    )


# -- Lead Team Tools --

def run_spawn_teammate(name: str, role: str, prompt: str,
                       task_id: str | None = None,
                       require_plan: bool = False) -> str:
    return spawn_teammate_thread(name, role, prompt, task_id, require_plan)


def run_list_teammates() -> str:
    # 列出队友及其当前状态。
    with team_lock:
        if not active_teammates:
            return "No active teammates."
        return "\n".join(
            f"{name}: {status}"
            for name, status in sorted(active_teammates.items())
        )


def run_send_message(to: str, content: str) -> str:
    # Lead 给某个队友发消息。
    if to not in active_teammates:
        return f"Teammate '{to}' is not active"
    BUS.send("lead", to, content)
    return f"Sent to {to}"


def run_request_shutdown(teammate: str) -> str:
    # 请求队友关闭：登记一条 pending 的 shutdown 请求，再发关闭消息。
    if teammate not in active_teammates:
        return f"Teammate '{teammate}' is not active"
    with team_lock:
        request_id = new_request_id()
        pending_requests[request_id] = ProtocolState(
            request_id=request_id,
            type="shutdown",
            sender="lead",
            target=teammate,
            status="pending",
            payload="",
        )
    BUS.send("lead", teammate, "Finish the current step and shut down.",
             "shutdown_request", {"request_id": request_id})
    return f"Shutdown requested from {teammate} ({request_id})"


def run_request_plan(teammate: str, task: str) -> str:
    # 要求队友先交方案：把闸门设为 required 并发一条 plan_request。
    if teammate not in active_teammates:
        return f"Teammate '{teammate}' is not active"
    with team_lock:
        plan_gates[teammate] = "required"
    BUS.send("lead", teammate, task, "plan_request")
    return f"Plan requested from {teammate}"


def run_review_plan(request_id: str, approve: bool,
                    feedback: str = "") -> str:
    # Lead 审批方案：核准/否决后把结论发回队友。
    state = pending_requests.get(request_id)
    if not state:
        return f"Request {request_id} not found"
    work_version, task_id = current_work_identity(state.sender)
    with team_lock:
        # 锁内重新取一次 state：上面取过之后可能已被别的线程处理掉。
        state = pending_requests.get(request_id)
        if not state:
            return f"Request {request_id} not found"
        if state.type != "plan_approval":
            return f"Request {request_id} is not a plan"
        if state.status != "pending":
            return f"Request {request_id} already {state.status}"
        if (state.work_version != work_version or state.task_id != task_id):
            # 队友已经换任务了 → 这份旧方案不再适用，别批。
            return f"Request {request_id} belongs to an earlier assignment"
        if plan_request_ids.get(state.sender) != request_id:
            return f"Request {request_id} is not the current plan"
        state.status = "approved" if approve else "rejected"
    content = feedback or ("Plan approved." if approve
                           else "Revise the plan and submit it again.")
    BUS.send("lead", state.sender, content, "plan_approval_response",
             {"request_id": request_id, "approve": approve})
    return f"Plan {state.status} ({request_id})"


def run_create_worktree(name: str, task_id: str) -> str:
    return create_worktree(name, task_id)


# -- Tool Definitions --
# 工具定义分成几组，再按「给谁用」拼成不同清单。

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

TASK_TOOLS = [
    # 用 JSON Schema 里的 pattern 限制任务 ID 形状，让模型从一开始就写对 ID。
    {"name": "create_task",
     "description": "Create a task and return its runtime-generated ID.",
     "input_schema": {"type": "object",
                      "properties": {
                          "subject": {"type": "string"},
                          "description": {"type": "string"}},
                      "required": ["subject"],
                      "additionalProperties": False}},
    {"name": "update_task",
     "description": "Add dependencies using IDs returned by create_task.",
     "input_schema": {"type": "object",
                      "properties": {
                          "task_id": {"type": "string",
                                      "pattern": "^task_[0-9a-f]{8}$"},
                          "addBlockedBy": {
                              "type": "array",
                              "items": {"type": "string",
                                        "pattern": "^task_[0-9a-f]{8}$"},
                              "minItems": 1}},
                      "required": ["task_id", "addBlockedBy"],
                      "additionalProperties": False}},
    {"name": "list_tasks", "description": "List shared tasks.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_task", "description": "Get one task by ID.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
    {"name": "claim_task", "description": "Claim a ready task.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
    {"name": "complete_task", "description": "Complete an owned task.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
]

TEAMMATE_TOOLS = [
    *BASE_TOOLS,  # 队友也要能读写代码、跑命令
    {"name": "send_message",
     "description": "Send an intermediate message to 'lead' or an active teammate.",
     "input_schema": {"type": "object",
                      "properties": {"to": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["to", "content"]}},
    {"name": "submit_plan",
     "description": "Submit a work plan for Lead approval.",
     "input_schema": {"type": "object",
                      "properties": {"plan": {"type": "string"}},
                      "required": ["plan"]}},
    # 从 TASK_TOOLS 里挑出三个给队友用（队友不能新建任务、不能改依赖）。
    next(tool for tool in TASK_TOOLS if tool["name"] == "list_tasks"),
    next(tool for tool in TASK_TOOLS if tool["name"] == "claim_task"),
    next(tool for tool in TASK_TOOLS if tool["name"] == "complete_task"),
]

TEAM_TOOLS = [
    # Lead 独有的团队管理工具。
    {"name": "spawn_teammate",
     "description": "Spawn a persistent teammate.",
     "input_schema": {"type": "object",
                      "properties": {
                          "name": {"type": "string",
                                   "pattern": "^[A-Za-z0-9_-]{1,64}$"},
                          "role": {"type": "string"},
                          "prompt": {"type": "string"},
                          "task_id": {"type": "string",
                                      "pattern": "^task_[0-9a-f]{8}$"},
                          "require_plan": {"type": "boolean"}},
                      "required": ["name", "role", "prompt"]}},
    {"name": "list_teammates", "description": "List active teammates.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "send_message", "description": "Message a teammate.",
     "input_schema": {"type": "object",
                      "properties": {"to": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["to", "content"]}},
    {"name": "request_shutdown",
     "description": "Ask a teammate to shut down.",
     "input_schema": {"type": "object",
                      "properties": {"teammate": {"type": "string"}},
                      "required": ["teammate"]}},
    {"name": "request_plan",
     "description": "Require a teammate plan before workspace changes.",
     "input_schema": {"type": "object",
                      "properties": {"teammate": {"type": "string"},
                                     "task": {"type": "string"}},
                      "required": ["teammate", "task"]}},
    {"name": "review_plan", "description": "Approve or reject a plan.",
     "input_schema": {"type": "object",
                      "properties": {
                          "request_id": {"type": "string"},
                          "approve": {"type": "boolean"},
                          "feedback": {"type": "string"}},
                      "required": ["request_id", "approve"]}},
    {"name": "create_worktree",
     "description": "Create and bind a task worktree.",
     "input_schema": {
         "type": "object",
         "properties": {
             # 负向先行断言 (?!.*\.\.)：从 schema 层就挡住含 ".." 的名字
             "name": {"type": "string",
                      "pattern": "^(?!.*\\.\\.)[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
                      "maxLength": 64},
             "task_id": {"type": "string"}},
         "required": ["name", "task_id"],
         "additionalProperties": False}},
]

# Lead 能用的全部工具 = 基础 + 任务 + 团队。
TOOLS = [*BASE_TOOLS, *TASK_TOOLS, *TEAM_TOOLS]

TOOL_HANDLERS = {
    "bash": run_agent_bash,
    "read_file": run_agent_read,
    "write_file": run_agent_write,
    "edit_file": run_agent_edit,
    "glob": run_agent_glob,
    "create_task": run_create_task,
    "update_task": run_update_task,
    "list_tasks": run_list_tasks,
    "get_task": run_get_task,
    "claim_task": run_claim_task,
    "complete_task": run_complete_task,
    "spawn_teammate": run_spawn_teammate,
    "list_teammates": run_list_teammates,
    "send_message": run_send_message,
    "request_shutdown": run_request_shutdown,
    "request_plan": run_request_plan,
    "review_plan": run_review_plan,
    "create_worktree": run_create_worktree,
}


# -- Hooks and Permission Checks --

HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]  # 硬黑名单
DESTRUCTIVE_COMMAND_WORD = re.compile(
    r"(?i)(?:^|[;&|()\n])\s*(?:rm|del)(?=\s|$|[;&|()])"
)
DESTRUCTIVE = ["rm ", "> /etc/", "chmod 777"]  # 需人工确认的关键词


def contains_destructive_command(command: str) -> bool:
    return bool(DESTRUCTIVE_COMMAND_WORD.search(command))


def register_hook(event: str, callback):
    HOOKS[event].append(callback)


def trigger_hooks(event: str, *args, skip_permission: bool = False):
    # 多了一个 skip_permission：队友那边已经自己做过权限检查，这里跳过 permission_hook。
    for callback in HOOKS[event]:
        if skip_permission and callback is permission_hook:
            continue
        result = callback(*args)
        if result is not None:
            return result  # 谁先返回内容谁就「拦截」
    return None


def check_permission(block, prompt_user: bool = True) -> str | None:
    # prompt_user=True：可以弹交互式确认框（Lead 在跟人对话）。
    # prompt_user=False：队友场景，不能弹框，只能返回「请 Lead 来跑」。
    if block.name == "bash":
        command = block.input.get("command", "")
        for pattern in DENY_LIST:
            if pattern in command:
                return f"Permission denied by deny list: {pattern}"
        if contains_destructive_command(command) or any(
            keyword in command for keyword in DESTRUCTIVE
        ):
            if not prompt_user:
                return "Permission required: ask Lead to run this command."
            print(f"\n[permission] {block.name}({block.input})")
            if cli_input("Allow? [y/N] ").strip().lower() not in {"y", "yes"}:
                return "Permission denied by user"

    if block.name in {"read_file", "write_file", "edit_file"}:
        raw_path = block.input.get("path", "")
        if not (WORKDIR / raw_path).resolve().is_relative_to(WORKDIR.resolve()):
            # 访问工作区外的路径同样需要授权。
            if not prompt_user:
                return "Permission required: path is outside the workspace."
            print(f"\n[permission] {block.name}({block.input})")
            if cli_input("Allow? [y/N] ").strip().lower() not in {"y", "yes"}:
                return "Permission denied by user"
    return None


def permission_hook(block):
    return check_permission(block, prompt_user=True)  # 默认：Lead 场景，可以问人


def log_hook(block):
    preview = str(list(block.input.values())[:2])[:60]
    print(f"[hook] {block.name}({preview})")
    return None


def large_output_hook(block, output):
    if len(str(output)) > 100000:
        print(f"[hook] Large output from {block.name}: {len(str(output))} chars")
    return None


def context_hook(query: str):
    print(f"[hook] UserPromptSubmit: working in {WORKDIR}")
    return None


def summary_hook(messages: list):
    # 回合结束时统计用掉的工具调用次数。
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
    print(f"[hook] Stop: session used {tool_count} tool calls")
    return None


register_hook("UserPromptSubmit", context_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", summary_hook)


def execute_tool(block) -> str:
    # 执行工具：先过 PreToolUse 钩子（可拦截），再找处理函数，最后发 PostToolUse。
    blocked = trigger_hooks("PreToolUse", block)
    if blocked:
        return str(blocked)
    handler = TOOL_HANDLERS.get(block.name)
    if not handler:
        return f"Unknown tool: {block.name}"
    try:
        output = str(handler(**block.input))
    except Exception as exc:
        output = f"Error: {type(exc).__name__}: {exc}"
    trigger_hooks("PostToolUse", block, output)
    return output


# -- Agent Loop --

def agent_loop(messages: list):
    # Lead 的 agent 循环：与前面几课相同，只是收尾时多了一步「释放目录租约」。
    while True:
        try:
            response = client.messages.create(
                model=MODEL,
                system=SYSTEM,
                messages=messages,
                tools=TOOLS,
                max_tokens=8000,
            )
        except Exception as exc:
            # 调用失败也要收尾：把错误写进对话、释放租约、跑 Stop 钩子后返回。
            messages.append({
                "role": "assistant",
                "content": [{
                    "type": "text",
                    "text": f"[Error] {type(exc).__name__}: {exc}",
                }],
            })
            release_completed_assignment("agent")
            trigger_hooks("Stop", messages)
            return

        messages.append({"role": "assistant", "content": response.content})
        tool_calls = [
            block for block in response.content if block.type == "tool_use"
        ]
        if not tool_calls:
            release_completed_assignment("agent")  # 本轮结束，释放 cwd 租约
            trigger_hooks("Stop", messages)
            return

        results = []
        for block in tool_calls:
            print(f"> {block.name}")
            output = execute_tool(block)
            print(output[:300])  # 终端上只看前 300 字符，完整内容仍回给模型
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": output,
            })
        messages.append({"role": "user", "content": results})


def print_last_assistant_message(history: list):
    # 只打印本次回合最后一条 assistant 消息里的文本部分。
    if not history:
        return
    for block in history[-1].get("content", []):
        if getattr(block, "type", None) == "text":
            print(block.text)
        elif isinstance(block, dict) and block.get("type") == "text":
            print(block.get("text", ""))


_stdin_queue: "queue.Queue[str | None] | None" = None


def _start_stdin_reader():
    """Windows 上 select 只能等 socket，等不了终端输入。

    所以退化为：一条后台线程阻塞读 stdin，读到一行就放进队列，
    主循环用 queue.get(timeout=...) 拿到同样的「带超时等待」效果。
    """
    global _stdin_queue
    if _stdin_queue is not None:
        return
    _stdin_queue = queue.Queue()

    def reader():
        for line in sys.stdin:
            _stdin_queue.put(line)
        _stdin_queue.put(None)  # EOF（Ctrl+Z 回车 / 管道结束）

    threading.Thread(target=reader, daemon=True).start()


def cli_input(prompt: str = "") -> str:
    """Read one line from the user through the same stdin path as the CLI loop."""
    # Windows 上 stdin 已被后台读取线程接管，不能再用 input()（两边抢同一行会卡死）。
    if os.name != "nt":
        return input(prompt)
    if prompt:
        print(prompt, end="", flush=True)
    _start_stdin_reader()
    line = _stdin_queue.get()
    return "" if line is None else line.rstrip("\n")


def wait_for_cli_event() -> tuple[str, str | None]:
    # CLI 事件循环的核心：同时盯着「键盘输入」和「Lead 的信箱」，谁先来就处理谁。
    # 有了它，队友发来的事件能像用户输入一样把 Lead 唤醒。
    prompt_visible = False
    while True:
        if BUS.peek("lead"):
            # 有团队事件：如果提示符已经打印出来，先换行免得挤在一起。
            if prompt_visible:
                print()
            return "wake", None
        if not prompt_visible:
            print("s13 >> ", end="", flush=True)  # 手写提示符（因为要交替监听）
            prompt_visible = True
        if os.name == "nt":
            # Windows 分支：select.select 不支持非 socket，改用后台线程 + 队列。
            _start_stdin_reader()
            try:
                line = _stdin_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if line is None:
                return "quit", None  # EOF
            return "user", line.rstrip("\n")
        # select 带 0.25 秒超时：既能及时响应输入，又能定期回头检查信箱。
        readable, _, _ = select.select([sys.stdin], [], [], 0.25)
        if readable:
            line = sys.stdin.readline()
            if line == "":
                return "quit", None  # EOF（Ctrl+D / 管道结束）
            return "user", line.rstrip("\n")


if __name__ == "__main__":
    print("s13: agent teams")
    print("Enter a question, press Enter to send. Type q to quit.\n")
    history = []          # Lead 的对话历史
    had_teammates = False  # 用于判断「刚刚还有队友，现在全关了」好打印提示

    while True:
        kind, payload = wait_for_cli_event()
        if kind == "quit":
            break
        if kind == "user":
            # 用户输入
            if payload is None or payload.strip().lower() in {"q", "exit", ""}:
                break
            trigger_hooks("UserPromptSubmit", payload)
            history.append({"role": "user", "content": payload})
        else:
            # 团队事件（wake）：取走信箱；如果全是协议回复而没有真正要看的消息，就跳过本轮。
            inbox = consume_lead_inbox()
            if not inbox:
                continue
            history.append({
                "role": "user",
                "content": format_team_events(inbox),
            })
            print(f"[wake: {len(inbox)} team event(s) -> new turn]")

        agent_loop(history)
        print_last_assistant_message(history)

        if active_teammates:
            had_teammates = True
        elif had_teammates and not BUS.peek("lead"):
            # 队友全没了，信箱也空了 → 收尾提示。
            print("[all teammates shut down]")
            had_teammates = False
        print()
