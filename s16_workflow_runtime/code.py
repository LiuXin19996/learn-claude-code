#!/usr/bin/env python3
"""
s16: Workflow Runtime - run a saved orchestration through one tool call.

Run:
  python s16_workflow_runtime/code.py
  python s16_workflow_runtime/code.py demo
  python s16_workflow_runtime/code.py resume

    +-------------+       +--------------------------------+
    | Agent loop  | ----> | Workflow(name, args, run_id)  |
    +-------------+       +---------------+----------------+
                                          |
                           +--------------+--------------+
                           | agent | parallel | pipeline  |
                           +--------------+--------------+
                                          |
                                   journal + result

（s16：工作流运行时 —— 用一次工具调用跑完一整套「预先写好的」编排脚本。

    和前 15 课的区别：之前的编排逻辑（先审哪几个维度、怎么并行、什么时候
    验证）都由模型每一轮临时决定；本课把它固化成一段可信代码（WORKFLOWS
    注册表里的函数），模型只能按名字调用并传入 args。脚本由宿主提供，
    模型无法篡改，这就是「saved orchestration（已保存的编排）」。

    四个关键设计：
      1. 原语：ExecutionState 注入 agent() / parallel() / pipeline() /
         workflow() / phase() / log()，脚本像写普通 Python 一样写编排；
      2. 持久化：每次 agent() 的结果按「语义键」追加进 <runId>.journal.jsonl，
         resume 时命中的调用直接回放缓存，不再重复花钱；
      3. 防护：runId 唯一并加锁、单次 run 的 agent 数与 token 预算上限、
         并发信号量、schema 校验失败自动重试一次；
      4. 可观测：LocalWorkflowTask 记录状态/用量/progress 事件，结束时把
         快照和输出写成 JSON 文件。

    阅读顺序建议：
      1. 先看 WorkflowTool.call -> _call_locked：一次 run 的完整生命周期；
      2. 再看 ExecutionState：编排脚本能用的原语都在这里；
      3. 最后看 sample_workflow：一个「审查改动 -> 逐条验证」的示例编排。

    更详细的英文说明、图示和用法见同目录 README.md / README.zh.md。
）"""

import asyncio              # 异步编排原语：gather、Semaphore、to_thread
import fcntl                # POSIX 跨进程文件锁，保证同一个 run 只被一个进程执行
import hashlib              # sha256：生成跨进程稳定的语义哈希（journal 缓存键）
import importlib.util       # 按文件路径动态加载 s15 集成 host
import json                 # 快照、journal、工具输入输出的 JSON 序列化
import os                   # O_CREAT|O_EXCL 原子占位、os.replace 原子替换、平台判断
import re                   # workflow 名称与 runId 的格式校验
import secrets              # 生成不可预测的 runId 随机后缀
import sys                  # 命令行参数、把动态加载的模块注册进 sys.modules
import threading            # 进程内 run 级互斥锁
from contextlib import contextmanager  # 实现 workflow_run_lock 上下文管理器
from dataclasses import dataclass      # RunnerOutput：结果值 + token 数
from pathlib import Path               # 面向对象的路径操作

# 注入给真实子代理的系统提示：说明宿主 OS 和能力边界。
# 子代理只能回答传入的这一步，没有 shell / 文件工具，不能编造工具结果。
WORKFLOW_AGENT_ENVIRONMENT = (
    "Host OS: Windows. This workflow agent only responds to its supplied step; "
    "it has no direct shell or file tools."
    if os.name == "nt"
    else "Host OS: Unix-like. This workflow agent only responds to its supplied "
         "step; it has no direct shell or file tools."
)

# -- Runtime Guards --
# （中文）运行时护栏常量。
# AGENT_CAP：单次 run 最多允许多少次 agent() 调用；
# CONCURRENCY：同时真正在跑的 agent 数量上限（信号量）；
# STORE：快照、journal、锁文件都落在本文件同级的 .runtime 目录；
# MISS：journal 缓存未命中的哨兵——用独立 object 而不是 None，
#       因为缓存值本身也可能是合法的 None。
AGENT_CAP = 1000                       # hard cap on agent() calls per run
CONCURRENCY = 8                        # parallelism cap (semaphore)
STORE = Path(__file__).parent / ".runtime"   # snapshots + journals live here
MISS = object()                        # journal cache miss sentinel
# （中文）WORKFLOW_NAME_RE：meta.name 必须是 1-64 位，字母/数字开头，
# 只能包含字母、数字、'.'、'_'、'-'（要能安全地拼进文件名）。
# RUN_ID_RE：runId 形如 wf_<name>_<16 位小写十六进制>。
WORKFLOW_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RUN_ID_RE = re.compile(r"^wf_[A-Za-z0-9][A-Za-z0-9._-]{0,63}_[0-9a-f]{16}$")


def _stable_hash(s: str) -> int:
    """Process-stable hash (Python's hash() is salted per process, which would
    break resume keys across `run` and `resume`)."""
    # （中文）用 sha256 而不是内置 hash()：Python 的内置哈希每个进程都会加盐，
    # 同一个字符串在 `run` 和 `resume` 两个进程里算出的值不同，
    # journal 的缓存键就对不上了。这里把 sha256 的十六进制摘要转成整数。
    return int(hashlib.sha256(s.encode()).hexdigest(), 16)


def create_run_id(meta) -> str:
    # （中文）wf_<workflow 名>_<8 字节随机十六进制>。
    # 随机部分由 secrets 生成，避免不同 run 之间互相猜中。
    return f"wf_{meta['name']}_{secrets.token_hex(8)}"


