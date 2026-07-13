#!/usr/bin/env python3
"""
电销 Agent Demo — 基于 learn-claude-code 的 harness 工程模式

分层架构:
  1. 流程定义 → skills/loan-sales/SKILL.md   (知识,按需加载,不前置塞 prompt)
  2. 流程推进 → 模型自己判断                  (基于 skill 知识 + 对话上下文)
  3. 流程跟踪 → todo_write                   (阶段进度,实时更新)
  4. 流程约束 → Permission Hook               (字段未齐不准提交,高价需审批)

对应项目章节:
  s01 Agent Loop     — 循环始终不变
  s02 Tool Use       — 工具注册进 dispatch map
  s03 Permission     — submit_order 的硬门禁
  s04 Hooks          — PreToolUse 拦截
  s05 TodoWrite      — 流程阶段跟踪
  s07 Skill Loading  — 流程知识按需注入

Run: python sales_agent_demo/code.py
Needs: 根目录 .env 已配置 ANTHROPIC_API_KEY 和 MODEL_ID
"""

import ast, json, os
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

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

WORKDIR = Path(__file__).parent
SKILLS_DIR = WORKDIR / "skills"
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.getenv("MODEL_ID", "claude-sonnet-4-5-20250929")

# 会话状态
CURRENT_TODOS: list[dict] = []


# ═══════════════════════════════════════════════════════════
#  Skill Loading (s07) — 流程知识按需注入
# ═══════════════════════════════════════════════════════════

