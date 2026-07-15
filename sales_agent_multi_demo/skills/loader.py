#!/usr/bin/env python3
"""
Skill 加载器 — 把业务知识从代码中剥离,改成声明式装配。

核心三件套:
  @tool          装饰器:声明工具的 schema + 限流 + 行为
  SkillManifest  数据类:SKILL.md frontmatter 解析结果
  SkillLoader    加载器:扫描 skills/ 目录,按需装配 Agent Kernel

设计原则:
  - Kernel(代码层)不知道任何业务词(车抵贷/行驶证/好友)
  - 所有业务知识(流程/风格/工具/权限/handoff协议)都在 SKILL.md + tools.py
  - 加新业务 = 新建 skills/xxx/ 目录,Kernel 代码零改动
"""

from __future__ import annotations
import ast
import importlib.util
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

try:
    import yaml
except ImportError:
    yaml = None


# ═══════════════════════════════════════════════════════════
#  @tool 装饰器 — 声明工具的元信息
# ═══════════════════════════════════════════════════════════

@dataclass
class ToolDef:
    """工具定义:schema + 实现 + 可选的限流/权限"""
    name: str
    description: str
    schema: dict
    handler: Callable[..., Any]
    # 可选:调用次数上限(超过触发 on_exceed)
    max_calls: Optional[int] = None
    on_exceed: Optional[str] = None  # 如 "transfer_human"
    # 可选:PreToolUse 权限检查(返回字符串则拦截)
    permission: Optional[Callable[[dict, dict], Optional[str]]] = None

    def invoke(self, input_data: dict, session: dict, events: list) -> str:
        """统一入口:权限检查 → 限流检查 → 执行 handler"""
        # 权限检查
        if self.permission:
            blocked = self.permission(input_data, session)
            if blocked:
                return blocked

        # 限流检查
        if self.max_calls is not None:
            key = f"_tool_call_count_{self.name}"
            count = session.get(key, 0)
            if count >= self.max_calls:
                if self.on_exceed:
                    return (f"已达到最大调用次数 {self.max_calls}。"
                            f"触发后续动作: {self.on_exceed}")
                return f"已达到最大调用次数 {self.max_calls}"

        # 执行
        output = self.handler(input_data, session, events)
        return output


def tool(
    name: str,
    description: str,
    schema: dict,
    max_calls: Optional[int] = None,
    on_exceed: Optional[str] = None,
    permission: Optional[Callable[[dict, dict], Optional[str]]] = None,
):
    """装饰器:把函数注册成 ToolDef。

    handler 签名统一为 (input_data: dict, session: dict, events: list) -> str
    """
    def decorator(func: Callable[..., Any]) -> ToolDef:
        return ToolDef(
            name=name,
            description=description,
            schema=schema,
            handler=func,
            max_calls=max_calls,
            on_exceed=on_exceed,
            permission=permission,
        )
    return decorator


# ═══════════════════════════════════════════════════════════
#  SkillManifest — SKILL.md frontmatter 的结构化解析
# ═══════════════════════════════════════════════════════════

@dataclass
class AgentDef:
    """单个 agent 的声明"""
    key: str                        # "phone" / "im"
    name: str                       # "PhoneAgent"
    responsibility: str             # 职责描述
    tools: list[str]                # 工具名列表
    style_ref: str                  # 指向 style 段的 key
    flow_ref: str                   # 指向 flow 文件的 key


@dataclass
class PermissionDef:
    """权限声明"""
    require_fields: list[str] = field(default_factory=list)
    max_calls: Optional[int] = None
    on_exceed: Optional[str] = None


@dataclass
class SkillManifest:
    """SKILL.md 的 frontmatter 解析结果"""
    name: str
    version: str
    description: str
    agents: dict[str, AgentDef]
    tools_module: str               # 相对路径,如 "./tools.py"
    handoff_schema: dict            # 结构化字段定义
    permissions: dict[str, PermissionDef]
    states: list[str]
    # 正文(markdown):对话风格等软知识
    body: str = ""


# ═══════════════════════════════════════════════════════════
#  Skill — 加载完成的完整 skill 对象
# ═══════════════════════════════════════════════════════════