def reserve_run_id(meta) -> str:
    """Reserve a fresh run identity before any journal can be truncated."""
    # （中文）先「占坑」再执行：用 O_CREAT|O_EXCL 原子地创建 <runId>.json，
    # 文件已存在说明撞号，换一个重试（最多 32 次）。这样即使两个进程
    # 同时发起新 run，也不会共用同一个 runId 而互相覆盖 journal。
    STORE.mkdir(parents=True, exist_ok=True)
    for _ in range(32):
        run_id = validate_run_id(create_run_id(meta))
        snapshot_path = STORE / f"{run_id}.json"
        try:
            fd = os.open(snapshot_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        return run_id
    raise WorkflowInputError("could not allocate a unique workflow runId")


def create_task_id(run_id) -> str:
    # （中文）任务系统里的本地任务 ID，与 runId 一一对应。
    return f"local_workflow_{run_id}"


def validate_run_id(run_id):
    # （中文）校验 runId 格式。resume 时用户传进来的 ID 也要先过这一关，
    # 因为 runId 会被直接拼进 .runtime 目录下的文件名（防止路径穿越）。
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise WorkflowInputError("invalid workflow runId")
    return run_id


# -- Errors --
class WorkflowInputError(Exception):
    """Bad workflow, metadata, or schema input."""
    # （中文）所有「输入/编排数据不合法」的错误都归到这一类：
    # 上层据此把错误信息回给模型，而不是当成宿主内部崩溃。


# （中文）进程内：每个 runId 一把 threading.Lock。
# 同一进程已经持有某个 run 时，再次进入直接报错，而不是排队等待。
_run_locks_guard = threading.Lock()
_run_locks: dict[str, threading.Lock] = {}


@contextmanager
def workflow_run_lock(run_id: str):
    """Hold one run across threads and host processes for its full lifecycle."""
    # （中文）两层锁，整个 run 生命周期都持有：
    #   1. threading.Lock —— 防止同一进程内的两个协程/线程同时跑同一个 run；
    #   2. fcntl.flock     —— 防止另一个宿主进程并发跑同一个 run。
    # 两者都非阻塞获取，拿不到就说明该 run 正在执行中。
    with _run_locks_guard:
        local_lock = _run_locks.setdefault(run_id, threading.Lock())
    if not local_lock.acquire(blocking=False):
        raise WorkflowInputError(f"workflow run {run_id} is already active")

    handle = None
    try:
        STORE.mkdir(parents=True, exist_ok=True)
        handle = (STORE / f"{run_id}.lock").open("a+", encoding="utf-8")
        try:
            # （中文）LOCK_EX 排他 + LOCK_NB 不等待：被别的进程占着就立刻失败。
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WorkflowInputError(
                f"workflow run {run_id} is already active"
            ) from exc
        yield
    finally:
        # （中文）无论正常结束还是异常退出，都释放文件锁、关闭句柄，
        # 并清理进程内锁表（只有确认无人持有时才删除条目）。
        if handle is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        local_lock.release()
        with _run_locks_guard:
            if not local_lock.locked() and _run_locks.get(run_id) is local_lock:
                _run_locks.pop(run_id, None)


# -- Metadata Validation --
def validate_meta(meta):
    """Validate name, description, and optional phases before launch."""
    # （中文）meta 是编排脚本自己声明的「启动信封」：
    #   name（必填，全局唯一）、description（必填）、
    #   phases（可选，阶段标题列表，只用于播报和展示）。
    # name 会被拼进文件名，所以限制为 1-64 位 slug。
    if not isinstance(meta, dict):
        raise WorkflowInputError("meta must be an object literal")
    if not meta.get("name") or not meta.get("description"):
        raise WorkflowInputError("meta requires `name` and `description`")
    if not isinstance(meta["name"], str) or not WORKFLOW_NAME_RE.fullmatch(meta["name"]):
        raise WorkflowInputError(
            "meta.name must be a 1-64 character slug using letters, numbers, '.', '_', or '-'"
        )
    if not isinstance(meta["description"], str):
        raise WorkflowInputError("meta.description must be a string")
    if "phases" in meta:
        # （中文）phases 必须是「非空字符串列表」；不写则视为没有阶段。
        if not isinstance(meta["phases"], list) or not all(
            isinstance(phase, str) and phase for phase in meta["phases"]
        ):
            raise WorkflowInputError("meta.phases must be a list of non-empty strings")
    return meta


def check_permission(meta, settings=None):
    """Apply the s03 allow/deny gate before launching a workflow."""
    # （中文）沿用 s03 的权限思路：settings["deny"] 里列出的 workflow 名
    # 直接拒绝；这里只演示 deny 名单，默认放行。
    settings = settings or {}
    if meta["name"] in settings.get("deny", []):
        raise WorkflowInputError(f"workflow '{meta['name']}' denied by settings")
    return "allow"


# -- Minimal JSON Schema --
class SimpleJsonSchema:
    """Tiny validator backing agent({schema}):
    object/array/string/boolean/number + required keys."""
    # （中文）只为 agent({schema}) 服务的迷你校验器：支持 enum、type、
    # required、properties、items 五种关键字，够 demo 用，不追求完整规范。

    def __init__(self, schema):
        self.schema = schema

    def validate(self, value, schema=None):
        # （中文）递归校验：schema=None 时从根 schema 开始；
        # 返回值是 (是否合法, 错误信息)。
        schema = self.schema if schema is None else schema
        if "enum" in schema and value not in schema["enum"]:
            return False, f"expected one of {schema['enum']}"
        t = schema.get("type")
        if t == "object":
            if not isinstance(value, dict):
                return False, "expected object"
            # （中文）required 里的键必须都存在；properties 中声明过的键再递归校验。
            for key in schema.get("required", []):
                if key not in value:
                    return False, f"missing required key '{key}'"
            for key, sub in schema.get("properties", {}).items():
                if key in value:
                    ok, err = self.validate(value[key], sub)
                    if not ok:
                        return False, f"{key}: {err}"
            return True, None
        if t == "array":
            if not isinstance(value, list):
                return False, "expected array"
            items = schema.get("items")
            if items:
                # （中文）逐元素按 items 子 schema 校验，错误信息带上下标。
                for i, el in enumerate(value):
                    ok, err = self.validate(el, items)
                    if not ok:
                        return False, f"[{i}]: {err}"
            return True, None
        if t == "string":
            return (isinstance(value, str), None if isinstance(value, str) else "expected string")
        if t == "boolean":
            return (isinstance(value, bool), None if isinstance(value, bool) else "expected boolean")
        if t in ("number", "integer"):
            # （中文）bool 是 int 的子类，必须显式排除，否则 True/False 会被当成数字。
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
            return (ok, None if ok else "expected number")
        # （中文）没写 type 的 schema 一律放行。
        return True, None


def _fill_schema(schema, seed):
    """Deterministic generic filler used for schemas the mock doesn't special-case."""
    # （中文）mock runner 专用：对任意 schema 都能「编」出一份结构合法、
    # 且由 seed 完全决定的确定性值，让 demo/测试无需真实模型也能跑通。
    t = schema.get("type")
    if t == "object":
        # （中文）优先只填 required 键；没写 required 就把 properties 全部填上。
        keys = schema.get("required") or list(schema.get("properties", {}))
        return {k: _fill_schema(schema["properties"][k], f"{seed}/{k}") for k in keys}
    if t == "array":
        # （中文）数组固定只填一个元素，seed 加 "/0" 继续往下递归。
        return [_fill_schema(schema["items"], f"{seed}/0")]
    if t == "boolean":
        # （中文）用哈希取模造出确定性的 True/False（约 3/4 为 True）。
        return _stable_hash(seed) % 4 != 0
    if t in ("number", "integer"):
        # （中文）确定性的小整数 0-4。
        return _stable_hash(seed) % 5
    # （中文）兜底：取 seed 最后一段作为字符串值。
    return seed.rsplit("/", 1)[-1]


# -- Agent Runners --
# （中文）agent() 实际执行者的两种实现：
#   MockAgentRunner     —— 确定性伪造，demo 和单元测试用，不需要 API key；
#   AnthropicAgentRunner —— 真正调用 Claude API。


@dataclass(frozen=True)
class RunnerOutput:
    # （中文）runner 的返回值：结果本身 + 本次消耗的 token 数（用于预算统计）。
    value: object
    tokens: int


class MockAgentRunner:
    """Deterministic runner used by demo mode and unit tests."""

    def run(self, prompt, schema=None, label=None):
        # （中文）没有 schema 就回一段截断后的文本；
        # 有 schema 时对两种示例 schema（findings / isReal）做特判，
        # 其余交给 _fill_schema 生成确定性结构。
        if schema is None:
            value = f"[mock] {(label or prompt)[:60]}"
            return RunnerOutput(value, self._tokens(prompt, value))
        props = schema.get("properties", {})
        if "findings" in props:
            # （中文）每个审查维度生成 1-2 条 finding，严重度由哈希决定。
            n = 1 + (_stable_hash(prompt) % 2)
            sev = ["high", "medium", "low"]
            value = {"findings": [
                {"title": f"{label or 'audit'} #{i + 1}",
                 "severity": sev[_stable_hash(prompt + str(i)) % 3]}
                for i in range(n)
            ]}
        elif "isReal" in props:
            # （中文）约 3/4 的 finding 被判为「真实」，让 demo 有过滤效果。
            real = _stable_hash(prompt) % 4 != 0
            value = {"isReal": real,
                     "reason": "reproduced" if real else "could not reproduce"}
        else:
            value = _fill_schema(schema, prompt)
        return RunnerOutput(value, self._tokens(prompt, value))

    @staticmethod
    def _tokens(prompt, result):
        # （中文）粗略估算 token：约 4 个字符算 1 token。
        return len(prompt) // 4 + len(json.dumps(result, default=str)) // 4


def _response_text(response) -> str:
    # （中文）Anthropic 响应的 content 是一个 block 列表，这里只取 type=="text"
    # 的块拼起来；其他类型的块直接忽略。
    return "\n".join(
        str(getattr(block, "text", ""))
        for block in getattr(response, "content", [])
        if getattr(block, "type", None) == "text"
    ).strip()


def _parse_runner_json(text: str) -> object:
    # （中文）模型常把 JSON 包在 ``` 代码围栏里，先剥掉首尾围栏；
    # 整体解析失败后，再从左到右找每个 '{'，用 raw_decode 尝试解析
    # 第一个完整的 JSON 值（容忍前后夹着解释性文字）。
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        lines = lines[1:] if lines else lines
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for position, character in enumerate(stripped):
            if character != "{":
                continue
            try:
                value, _ = decoder.raw_decode(stripped[position:])
            except json.JSONDecodeError:
                continue
            return value
        # （中文）连一个合法 JSON 都找不到，抛输入错误（上层会据此重试一次）。
        raise WorkflowInputError("workflow agent returned invalid JSON")


class AnthropicAgentRunner:
    """Run workflow agents through the same API client as the host."""

    def __init__(self, client, model):
        self.client = client
        self.model = model

    def run(self, prompt, schema=None, label=None):
        # （中文）有 schema 时把 schema 的 JSON 一并塞进 prompt，并要求
        # 「只返回一个 JSON 对象」——子代理没有工具调用，只能靠提示约束格式。
        request = prompt
        if schema is not None:
            request += (
                "\n\nReturn only one JSON object matching this schema:\n"
                + json.dumps(schema, ensure_ascii=True, sort_keys=True)
            )
        # （中文）直接同步调用 SDK；ExecutionState 会用 asyncio.to_thread
        # 把它丢进线程池，避免阻塞事件循环。
        response = self.client.messages.create(
            model=self.model,
            system=(
                "You are a focused workflow agent. Complete only the supplied step. "
                + f"{WORKFLOW_AGENT_ENVIRONMENT} "
                + "Do not claim access to files or results not included in the prompt."
            ),
            messages=[{"role": "user", "content": request}],
            max_tokens=2000,
        )
        text = _response_text(response)
        if schema is None:
            value = text
        else:
            try:
                value = _parse_runner_json(text)
            except WorkflowInputError:
                # Let ExecutionState's schema check trigger its single retry.
                # （中文）解析失败也先把原始文本交上去：后面的 schema 校验会失败，
                # 从而触发 ExecutionState 里统一的「再试一次」逻辑。
                value = text
        # （中文）用量 = 输入 token + 输出 token，全部计入本次 run 的预算。
        usage = getattr(response, "usage", None)
        tokens = int(getattr(usage, "input_tokens", 0) or 0) + int(
            getattr(usage, "output_tokens", 0) or 0
        )
        return RunnerOutput(value, tokens)


# （中文）默认用 mock；install_workflow_tool 挂到 s15 host 时会换成真实 runner。
RUNNER_FACTORY = MockAgentRunner


# -- Journal --
class WorkflowJournal:
    """Append-only <runId>.journal.jsonl. On resume, agent() calls whose
    semantic key is already present are replayed from cache instead of re-run."""
    # （中文）追加式日志：每行一条 {"key": ..., "value": ...}。
    # resume 时先把历史记录读进 cache，之后 agent() 只要键命中就直接回放，
    # 只有未命中的（例如这轮新加的步骤）才真正调用模型。

    def __init__(self, run_id, resume, store=None):
        store = STORE if store is None else store
        store.mkdir(parents=True, exist_ok=True)
        self.path = store / f"{run_id}.journal.jsonl"
        self.resume = resume
        self.cache = {}
        if resume:
            # （中文）resume 模式：journal 必须存在；逐行解析并做基本结构校验，
            # 任何一行坏掉都直接报错，避免带着残缺状态继续跑。
            if not self.path.exists():
                raise WorkflowInputError(f"resume journal not found for {run_id}")
            for line_number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
                try:
                    rec = json.loads(line)
                    if (
                        not isinstance(rec, dict)
                        or not isinstance(rec.get("key"), str)
                        or "value" not in rec
                    ):
                        raise ValueError("expected key/value record")
                except (json.JSONDecodeError, ValueError) as exc:
                    raise WorkflowInputError(
                        f"invalid resume journal record at line {line_number}"
                    ) from exc
                self.cache[rec["key"]] = rec["value"]
            # （中文）以追加模式打开，本次新产生的记录继续写到文件尾部。
            self._f = self.path.open("a", encoding="utf-8")
        else:
            # （中文）全新 run：以写模式打开即清空旧 journal（一般也不会撞上旧文件）。
            self._f = self.path.open("w", encoding="utf-8")             # fresh run truncates

    def key(self, kind, label, prompt, schema):
        # Deterministic semantic key, independent of concurrency order, so a
        # parallel/pipeline call gets the same key on resume.
        # （中文）语义键只由「调用类型 + label + prompt + schema」决定，
        # 与并发顺序、线程调度无关，所以 parallel/pipeline 里的同一次调用
        # 在 resume 时能算出同一个键。
        basis = f"{kind}|{label}|{prompt}|{json.dumps(schema, sort_keys=True)}"
        return f"{kind}-{_stable_hash(basis) % 10**10:010d}"

    def cached(self, key):
        # （中文）命中返回缓存值，未命中返回哨兵 MISS。
        return self.cache.get(key, MISS)

    def record(self, key, value):
        # （中文）先写文件并 flush，再更新内存 cache：立刻落盘，
        # 进程中途崩溃也不会丢掉已经完成的步骤。
        self._f.write(json.dumps({"key": key, "value": value}) + "\n")
        self._f.flush()
        self.cache[key] = value

    def close(self):
        self._f.close()


# -- Token Budget --
class Budget:
    """budget.total / spent() / remaining(). Once spent reaches total, agent()
    calls raise instead of silently overspending."""
    # （中文）整个 run（包括嵌套 workflow）共享的 token 预算。
    # total=None 表示不限额（remaining() 返回无穷大）。

    def __init__(self, total=None):
        self.total = total
        self._spent = 0

    def add(self, n):
        # （中文）一旦累计将超过上限就抛错，让编排停在半路而不是悄悄烧钱。
        if self.total is not None and self._spent + n > self.total:
            raise WorkflowInputError(
                f"token budget exceeded ({self._spent + n} > {self.total})"
            )
        self._spent += n

    def spent(self):
        return self._spent

    def remaining(self):
        # （中文）不限额时剩余量视为无穷大；限额时最小取 0。
        return float("inf") if self.total is None else max(0, self.total - self._spent)


# -- Workflow Task Lifecycle --
class LocalWorkflowTask:
    """Hold workflow status, usage, and progress events."""

    def __init__(self, task_id, run_id, meta):
        # （中文）status: running / completed / failed；
        # usage 统计本次 run 调用了多少个 agent、花了多少 token；
        # progress 保存所有可回放给宿主的进度事件。
        self.task_id = task_id
        self.run_id = run_id
        self.meta = meta
        self.status = "running"
        self.usage = {"agents": 0, "tokens": 0}
        self.progress = []

    def event(self, name, **data):
        # （中文）生命周期事件（async_launched / task_started /
        # task_notification），目前只打印到终端。
        line = " ".join(f"{k}={v}" for k, v in data.items())
        print(f"  event      {name:<18} {line}")

    def progress_event(self, ptype, **data):
        # （中文）脚本级进度（workflow_phase / workflow_log / workflow_agent），
        # 既追加进 progress 列表（随快照持久化），也实时打印。
        self.progress.append({"type": ptype, **data})
        line = " ".join(f"{k}={v}" for k, v in data.items())
        print(f"  progress   {ptype:<16} {line}")


# -- Workflow Primitives --
# （中文）下面这几个类就是「编排脚本能看到的全部 API」。
class ExecutionLimits:
    """Shared run-wide limits, including nested workflows."""
    # （中文）特意放在 ExecutionState 之外：嵌套子 workflow 共享同一份
    # 计数器和信号量，否则子 workflow 可以绕过 agent 上限。

    def __init__(self):
        self.agents = 0
        self.semaphore = asyncio.Semaphore(CONCURRENCY)

    def claim_agent(self):
        # （中文）先计数再判断，保证超限时立刻失败；这一步在事件循环线程里
        # 同步执行，不存在竞态。
        self.agents += 1
        if self.agents > AGENT_CAP:
            raise WorkflowInputError(f"agent() cap reached ({AGENT_CAP})")


class ExecutionState:
    """Injected into the workflow script with the orchestration primitives."""
    # （中文）编排函数拿到的 ctx 就是它。脚本通过 ctx.agent()/parallel()/
    # pipeline()/workflow()/phase()/log() 来编排，看不到底层 runner 和 journal，
    # 这样原语的实现可以自由替换而不影响脚本。

    def __init__(self, task, journal, runner, budget, args, depth=0, limits=None):
        self.task = task
        self.journal = journal
        self.runner = runner
        self.budget = budget
        self.args = args
        self._depth = depth
        self._phase = None
        self._phases_seen = set()
        self._limits = limits or ExecutionLimits()

    def phase(self, title):
        """Start a phase; subsequent agent()s group under it. Upsert: emitting the
        same phase again (e.g. from each pipeline item) does not re-announce it."""
        # （中文）当前阶段只是个「标签」：之后每个 agent() 的进度事件都会带上它。
        # 用 set 去重，pipeline 的每个 item 都调用 phase("Verify") 也只播报一次。
        self._phase = title
        if title not in self._phases_seen:
            self._phases_seen.add(title)
            self.task.progress_event("workflow_phase", title=title)

    def log(self, message):
        """Emit a workflow_log progress line."""
        # （中文）脚本自己的日志，和 agent 进度事件走同一条 progress 通道。
        self.task.progress_event("workflow_log", message=message)

    async def agent(self, prompt, schema=None, label=None, phase=None):
        """Spawn one subagent. With a schema, force StructuredOutput + validate
        (retry once). On resume, a cached key short-circuits the run."""
        # （中文）agent() 是整个运行时的核心，依次做六件事：
        #   1. 计数 + 预算检查（超限直接失败）；
        #   2. 算语义键、查 journal 缓存，命中则校验 schema 后直接返回；
        #   3. 未命中：在并发信号量下把同步 runner 丢进线程池执行；
        #   4. 有 schema 时校验输出，不合法就带「请返回合法 JSON」重试一次；
        #   5. 把 token 记账到 Budget 和 task.usage；
        #   6. 写 journal、播报 workflow_agent 进度、返回结果。
        label = label or (prompt[:24] + "...")
        self._limits.claim_agent()
        if self.budget.remaining() <= 0:
            raise WorkflowInputError("token budget exceeded")

        key = self.journal.key("agent", label, prompt, schema)
        cached = self.journal.cached(key)
        if cached is not MISS:
            # （中文）缓存命中：resume 时不重复花钱，但仍校验一次 schema，
            # 防止旧缓存与当前 schema 不兼容。
            if schema is not None:
                ok, err = SimpleJsonSchema(schema).validate(cached)
                if not ok:
                    raise WorkflowInputError(
                        f"cached agent output failed schema validation: {err}"
                    )
            self.task.progress_event("workflow_agent", label=label,
                                     phase=phase or self._phase, status="cached")
            return cached

        async with self._limits.semaphore:
            # （中文）Anthropic SDK 是同步的，用 to_thread 包一层，
            # 多个 agent() 才能真正并发；信号量限制同时进行的数量。
            run = await asyncio.to_thread(
                self.runner.run, prompt, schema, label
            )
            result = run.value
            tokens = run.tokens

        if schema is not None:
            # （中文）第一次输出不合法：追加一句要求后重试一次；仍不合法就失败。
            ok, err = SimpleJsonSchema(schema).validate(result)
            if not ok:
                retry = await asyncio.to_thread(
                    self.runner.run,
                    prompt + "\n\nReturn valid JSON.",
                    schema,
                    label,
                )
                result = retry.value
                tokens += retry.tokens
                ok, err = SimpleJsonSchema(schema).validate(result)
                if not ok:
                    raise WorkflowInputError(f"agent({{schema}}) invalid output: {err}")

        # （中文）记账 + 落盘 + 播报；重试消耗的 token 也一并计入。
        self.budget.add(tokens)
        self.task.usage["agents"] += 1
        self.task.usage["tokens"] += tokens
        self.journal.record(key, result)
        self.task.progress_event("workflow_agent", label=label,
                                 phase=phase or self._phase, status="done")
        return result

    async def parallel(self, thunks):
        """BARRIER: run all thunks concurrently and fail if any thunk fails."""
        # （中文）屏障语义：等全部完成才返回；任何一个抛异常，gather 都会
        # 让整个 parallel 失败，进而让 workflow 失败。
        return await asyncio.gather(*[thunk() for thunk in thunks])

    async def pipeline(self, items, *stages):
        """Per-item staged flow, NO barrier between stages: item A can be in
        stage 3 while item B is still in stage 1. Each stage gets
        (prev_result, original_item, index). A throwing stage fails the workflow."""
        # （中文）流式流水线：每个 item 各自从 stage 1 走到最后，
        # 阶段之间没有全局屏障——item A 可能已经在 stage 3，item B 才刚进 stage 1，
        # 谁先完成谁先推进，吞吐更高（代价是总耗时由最慢的 item 决定）。
        async def run_item(item, idx):
            value = item
            for stage in stages:
                # （中文）stage 的签名固定为：(上一步结果, 原始 item, 下标)。
                value = await stage(value, item, idx)
            return value
        return await asyncio.gather(*[run_item(it, i) for i, it in enumerate(items)])

    async def workflow(self, name, args=None):
        """Run a saved workflow inline as a child (one level), sharing this run's
        journal + budget + agent counter."""
        # （中文）在脚本里内联调用另一个已注册的 workflow：
        #   - 只允许一层嵌套（depth >= 1 直接拒绝），防止无限递归；
        #   - 子 workflow 复用同一个 task/journal/budget/limits，
        #     所以子步骤也计入同一个 token 预算和 agent 上限。
        if self._depth >= 1:
            raise WorkflowInputError("workflow() nesting is one level only")
        if name not in WORKFLOWS:
            raise WorkflowInputError(f"unknown workflow '{name}'")
        meta, fn = WORKFLOWS[name]
        child = ExecutionState(self.task, self.journal, self.runner, self.budget,
                               args or {}, depth=self._depth + 1,
                               limits=self._limits)
        return await fn(child, args or {})


# -- Workflow Tool --
class WorkflowTool:
    """The Workflow tool. .call() validates meta, runs the permission check,
    creates runId/taskId, registers a LocalWorkflowTask, and emits lifecycle
    events while executing the script. It returns the result and task state and
    supports resume."""
    # （中文）一次 Workflow 工具调用的完整入口：
    # 校验 meta -> 权限检查 -> 分配 runId -> 持 run 锁 -> 执行 -> 持久化结果。

    async def call(self, meta, script_fn, args=None, resume_from_run_id=None):
        # （中文）resume 时 runId 由调用方给出并校验格式；
        # 新 run 则先 reserve_run_id 原子占坑。执行期间一直持有 workflow_run_lock。
        validate_meta(meta)
        check_permission(meta)
        resuming = resume_from_run_id is not None
        if resuming:
            run_id = validate_run_id(resume_from_run_id)
        else:
            run_id = reserve_run_id(meta)
        with workflow_run_lock(run_id):
            return await self._call_locked(
                meta, script_fn, args, run_id, resuming
            )

    async def _call_locked(self, meta, script_fn, args, run_id, resuming):
        if resuming:
            # （中文）resume 必须与原始 run 对得上：workflow 名一致、
            # args 一致（不传则沿用快照里的 args），
            # 然后以追加模式打开 journal，准备缓存回放。
            snapshot = _read_snapshot(run_id)
            if snapshot.get("workflowName") != meta["name"]:
                raise WorkflowInputError("resume runId does not match workflow meta")
            saved_args = snapshot.get("args", {})
            if args is None:
                args = saved_args
            elif args != saved_args:
                raise WorkflowInputError("resume args do not match the original run")
            journal = WorkflowJournal(run_id, resume=True)
        else:
            args = args or {}
            journal = WorkflowJournal(run_id, resume=False)
        task_id = create_task_id(run_id)

        task = LocalWorkflowTask(task_id, run_id, meta)
        # Record the launch envelope before workflow execution starts.
        # （中文）先写「启动信封」：即使脚本随后崩溃，宿主也能从快照里
        # 查到这次 run 的 runId/taskId/名字，方便恢复和排查。
        launched = {"status": "async_launched", "taskId": task_id,
                    "taskType": "local_workflow", "runId": run_id,
                    "workflowName": meta["name"]}
        task.event("async_launched", runId=run_id, taskId=task_id)
        task.event("task_started", workflow=meta["name"],
                   phases=",".join(meta.get("phases", [])) or "-",
                   resume=resuming)
        _write_json(STORE / f"{run_id}.json", {
            "runId": run_id,
            "workflowName": meta["name"],
            "args": args,
            "task": serialize_task(task),
        })

        try:
            # （中文）把原语上下文交给可信脚本；budget 来自 args["budget"]。
            ctx = ExecutionState(
                task, journal, RUNNER_FACTORY(), Budget(args.get("budget")), args
            )
            result = await script_fn(ctx, args)
            task.status = "completed"
        except Exception as e:                          # failed / stopped close the loop too
            # （中文）脚本内任何异常都转成 failed 状态，并把错误信息作为结果返回，
            # 而不是让整个工具调用崩掉。
            task.status = "failed"
            result = {"error": str(e)}
        finally:
            # （中文）无论如何都要关闭 journal 文件句柄。
            journal.close()

        # （中文）执行结束后统一落盘：结果写单独的 .output.json，
        # 快照更新为最终状态，并记录 last_run 供之后 resume。
        _write_json(STORE / f"{run_id}.output.json", result)
        _write_json(STORE / f"{run_id}.json", {
            "runId": run_id,
            "workflowName": meta["name"],
            "args": args,
            "task": serialize_task(task),
        })
        _save_last_run(run_id)
        task.event("task_notification", status=task.status,
                   agents=task.usage["agents"], tokens=task.usage["tokens"],
                   outputFile=f".runtime/{run_id}.output.json")
        return {"launched": launched, "result": result, "task": task}


def _write_json(path, value):
    # （中文）原子写：先写同目录的 .tmp 文件，再 os.replace 覆盖目标文件。
    # replace 在同一文件系统内是原子操作，读者永远看不到写了一半的 JSON。
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, default=str), encoding="utf-8")
    os.replace(temporary, path)


