#!/usr/bin/env python3
"""
通用 Agent Kernel — 与业务无关的 agent 运行时。

架构:
  ┌──────────────────────────────────────────────┐
  │  Agent Kernel (本文件,通用)                  │
  │  - agent_loop / tool dispatch                 │
  │  - permission / hooks                         │
  │  - handoff 协议                               │
  │  - Orchestrator(管理多 agent 切换)          │
  └──────────────────┬───────────────────────────┘
                     │ 动态装配
  ┌──────────────────▼───────────────────────────┐
  │  Skill (业务插件,自描述)                     │
  │  - SKILL.md (manifest: agents/tools/perm)    │
  │  - tools.py (工具实现)                       │
  │  - flows/*.md (流程文档)                     │
  └──────────────────────────────────────────────┘

设计原则:
  - Kernel 代码里不含任何业务词(车抵贷/行驶证/好友)
  - 加新业务 = 新建 skills/xxx/ 目录,Kernel 代码零改动
  - system prompt 由 Kernel 模板 + skill 注入动态装配

对应项目章节:
  s01 Agent Loop      — 每个 agent 各自一个循环
  s02 Tool Use        — 每个 agent 有独立工具集(从 skill 加载)
  s03 Permission      — 声明式权限(SKILL.md permissions 段)
  s04 Hooks           — PreToolUse/PostToolUse
  s05 TodoWrite       — 跨 agent 共享流程进度
  s06 Subagent        — handoff 传递结构化 payload
  s07 Skill Loading   — 流程知识按需注入
  s08 Background Tasks — 加好友检测线程(在 tools.py 里)

Run: python sales_agent_multi_demo/code.py
Needs: 根目录 .env 已配置 ANTHROPIC_API_KEY 和 MODEL_ID
"""

from __future__ import annotations
import os
from pathlib import Path
from typing import Optional

try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
except ImportError:
    pass

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# ═══════════════════════════════════════════════════════════
#  路径 & 客户端
# ═══════════════════════════════════════════════════════════

WORKDIR = Path(__file__).parent
SKILLS_DIR = WORKDIR / "skills"
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.getenv("MODEL_ID", "claude-sonnet-4-5-20250929")

# 默认 skill(可通过环境变量覆盖)
DEFAULT_SKILL = os.getenv("DEFAULT_SKILL", "loan-sales")


# ═══════════════════════════════════════════════════════════
#  Skill 加载 — 从 skills/loader.py 导入
# ═══════════════════════════════════════════════════════════

import sys
sys.path.insert(0, str(SKILLS_DIR))
from loader import (
    SkillLoader, Skill, ToolDef, register_builtins,
    load_skill_runtime, BUILTIN_TOOLS,
)


def get_skill(skill_name: str = DEFAULT_SKILL) -> Skill:
    """加载指定 skill,注册内置工具,返回 ready-to-use 的 Skill 对象"""
    skill = load_skill_runtime(SKILLS_DIR, skill_name)
    if not skill:
        raise RuntimeError(
            f"Skill '{skill_name}' not found in {SKILLS_DIR}. "
            f"Available: {SkillLoader(SKILLS_DIR).list_skills()}"
        )
    return skill


def list_skills() -> str:
    """列出所有可用 skill(给前端展示用)"""
    loader = SkillLoader(SKILLS_DIR)
    names = loader.list_skills()
    if not names:
        return "(暂无可用技能)"
    return "\n".join(f"- **{n}**" for n in names)


# ═══════════════════════════════════════════════════════════
#  Hooks (s04) — PreToolUse / PostToolUse / Stop
# ═══════════════════════════════════════════════════════════

HOOKS: dict[str, list] = {"PreToolUse": [], "PostToolUse": [], "Stop": []}


def register_hook(event: str, callback):
    HOOKS[event].append(callback)


def trigger_hooks(event: str, *args):
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None


# ═══════════════════════════════════════════════════════════
#  Agent Loop (s01) — 通用,从 skill 装配 system + tools
# ═══════════════════════════════════════════════════════════

def agent_loop(skill: Skill, agent_key: str, history: list, session: dict):
    """通用 agent loop — 用 skill 装配的 system + tools 跑循环。

    检测到 handoff_to_im 时:切换 active_agent,用 handoff payload 初始化
    下一个 agent 的 history,递归跑下一个 agent 的 loop。
    """
    system = skill.build_system_prompt(agent_key)
    tools_schema = [t.schema | {"name": t.name, "description": t.description}
                    for t in skill.get_tools_for_agent(agent_key)]

    while True:
        response = client.messages.create(
            model=MODEL, system=system, messages=history,
            tools=tools_schema, max_tokens=8000,
        )
        history.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            trigger_hooks("Stop", history)
            return

        results = []
        pending_handoff = None

        for block in response.content:
            if getattr(block, "type", None) != "tool_use":
                continue

            # 找到对应的 ToolDef
            tool_def: Optional[ToolDef] = skill.tools.get(block.name)
            if not tool_def:
                output = f"Unknown tool: {block.name}"
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": output})
                continue

            # PreToolUse hook
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": str(blocked)})
                continue

            # 执行工具(ToolDef.invoke 内部做权限 + 限流检查)
            events_buf = []
            output = tool_def.invoke(block.input, session, events_buf)
            trigger_hooks("PostToolUse", block, output)
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": output})

            # 检测 handoff
            if block.name == "handoff_to_im":
                pending_handoff = session.pop("_pending_handoff", None)

        history.append({"role": "user", "content": results})

        # handoff 处理:切换到下一个 agent
        if pending_handoff is not None:
            _do_handoff(skill, session, pending_handoff)
            return  # 当前 agent 循环结束,由调用方继续跑下一个 agent