@dataclass
class Skill:
    """加载完成的 skill:manifest + tools + flows"""
    manifest: SkillManifest
    tools: dict[str, ToolDef]       # name → ToolDef
    flows: dict[str, str]           # flow_ref → markdown 内容
    style: str                      # SKILL.md 正文(对话风格等)

    def agent_names(self) -> list[str]:
        return list(self.manifest.agents.keys())

    def get_agent(self, key: str) -> Optional[AgentDef]:
        return self.manifest.agents.get(key)

    def get_tools_for_agent(self, agent_key: str) -> list[ToolDef]:
        """返回该 agent 可见的工具列表(按 manifest 顺序)"""
        agent = self.get_agent(agent_key)
        if not agent:
            return []
        return [self.tools[n] for n in agent.tools if n in self.tools]

    def build_system_prompt(self, agent_key: str) -> str:
        """为指定 agent 装配 system prompt(Kernel 模板 + skill 注入)"""
        agent = self.get_agent(agent_key)
        if not agent:
            raise ValueError(f"Agent '{agent_key}' not found in skill '{self.manifest.name}'")

        flow_content = self.flows.get(agent.flow_ref, "")
        tools_desc = "\n".join(
            f"- **{t.name}**: {t.description}" for t in self.get_tools_for_agent(agent_key)
        )

        return KERNEL_SYSTEM_TEMPLATE.format(
            skill_name=self.manifest.name,
            skill_description=self.manifest.description,
            agent_name=agent.name,
            agent_key=agent_key,
            agent_responsibility=agent.responsibility,
            tools=tools_desc,
            style_content=self.style,
            flow_content=flow_content,
        )

    def build_handoff_prompt(self, from_agent: str, payload: dict) -> str:
        """构造 handoff 时注入给下一个 agent 的首条 user message"""
        # 按 handoff_schema 格式化
        lines = []
        for field_name, field_def in self.manifest.handoff_schema.items():
            value = payload.get(field_name, "")
            if value:
                lines.append(f"- {field_name}: {value}")
        payload_str = "\n".join(lines) if lines else json.dumps(payload, ensure_ascii=False, indent=2)

        return (
            f"(场景:你已从 {from_agent} 切换过来,接收到以下结构化客户信息:\n"
            f"{payload_str}\n\n"
            "请基于以上信息继续推进业务流程。不要重复询问已提供的信息。\n"
            "需要时用 load_skill 加载流程定义。)"
        )


# ═══════════════════════════════════════════════════════════
#  Kernel System Prompt 模板 — 通用,不含任何业务词
# ═══════════════════════════════════════════════════════════

KERNEL_SYSTEM_TEMPLATE = """你是 {agent_name},负责 {agent_responsibility}。

语言要求:全程使用中文,包括 thinking 思考过程也用中文。

## 当前业务
- 业务名称: {skill_name}
- 业务说明: {skill_description}
- 你的角色: {agent_name} (agent_key: {agent_key})

## 工作方式(通用 Agent 行为)
1. 会话开始:第一轮调 load_skill 加载业务流程定义,用 todo_write 建立流程清单
2. 第二轮才开始对客户说话(基于流程的第一步)
3. 按 flow_content 中的流程推进,每次阶段变化调 todo_write 更新清单
4. 需要外部能力时调用下面声明的工具
5. 工具调用后若已输出 text,收到 tool_result 后不要再说话,直接结束本轮

## 输出节奏(核心原则)
每一轮优先只做一件事:要么调工具,要么对客户说话。
- 需要准备(load_skill) → 只调工具,不输出 text
- 阶段切换时更新进度(todo_write) → 可以和对客户说的话同轮:先调 todo_write,再输出 text
- 正常对话 → 只输出 text,不调工具,说完即停等客户回应

## text 输出规范
你的 text 输出就是**直接说给客户/发给客户的话**,会原样显示给客户。
禁止输出任何自我陈述、内心独白、流程描述、计划说明(如"现在等待客户回应"
"接下来我要询问意向"等)。这些思考放到 thinking 块里。

## Todo 更新规则
不要只在开场创建一次清单!整个对话中每次阶段变化都必须调用 todo_write 更新:
- 每次阶段切换 → 标记上一阶段 completed,新阶段 in_progress
- 每次调用 todo_write 都要传入完整清单(所有阶段,含已完成和未完成)
- 阶段切换时,优先在同轮先调 todo_write 再输出 text
- 开场轮(load_skill + todo_write)之后,下一轮开始业务对话时,必须同轮先调 todo_write
  把当前阶段标记为 in_progress,再输出对客户的话

## 可用工具
{tools}

## 对话风格
{style_content}

## 业务流程
{flow_content}
"""