def _read_snapshot(run_id):
    # （中文）读取 run 快照；文件缺失、JSON 损坏、或顶层不是对象，
    # 都视为 resume 输入错误。
    path = STORE / f"{run_id}.json"
    if not path.exists():
        raise WorkflowInputError(f"resume snapshot not found for {run_id}")
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WorkflowInputError(f"invalid resume snapshot for {run_id}") from exc
    if not isinstance(snapshot, dict):
        raise WorkflowInputError(f"invalid resume snapshot for {run_id}")
    return snapshot


def _save_last_run(run_id):
    # （中文）把最近一次 runId 写进 last_run.txt，供 `demo resume` / `resume` 使用。
    (STORE / "last_run.txt").write_text(run_id, encoding="utf-8")


def _read_last_run():
    # （中文）没有历史 run 时返回 None。
    p = STORE / "last_run.txt"
    return p.read_text(encoding="utf-8").strip() if p.exists() else None


# -- Sample Workflow --
# （中文）示例编排「review-changes」：按四个维度并行审查改动，
# 再对每条 finding 各起一个子代理做对抗验证，只保留确认属实的问题。
# 下面两个 schema 分别约束「审查输出」和「验证结论」。
FINDINGS_SCHEMA = {
    "type": "object", "required": ["findings"],
    "properties": {"findings": {"type": "array", "items": {
        "type": "object", "required": ["title", "severity"],
        "properties": {
            "title": {"type": "string"},
            "severity": {
                "type": "string", "enum": ["high", "medium", "low"]
            },
        }}}},
}
VERDICT_SCHEMA = {
    "type": "object", "required": ["isReal", "reason"],
    "properties": {"isReal": {"type": "boolean"}, "reason": {"type": "string"}},
}