def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 SKILL.md 的 YAML frontmatter,返回 (meta, body)"""
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

SKILL_REGISTRY: dict[str, dict] = {}

def _scan_skills():
    """启动时扫描 skills/ 目录,只读 name + description(便宜),不读全文"""
    if not SKILLS_DIR.exists():
        return
    for d in sorted(SKILLS_DIR.iterdir()):
        if not d.is_dir():
            continue
        manifest = d / "SKILL.md"
        if manifest.exists():
            raw = manifest.read_text()
            meta, _ = _parse_frontmatter(raw)
            name = meta.get("name", d.name)
            desc = meta.get("description", raw.split("\n")[0].lstrip("#").strip())
            SKILL_REGISTRY[name] = {"name": name, "description": desc, "content": raw}

_scan_skills()

def list_skills() -> str:
    if not SKILL_REGISTRY:
        return "(暂无可用技能)"
    return "\n".join(f"- **{s['name']}**: {s['description']}" for s in SKILL_REGISTRY.values())

def load_skill(name: str) -> str:
    """运行时按需加载技能全文(贵),通过 tool_result 注入上下文"""
    skill = SKILL_REGISTRY.get(name)
    if not skill:
        return f"技能不存在: {name}"
    return skill["content"]


# ═══════════════════════════════════════════════════════════
#  SYSTEM prompt — 包含 skill 目录(便宜) + 电销角色
# ═══════════════════════════════════════════════════════════

def build_system() -> str:
    catalog = list_skills()
    return (
        "你是一名电销 Agent,负责联系客户、收集信息、帮助客户完成订单提交。\n\n"
        "工作原则:\n"
        "1. 进入具体业务流程前,先用 load_skill 加载对应的流程知识\n"
        "2. 用 todo_write 建立阶段清单,推进过程中实时更新状态\n"
        "3. 流程节奏由你把握——客户岔开话题先回应再拉回,不要机械念稿\n"
        "4. 客户明确拒绝不得强推;涉及敏感承诺必须用 skill 中的话术原文\n"
        "5. 提交订单前必须确认所有必填字段已收集齐,并向客户做最终确认\n\n"
        f"可用技能(按需 load_skill 展开全文):\n{catalog}\n"
    )

SYSTEM = build_system()


# ═══════════════════════════════════════════════════════════
#  Tools — 给 agent 的手 (s02)
# ═══════════════════════════════════════════════════════════

def run_query_crm(customer_phone: str) -> str:
    """查询 CRM 客户画像(模拟数据)"""
    db = {
        "13800000001": {
            "name": "张先生", "tag": "老客户",
            "last_contact": "2025-06-01", "product_interest": "贷款"
        },
        "13800000002": {
            "name": "李女士", "tag": "新线索",
            "last_contact": None, "product_interest": "理财"
        },
    }
    c = db.get(customer_phone)
    if c:
        return json.dumps(c, ensure_ascii=False)
    return f"未找到手机号 {customer_phone} 的客户记录(新客户)"

def run_update_crm(record: str) -> str:
    """写回跟进记录到 CRM(模拟)"""
    print(f"\033[90m[CRM] 已写回跟进记录: {record[:80]}...\033[0m")
    return f"已保存跟进记录({len(record)} 字)"

def run_submit_order(order_data: dict) -> str:
    """提交订单到业务系统(模拟)"""
    print(f"\033[32m[订单系统] 订单已提交:\n{json.dumps(order_data, ensure_ascii=False, indent=2)}\033[0m")
    order_id = f"ORD_{abs(hash(json.dumps(order_data, sort_keys=True)))%100000:05d}"
    return f"订单提交成功,订单号: {order_id}"

def run_transfer_human(reason: str) -> str:
    """转人工坐席"""
    return f"已转人工坐席,原因: {reason}。坐席将在 30 秒内接入。"

def run_check_credit(id_card: str) -> str:
    """查征信预评(模拟)"""
    return json.dumps({"id_card_tail": id_card[-4:], "score": 720, "level": "B"}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════
#  TodoWrite (s05) — 流程阶段跟踪
# ═══════════════════════════════════════════════════════════

def _normalize_todos(todos):
    if isinstance(todos, str):
        try:
            todos = json.loads(todos)
        except json.JSONDecodeError:
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

def run_todo_write(todos: list) -> str:
    global CURRENT_TODOS
    todos, error = _normalize_todos(todos)
    if error:
        return error
    CURRENT_TODOS = todos
    lines = ["\n\033[33m## 当前流程进度\033[0m"]
    icon = {"pending": "○", "in_progress": "▶", "completed": "✓"}
    for t in CURRENT_TODOS:
        color = {"pending": "\033[90m", "in_progress": "\033[36m", "completed": "\033[32m"}[t["status"]]
        lines.append(f"  {color}{icon[t['status']]} {t['content']}\033[0m")
    print("\n".join(lines))
    return f"已更新 {len(CURRENT_TODOS)} 个阶段状态"


# ═══════════════════════════════════════════════════════════
#  Tool Registry — dispatch map (s02)
# ═══════════════════════════════════════════════════════════

TOOLS = [
    {"name": "load_skill", "description": "加载某个业务流程的完整定义(流程阶段、必收字段、合规约束)。",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "todo_write", "description": "建立或更新当前业务的流程阶段清单。",
     "input_schema": {"type": "object", "properties": {"todos": {"type": "array", "items": {"type": "object", "properties": {"content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}, "required": ["content", "status"]}}}, "required": ["todos"]}},
    {"name": "query_crm", "description": "根据手机号查询客户画像、标签、历史接触记录。",
     "input_schema": {"type": "object", "properties": {"customer_phone": {"type": "string"}}, "required": ["customer_phone"]}},
    {"name": "update_crm", "description": "写回本次跟进记录到 CRM。",
     "input_schema": {"type": "object", "properties": {"record": {"type": "string"}}, "required": ["record"]}},
    {"name": "check_credit", "description": "根据身份证号查征信预评(分数与等级)。",
     "input_schema": {"type": "object", "properties": {"id_card": {"type": "string"}}, "required": ["id_card"]}},
    {"name": "submit_order", "description": "提交订单。前置条件:所有必填字段已收集齐并向客户确认。order_data 必须包含 name/id_card/phone/income/employer/social_security_years/product_id/amount。",
     "input_schema": {"type": "object", "properties": {"order_data": {"type": "object", "properties": {
        "name": {"type": "string"}, "id_card": {"type": "string"}, "phone": {"type": "string"},
        "income": {"type": "number"}, "employer": {"type": "string"},
        "social_security_years": {"type": "integer"}, "product_id": {"type": "string"},
        "amount": {"type": "number"},
     }}}, "required": ["order_data"]}},
    {"name": "transfer_human", "description": "转人工坐席。用于:客户投诉、超出权限、高客单价审批等。",
     "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
]

TOOL_HANDLERS = {
    "load_skill": load_skill,
    "todo_write": run_todo_write,
    "query_crm": run_query_crm,
    "update_crm": run_update_crm,
    "check_credit": run_check_credit,
    "submit_order": run_submit_order,
    "transfer_human": run_transfer_human,
}


# ═══════════════════════════════════════════════════════════
#  Hooks (s04) + Permission (s03) — 流程硬约束
# ═══════════════════════════════════════════════════════════

HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}

def register_hook(event: str, callback):
    HOOKS[event].append(callback)

def trigger_hooks(event: str, *args):
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None

# 核心约束:submit_order 的流程门禁
REQUIRED_FIELDS = ["name", "id_card", "phone", "income", "employer", "social_security_years", "product_id"]

def permission_hook(block):
    """PreToolUse: 拦截不合规的订单提交"""
    if block.name == "submit_order":
        order = block.input.get("order_data", {})
        # 硬性检查 1:必填字段齐全
        missing = [f for f in REQUIRED_FIELDS if not order.get(f)]
        if missing:
            print(f"\033[31m⛔ [Permission] 拦截 submit_order: 缺少字段 {missing}\033[0m")
            return f"Permission denied: 缺少必填字段 {missing},请先收集齐再提交。当前已收集: {list(order.keys())}"
        # 硬性检查 2:高客单价需人工审批
        amount = order.get("amount", 0)
        if amount > 100000:
            print(f"\033[31m⛔ [Permission] 拦截 submit_order: 金额 {amount} 超 10万,需人工审批\033[0m")
            return f"Permission denied: 订单金额 {amount} 超过 10万,需先调用 transfer_human 转人工审批"
    return None

def log_hook(block):
    print(f"\033[90m[HOOK] {block.name}\033[0m")
    return None

def summary_hook(messages: list):
    tool_count = sum(1 for m in messages
                     for b in (m.get("content") if isinstance(m.get("content"), list) else [])
                     if isinstance(b, dict) and b.get("type") == "tool_result")
    print(f"\033[90m[HOOK] 本次对话用了 {tool_count} 次工具调用\033[0m")
    return None

register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("Stop", summary_hook)


# ═══════════════════════════════════════════════════════════
#  Agent Loop (s01) — 循环始终不变
# ═══════════════════════════════════════════════════════════

def agent_loop(messages: list):
    while True:
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            trigger_hooks("Stop", messages)
            return

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            # 约束层:提交前拦截
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": str(blocked)})
                continue

            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"

            trigger_hooks("PostToolUse", block, output)
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": output})

        messages.append({"role": "user", "content": results})


# ═══════════════════════════════════════════════════════════
#  主程序 — 你扮演客户,agent 跟你对话
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  电销 Agent Demo")
    print("  你扮演客户,Agent 主动联系你推进贷款流程")
    print("  试试:告诉 Agent 你的手机号,或直接说需求")
    print("  输入 q 退出")
    print("=" * 60)

    # 模拟:坐席外呼接通,agent 先开口
    history = [{"role": "user",
                "content": "(场景:你是电销 Agent,刚刚拨通了客户的电话。客户已接听。请开始对话——先自报家门说明来意,然后根据客户回应推进流程。需要时用 load_skill 加载 loan-sales 流程。)"}

              ]

    # 第一轮让 agent 先开口
    agent_loop(history)
    for block in history[-1]["content"]:
        if getattr(block, "type", None) == "text":
            print(f"\n\033[36m[Agent]\033[0m {block.text}")

    # 后续轮次:用户输入(扮演客户)
    while True:
        try:
            user_input = input("\n\033[33m[你/客户]\033[0m ")
        except (EOFError, KeyboardInterrupt):
            break
        if user_input.strip().lower() in ("q", "exit", ""):
            break

        history.append({"role": "user", "content": user_input})
        agent_loop(history)
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(f"\n\033[36m[Agent]\033[0m {block.text}")