# ═══════════════════════════════════════════════════════════
#  SkillLoader — 扫描 + 解析 + 加载
# ═══════════════════════════════════════════════════════════

class SkillLoader:
    """扫描 skills/ 目录,加载所有 skill"""

    def __init__(self, skills_dir: Path):
        self.skills_dir = skills_dir
        self._cache: dict[str, Skill] = {}

    def _parse_frontmatter(self, text: str) -> tuple[dict, str]:
        """解析 markdown frontmatter(--- 之间的 yaml)"""
        if not text.startswith("---"):
            return {}, text
        parts = text.split("---", 2)
        if len(parts) < 3:
            return {}, text
        if yaml:
            try:
                meta = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                meta = {}
        else:
            meta = {}
        return meta, parts[2].strip()

    def _load_tools_module(self, skill_dir: Path, module_path: str) -> dict[str, ToolDef]:
        """动态加载 tools.py,收集所有 ToolDef"""
        tools_py = skill_dir / module_path.lstrip("./")
        if not tools_py.exists():
            return {}

        spec = importlib.util.spec_from_file_location(
            f"skill_tools_{skill_dir.name}", tools_py
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        tools = {}
        for attr_name in dir(mod):
            attr = getattr(mod, attr_name)
            if isinstance(attr, ToolDef):
                tools[attr.name] = attr
        return tools

    def _load_flows(self, skill_dir: Path, manifest: SkillManifest) -> dict[str, str]:
        """加载 flows/ 目录下的 markdown 流程文件"""
        flows_dir = skill_dir / "flows"
        flows = {}
        if not flows_dir.exists():
            return flows

        for agent_key, agent_def in manifest.agents.items():
            # flow_ref 形如 "phone_flow" → 找 flows/phone.md
            flow_file = flows_dir / f"{agent_def.flow_ref}.md"
            if flow_file.exists():
                flows[agent_def.flow_ref] = flow_file.read_text(encoding="utf-8").strip()
            else:
                # 兼容:直接用 agent_key
                flow_file = flows_dir / f"{agent_key}.md"
                if flow_file.exists():
                    flows[agent_def.flow_ref] = flow_file.read_text(encoding="utf-8").strip()

        return flows

    def load(self, skill_name: str) -> Optional[Skill]:
        """加载指定 skill"""
        if skill_name in self._cache:
            return self._cache[skill_name]

        skill_dir = self.skills_dir / skill_name
        manifest_path = skill_dir / "SKILL.md"
        if not manifest_path.exists():
            return None

        raw = manifest_path.read_text(encoding="utf-8")
        meta, body = self._parse_frontmatter(raw)

        # 解析 agents
        agents = {}
        for key, agent_meta in (meta.get("agents") or {}).items():
            agents[key] = AgentDef(
                key=key,
                name=agent_meta.get("name", key),
                responsibility=agent_meta.get("responsibility", ""),
                tools=agent_meta.get("tools", []),
                style_ref=agent_meta.get("style_ref", f"{key}_style"),
                flow_ref=agent_meta.get("flow_ref", f"{key}_flow"),
            )

        # 解析 permissions
        permissions = {}
        for tool_name, perm_meta in (meta.get("permissions") or {}).items():
            permissions[tool_name] = PermissionDef(
                require_fields=perm_meta.get("require_fields", []),
                max_calls=perm_meta.get("max_calls"),
                on_exceed=perm_meta.get("on_exceed"),
            )

        manifest = SkillManifest(
            name=meta.get("name", skill_name),
            version=meta.get("version", "1.0"),
            description=meta.get("description", ""),
            agents=agents,
            tools_module=meta.get("tools_module", "./tools.py"),
            handoff_schema=meta.get("handoff_schema") or {},
            permissions=permissions,
            states=meta.get("states") or [],
            body=body,
        )

        # 加载 tools.py
        tools = self._load_tools_module(skill_dir, manifest.tools_module)

        # 把 manifest 的 permissions 注入到对应 ToolDef
        for tool_name, perm in permissions.items():
            if tool_name in tools:
                td = tools[tool_name]
                if perm.max_calls is not None and td.max_calls is None:
                    td.max_calls = perm.max_calls
                if perm.on_exceed and not td.on_exceed:
                    td.on_exceed = perm.on_exceed
                # require_fields 的权限检查
                if perm.require_fields:
                    required = list(perm.require_fields)
                    tools[tool_name] = ToolDef(
                        name=td.name, description=td.description, schema=td.schema,
                        handler=td.handler, max_calls=td.max_calls, on_exceed=td.on_exceed,
                        permission=_make_field_validator(required),
                    )

        # 加载 flows
        flows = self._load_flows(skill_dir, manifest)

        skill = Skill(
            manifest=manifest,
            tools=tools,
            flows=flows,
            style=body,
        )
        self._cache[skill_name] = skill
        return skill

    def list_skills(self) -> list[str]:
        """列出所有可用 skill 名"""
        if not self.skills_dir.exists():
            return []
        names = []
        for d in sorted(self.skills_dir.iterdir()):
            if d.is_dir() and (d / "SKILL.md").exists():
                names.append(d.name)
        return names


def _make_field_validator(required_fields: list[str]) -> Callable[[dict, dict], Optional[str]]:
    """生成字段校验权限函数(用于 submit_order 等)"""
    def validator(input_data: dict, session: dict) -> Optional[str]:
        # 支持嵌套:submit_order 的字段在 order_data 里
        data = input_data.get("order_data", input_data)
        missing = [f for f in required_fields if not data.get(f)]
        if missing:
            return (f"Permission denied: 缺少必填字段 {missing},"
                    f"请先收集齐再提交。当前已收集: {list(data.keys())}")
        return None
    return validator


# ═══════════════════════════════════════════════════════════
#  内置工具:load_skill / todo_write / handoff_to_im / transfer_human
#  这些是 Kernel 级工具,所有 skill 共用
# ═══════════════════════════════════════════════════════════

import json as _json
import threading
import random
import time


def _normalize_todos(todos) -> tuple[Optional[list], Optional[str]]:
    """规范化 todos 输入(支持 list / json str / python str)"""
    if isinstance(todos, str):
        try:
            todos = _json.loads(todos)
        except _json.JSONDecodeError:
            try:
                todos = ast.literal_eval(todos)
            except (SyntaxError, ValueError):
                return None, "Error: todos 必须是列表或 JSON 数组"
    if not isinstance(todos, list):
        return None, "Error: todos 必须是列表"
    for i, t in enumerate(todos):
        if not isinstance(t, dict):
            return None, f"Error: todos[{i}] 必须是对象"
        if "content" not in t or "status" not in t:
            return None, f"Error: todos[{i}] 缺少 'content' 或 'status'"
        if t["status"] not in ("pending", "in_progress", "completed"):
            return None, f"Error: todos[{i}] 状态非法 '{t['status']}'"
    return todos, None


# load_skill 工具:加载指定 skill 的流程定义(返回 SKILL.md 正文)
@tool(
    name="load_skill",
    description="加载某个业务流程的完整定义,返回流程文档供你参考。",
    schema={
        "type": "object",
        "properties": {"name": {"type": "string", "description": "技能名,如 loan-sales"}},
        "required": ["name"],
    },
)
def _builtin_load_skill(input_data: dict, session: dict, events: list) -> str:
    loader: SkillLoader = session["_skill_loader"]
    skill_name = input_data.get("name", "")
    skill = loader.load(skill_name)
    if not skill:
        return f"技能不存在: {skill_name}"

    # 返回 SKILL.md 正文 + 所有 flow 内容
    parts = [skill.style]
    for flow_ref, flow_content in skill.flows.items():
        parts.append(f"\n\n---\n# 流程: {flow_ref}\n{flow_content}")
    content = "\n".join(parts)

    events.append({
        "type": "tool_detail",
        "name": "load_skill",
        "detail": f"加载技能: {skill_name}",
        "output_preview": content[:300],
    })
    return content


# todo_write 工具:更新流程进度
@tool(
    name="todo_write",
    description="建立或更新当前业务的流程阶段清单。每次阶段变化都要调用。",
    schema={
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                    },
                    "required": ["content", "status"],
                },
            },
        },
        "required": ["todos"],
    },
)
def _builtin_todo_write(input_data: dict, session: dict, events: list) -> str:
    todos, error = _normalize_todos(input_data.get("todos", []))
    if error:
        return error

    agent = session["active_agent"]
    tagged = [dict(t, agent=agent) for t in todos]

    if agent == "phone":
        session["phone_todos"] = tagged
    else:
        session["im_todos"] = tagged

    session["todos"] = session.get("phone_todos", []) + session.get("im_todos", [])
    events.append({"type": "todos_updated", "todos": session["todos"]})
    return f"已更新 {len(todos)} 个阶段状态"