# （中文）meta：名字 + 描述 + 阶段列表（阶段只影响播报和展示）。
SAMPLE_META = {
    "name": "review-changes",
    "description": "Review changed files across dimensions, verify each finding",
    "phases": ["Review", "Verify"],
}

# （中文）演示用的审查维度，以及一段带 SQL 注入隐患的示例改动。
DIMENSIONS = ["correctness", "security", "performance", "style"]
DEMO_CHANGES = (
    "def load_user(user_id):\n"
    "    query = f\"SELECT * FROM users WHERE id = {user_id}\"\n"
    "    return db.execute(query).fetchone()\n"
)


async def sample_workflow(ctx, args):
    """pipeline over review dimensions (audit -> verify-each), then keep only the
    findings a verifier confirms. The plan is code, not a chat turn."""
    # （中文）整张编排图都写死在代码里：
    #   pipeline(维度) -> audit 生成 findings -> verify 逐条对抗验证 -> 过滤。
    # 模型只负责每一步的内容判断，不负责决定流程。
    ctx.phase("Review")
    changes = args.get("changes", "")
    if not isinstance(changes, str):
        raise WorkflowInputError("args.changes must be a string")
    review_input = changes.strip() or "No change context was supplied."

    async def audit(_value, dimension, _idx):
        # （中文）pipeline 的 stage 签名固定是 (上一步结果, 原始 item, 下标)；
        # 第一步没有上一步结果，所以第一个参数不用。
        out = await ctx.agent(
            f"Review this change context for {dimension} issues. "
            "Report only issues supported by the supplied text.\n\n"
            f"{review_input}",
            schema=FINDINGS_SCHEMA, label=f"audit:{dimension}", phase="Review")
        return {"dimension": dimension, "findings": out["findings"]}

    async def verify(audited, dimension, _idx):
        # （中文）每条 finding 起一个独立的验证子代理，用 parallel 并发执行。
        ctx.phase("Verify")
        # Each finding is verified by its own adversarial subagent, concurrently.
        verdicts = await ctx.parallel([
            (lambda f=f: ctx.agent(
                f"Adversarially verify this {dimension} finding against the "
                "supplied change context.\n\n"
                f"Change context:\n{review_input}\n\n"
                f"Finding:\n{json.dumps(f, ensure_ascii=True)}",
                schema=VERDICT_SCHEMA, label=f"verify:{dimension}:{f['title']}", phase="Verify"))
            for f in audited["findings"]])
        # （中文）verdicts 与 findings 顺序一一对应，只保留 isReal 为真的。
        confirmed = [f for f, v in zip(audited["findings"], verdicts)
                     if v and v.get("isReal")]
        return {"dimension": dimension, "confirmed": confirmed}

    results = await ctx.pipeline(DIMENSIONS, audit, verify)
    # （中文）把各维度的确认结果摊平，按严重度 high -> medium -> low 排序。
    confirmed = [{"dimension": r["dimension"], **f}
                 for r in results if r for f in r["confirmed"]]
    confirmed.sort(key=lambda f: {"high": 0, "medium": 1, "low": 2}.get(f["severity"], 3))
    ctx.log(f"confirmed {len(confirmed)} real finding(s)")
    return {"confirmed": confirmed}