def _do_handoff(skill: Skill, session: dict, handoff: dict):
    """执行 handoff:切换 active_agent,用 payload 初始化下一个 agent 的 history。

    注意:此函数只做状态切换,不主动跑下一个 agent 的 loop(由 Orchestrator 或
    web_agent_loop_stream 负责继续)。
    """
    payload = handoff.get("payload", {})
    summary = handoff.get("summary", "")

    # 找下一个 agent(简单策略:当前 agent 之后的第一个)
    agent_keys = skill.agent_names()
    current_idx = agent_keys.index(session["active_agent"])
    if current_idx + 1 >= len(agent_keys):
        return  # 没有下一个 agent 了

    next_agent = agent_keys[current_idx + 1]
    session["active_agent"] = next_agent
    session["handoff_payload"] = payload
    session["handoff_summary"] = summary

    # 用 handoff payload 初始化下一个 agent 的 history
    next_history_key = f"{next_agent}_history"
    session[next_history_key] = [{
        "role": "user",
        "content": skill.build_handoff_prompt(session["active_agent"], payload) +
                   (f"\n\n补充说明: {summary}" if summary else ""),
    }]


# ═══════════════════════════════════════════════════════════
#  Orchestrator — 管理多 Agent 的切换(通用版)
# ═══════════════════════════════════════════════════════════

class Orchestrator:
    """通用多 Agent 协调器:管理 active agent、session state、handoff。

    与业务无关:所有业务知识从 skill 加载。
    """

    def __init__(self, skill: Skill, customer_name: str = "", customer_phone: str = ""):
        self.skill = skill
        self.session: dict = {
            "customer_name": customer_name,
            "customer_phone": customer_phone,
            "active_agent": skill.agent_names()[0] if skill.agent_names() else "phone",
            "phone_history": [],
            "im_history": [],
            "phone_todos": [],
            "im_todos": [],
            "todos": [],
            "friend_status": "pending",
            "friend_request_count": 0,
            "order_submitted": False,
            "order_id_final": "",
            "order_data_final": {},
            "transferred": False,
            "transfer_reason_final": "",
            "handoff_payload": {},
            "handoff_summary": "",
            "_skill": skill,
            "_skill_loader": SkillLoader(SKILLS_DIR),
        }

        # 初始化第一个 agent 的 history
        first_agent = self.session["active_agent"]
        first_history_key = f"{first_agent}_history"
        self.session[first_history_key] = [{
            "role": "user",
            "content": self._build_initial_prompt(customer_name, customer_phone),
        }]

    def _build_initial_prompt(self, customer_name: str, customer_phone: str) -> str:
        """构建第一个 agent 的初始 prompt"""
        name_part = f"客户姓名: {customer_name}" if customer_name else "客户姓名: 未知"
        phone_part = f"客户手机号: {customer_phone}" if customer_phone else ""
        return (
            f"(场景:你是 {self.skill.manifest.agents[self.session['active_agent']].name},"
            f"刚刚接通了客户的联系。外呼系统信息:{name_part}"
            f"{'，' + phone_part if phone_part else ''}。\n"
            "请开始对话。第一轮调 load_skill 加载流程定义 + todo_write 建立流程清单,"
            "第二轮才开始对客户说话。)"
        )

    def start(self) -> str:
        """启动第一个 agent,让它先开口"""
        first_agent = self.session["active_agent"]
        history = self.session[f"{first_agent}_history"]
        agent_loop(self.skill, first_agent, history, self.session)
        return self._extract_text(history[-1])

    def process_user_message(self, message: str) -> str:
        """处理用户消息,路由到当前活跃的 agent"""
        active = self.session["active_agent"]
        history_key = f"{active}_history"
        self.session[history_key].append({"role": "user", "content": message})
        agent_loop(self.skill, active, self.session[history_key], self.session)
        return self._extract_text(self.session[history_key][-1])

    @staticmethod
    def _extract_text(message: dict) -> str:
        content = message.get("content")
        if isinstance(content, list):
            texts = [b.text for b in content if hasattr(b, "text")]
            return "\n".join(texts)
        return str(content) if content else ""


# ═══════════════════════════════════════════════════════════
#  主程序 — CLI 测试入口(你扮演客户)
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  通用 Agent Kernel — 多 Agent Demo")
    print(f"  Skill: {DEFAULT_SKILL}")
    print(f"  模型:  {MODEL}")
    print(f"  可用 Skill: {list_skills()}")
    print("  你扮演客户,与 Agent 对话推进流程")
    print("  输入 q 退出")
    print("=" * 60)

    skill = get_skill(DEFAULT_SKILL)
    orchestrator = Orchestrator(skill, customer_name="张先生")

    # 第一个 agent 先开口
    reply = orchestrator.start()
    agent_name = skill.get_agent(orchestrator.session["active_agent"]).name
    print(f"\n\033[36m[{agent_name}]\033[0m {reply}")

    # 后续轮次:用户输入(扮演客户)
    while True:
        active = orchestrator.session["active_agent"]
        agent_name = skill.get_agent(active).name if skill.get_agent(active) else active
        try:
            user_input = input(f"\n\033[33m[你/客户]\033[0m ")
        except (EOFError, KeyboardInterrupt):
            break
        if user_input.strip().lower() in ("q", "exit", ""):
            break

        reply = orchestrator.process_user_message(user_input)
        # handoff 后 active_agent 可能已切换,用最新的 label
        active = orchestrator.session["active_agent"]
        agent_name = skill.get_agent(active).name if skill.get_agent(active) else active
        print(f"\n\033[36m[{agent_name}]\033[0m {reply}")