# handoff_to_im 工具:切换到下一个 agent
@tool(
    name="handoff_to_im",
    description="切换到下一个 Agent。payload 必须按 handoff_schema 填写结构化字段。",
    schema={
        "type": "object",
        "properties": {
            "payload": {
                "type": "object",
                "description": "结构化 handoff 数据,字段见 system prompt 中的 handoff_schema",
            },
            "summary": {
                "type": "string",
                "description": "自由文本总结(可选,补充结构化字段无法表达的信息)",
            },
        },
        "required": ["payload"],
    },
)
def _builtin_handoff_to_im(input_data: dict, session: dict, events: list) -> str:
    payload = input_data.get("payload", {})
    summary = input_data.get("summary", "")

    # 校验必填字段(从 skill 的 handoff_schema)
    skill: Skill = session["_skill"]
    missing = []
    for field_name, field_def in skill.manifest.handoff_schema.items():
        is_required = (
            (isinstance(field_def, dict) and field_def.get("required"))
            or field_def is True
        )
        if is_required and not payload.get(field_name):
            missing.append(field_name)

    if missing:
        return (f"handoff 校验失败: 缺少必填字段 {missing}。"
                f"请在 payload 中补齐: {list(skill.manifest.handoff_schema.keys())}")

    # 标记 phone_todos 完成
    for t in session.get("phone_todos", []):
        content = t.get("content", "")
        if any(k in content for k in ["加微信", "加好友", "好友", "handoff", "切换", "移交", "到IM"]):
            t["status"] = "completed"
    session["todos"] = session.get("phone_todos", []) + session.get("im_todos", [])
    events.append({"type": "todos_updated", "todos": session["todos"]})
    events.append({
        "type": "tool_detail",
        "name": "handoff_to_im",
        "detail": "handoff 触发,即将切换 agent",
        "output_preview": _json.dumps(payload, ensure_ascii=True)[:200],
    })

    # 标记 session 待 handoff(真正的切换由 web_agent_loop_stream 检测并执行)
    session["_pending_handoff"] = {"payload": payload, "summary": summary}
    return f"已切换到下一个 Agent。结构化数据已传递: {_json.dumps(payload, ensure_ascii=False)[:100]}"