# Saved workflow registry
# （中文）注册表是唯一的「可信代码」来源：模型只能按名字选择，
# 不能提交任意 Python 代码。
WORKFLOWS = {SAMPLE_META["name"]: (SAMPLE_META, sample_workflow)}

# （中文）暴露给模型的工具定义：workflow 名 + args + 可选的 resume runId。
WORKFLOW_TOOL = {
    "name": "Workflow",
    "description": "Run a saved workflow by name. Pass input in args.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "args": {"type": "object"},
            "resume_from_run_id": {"type": "string"},
        },
        "required": ["name"],
        "additionalProperties": False,
    },
}


def serialize_task(task):
    # （中文）把任务对象转成可 JSON 序列化的 dict（写快照、返回宿主都用它）。
    return {
        "taskId": task.task_id,
        "taskType": "local_workflow",
        "runId": task.run_id,
        "workflowName": task.meta["name"],
        "status": task.status,
        "usage": dict(task.usage),
        "progress": list(task.progress),
    }


async def run_workflow(name, args=None, resume_from_run_id=None):
    """Model-facing adapter: resolve trusted code from the host registry."""
    # （中文）模型侧适配器：先做输入校验，再从注册表解析出可信的
    # meta + 脚本，然后交给 WorkflowTool 执行。
    if not isinstance(name, str):
        raise WorkflowInputError("workflow name must be a string")
    if name not in WORKFLOWS:
        raise WorkflowInputError(f"unknown workflow '{name}'")
    if args is not None and not isinstance(args, dict):
        raise WorkflowInputError("workflow args must be an object")
    meta, script_fn = WORKFLOWS[name]
    out = await WorkflowTool().call(
        meta,
        script_fn,
        args=args,
        resume_from_run_id=resume_from_run_id,
    )
    return {
        "launched": out["launched"],
        "result": out["result"],
        "task": serialize_task(out["task"]),
    }


WORKFLOW_HANDLERS = {"Workflow": run_workflow}
# （中文）标明工具池继承自 s15 的集成 harness。
INHERITS_TOOLS_FROM = "s15"


def run_workflow_sync(**tool_input):
    """Bridge the synchronous host dispatcher to the async workflow runtime."""
    # （中文）s15 的工具分发是同步的，这里用 asyncio.run 起一个临时事件循环
    # 跑异步运行时；输入类错误转成文本返回给模型。
    try:
        return json.dumps(asyncio.run(run_workflow(**tool_input)), default=str)
    except WorkflowInputError as exc:
        return f"Error: {exc}"


def install_workflow_tool(host):
    """Extend the s15 host tool pool without changing its dispatch loop."""
    # （中文）不改 s15 的分发循环，只做两件事：
    #   1. 把 RUNNER_FACTORY 换成使用宿主 client/model 的真实 runner；
    #   2. 包住 host.assemble_tool_pool，在原工具池末尾追加 Workflow 工具。
    global RUNNER_FACTORY
    RUNNER_FACTORY = lambda: AnthropicAgentRunner(host.client, host.MODEL)
    if getattr(host, "_workflow_tool_installed", False):
        # （中文）幂等：重复安装直接返回。
        return
    base_assemble = host.assemble_tool_pool

    def assemble_with_workflow():
        # （中文）先取 s15 原有的工具和处理器，再追加 Workflow（避免重复追加）。
        tools, handlers = base_assemble()
        if not any(tool.get("name") == "Workflow" for tool in tools):
            tools.append(WORKFLOW_TOOL)
        handlers["Workflow"] = run_workflow_sync
        return tools, handlers

    host.assemble_tool_pool = assemble_with_workflow
    host._workflow_tool_installed = True