# transfer_human 工具:转人工
@tool(
    name="transfer_human",
    description="转人工坐席。用于:客户投诉、客户要求人工、工具调用达上限等。",
    schema={
        "type": "object",
        "properties": {"reason": {"type": "string"}},
        "required": ["reason"],
    },
)
def _builtin_transfer_human(input_data: dict, session: dict, events: list) -> str:
    reason = input_data.get("reason", "")
    session["transferred"] = True
    session["transfer_reason_final"] = reason
    events.append({"type": "transfer_human", "reason": reason})
    return f"已转人工坐席,原因: {reason}。坐席将在 30 秒内接入。"


# ═══════════════════════════════════════════════════════════
#  注册内置工具到 skill(如果 skill 没有自定义同名工具)
# ═══════════════════════════════════════════════════════════

BUILTIN_TOOLS: list[ToolDef] = [
    _builtin_load_skill,
    _builtin_todo_write,
    _builtin_handoff_to_im,
    _builtin_transfer_human,
]

def register_builtins(skill: Skill):
    """把内置工具注册到 skill(不覆盖 skill 自定义的同名工具)"""
    for td in BUILTIN_TOOLS:
        if td.name not in skill.tools:
            skill.tools[td.name] = td


# ═══════════════════════════════════════════════════════════
#  便捷入口
# ═══════════════════════════════════════════════════════════

def load_skill_runtime(skills_dir: Path, skill_name: str) -> Optional[Skill]:
    """加载 skill 并注册内置工具,返回 ready-to-use 的 Skill 对象"""
    loader = SkillLoader(skills_dir)
    skill = loader.load(skill_name)
    if skill:
        register_builtins(skill)
    return skill