def load_integrated_host():
    """Load s15 lazily so deterministic workflow tests need no API key."""
    # （中文）按文件路径懒加载 s15 的 code.py，注册进 sys.modules 后执行。
    # 放在函数里而不是模块顶层，是为了让不涉及 API 的测试可以只导入本文件。
    path = Path(__file__).resolve().parents[1] / "s15_integrated_harness" / "code.py"
    spec = importlib.util.spec_from_file_location("integrated_host", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load integrated host from {path}")
    host = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = host
    spec.loader.exec_module(host)
    return host


# -- CLI --
async def run_demo(argv):
    # （中文）demo 模式跑一遍示例编排；`resume` 子命令会读取 last_run.txt
    # 并用 journal 缓存跳过没有变化的 agent() 调用。
    resume_id = None
    if argv and argv[0] == "resume":
        resume_id = _read_last_run()
        if not resume_id:
            print("nothing to resume; run `python code.py demo` first.")
            return
        print(f"resuming {resume_id}; unchanged agent() calls use the journal cache\n")
    else:
        print("launching workflow `review-changes`\n")

    # （中文）直接调用工具处理器，效果等同于模型发起一次 Workflow 工具调用。
    out = await WORKFLOW_HANDLERS["Workflow"](
        name="review-changes",
        args={"budget": None, "changes": DEMO_CHANGES},
        resume_from_run_id=resume_id,
    )

    # （中文）打印确认的 findings 和最终统计。
    print("\nresult:")
    for f in out["result"].get("confirmed", []):
        print(f"  [{f['severity']:<6}] {f['dimension']}: {f['title']}")
    task = out["task"]
    usage = task["usage"]
    print(f"\nstatus={task['status']}  agents={usage['agents']}  "
          f"tokens={usage['tokens']}  journal=.runtime/{task['runId']}.journal.jsonl")


PROMPT = "\033[36ms16 >> \033[0m"           # 普通输入提示符（青色）
# \001/\002 tell Readline the ANSI escapes have zero display width.
READLINE_PROMPT = "\001\033[36m\002s16 >> \001\033[0m\002"  # readline 版本：转义序列不占显示宽度


def run_cli():
    """Run the cumulative s15 host with Workflow added to its tool pool."""
    # （中文）交互模式：加载 s15 host、装上 Workflow 工具，然后把主循环
    # 交给 s15 的 agent_loop——本课只是在工具池里多了一个 Workflow。
    host = load_integrated_host()
    install_workflow_tool(host)
    host.CONSOLE.set_prompt(PROMPT, READLINE_PROMPT)
    host.CLI_ACTIVE = True
    host.start_runtime_services()
    print("s16: workflow runtime")
    print("Enter a question, press Enter to send. Type q to quit.\n")
    history = []
    context = host.update_context({}, history)
    session_state = {"active_user_request": "(no active user request)"}
    # （中文）守护线程运行 s15 的异步事件循环（cron 触发、队友事件等）。
    threading.Thread(
        target=host.async_event_loop,
        args=(history, context, session_state),
        daemon=True,
    ).start()
    while True:
        try:
            query = host.CONSOLE.ask()
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        # （中文）串行处理一轮用户输入：
        # hooks -> 追加消息 -> agent_loop -> 重新计算上下文 -> 打印回复。
        with host.agent_lock:
            host.trigger_hooks("UserPromptSubmit", query)
            turn_start = len(history)
            session_state["active_user_request"] = query
            history.append({"role": "user", "content": query})
            host.agent_loop(history, context, query)
            context = host.update_context(context, history)
            host.print_turn_assistants(history, turn_start)
        print()


if __name__ == "__main__":
    # （中文）带 demo / resume 参数进演示模式，否则进交互式 CLI。
    if sys.argv[1:] and sys.argv[1] in {"demo", "resume"}:
        asyncio.run(run_demo(sys.argv[1:]))
    else:
        run_cli()
