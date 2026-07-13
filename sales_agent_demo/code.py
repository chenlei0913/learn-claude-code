#!/usr/bin/env python3
"""
车抵贷电销+IM Agent Demo — 基于 learn-claude-code 的 harness 工程模式

分层架构:
  1. 流程定义 → skills/loan-sales/SKILL.md   (知识,按需加载,不前置塞 prompt)
  2. 流程推进 → 模型自己判断                  (基于 skill 知识 + 对话上下文)
  3. 流程跟踪 → todo_write                   (阶段进度,实时更新)
  4. 流程约束 → Permission Hook               (字段未齐不准提交)
  5. 后台任务 → 加好友检测线程                (每5秒检测,不阻塞 agent loop)

对应项目章节:
  s01 Agent Loop     — 循环始终不变
  s02 Tool Use       — 工具注册进 dispatch map
  s03 Permission     — submit_order 的硬门禁
  s04 Hooks          — PreToolUse 拦截
  s05 TodoWrite      — 流程阶段跟踪
  s07 Skill Loading  — 流程知识按需注入
  s08 Background Tasks — 加好友后台检测线程

Run: python sales_agent_demo/code.py
Needs: 根目录 .env 已配置 ANTHROPIC_API_KEY 和 MODEL_ID
"""

import ast, json, os, threading, random, time
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
FRIEND_STATUS: dict = {"status": "pending", "request_count": 0}


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
#  SYSTEM prompt (s10) — 运行时按阶段组装,不硬编码
# ═══════════════════════════════════════════════════════════
#
# 电话阶段和 IM 阶段是两种媒介,风格不同。
# system prompt 根据 STAGE 动态拼接对应风格段,
# 加好友成功后 STAGE 从 phone 切到 im,system prompt 自动换风格。

STAGE = "phone"  # "phone" | "im"

PROMPT_SECTIONS = {
    "identity": "你是一名车抵贷电销 Agent。",

    "phone_style": (
        "## 对话风格(电话阶段,最高优先级)\n"
        "像真人打电话,不是念稿,也不是机器人问答。核心:短、自然、有人味。\n\n"
        "该做的:\n"
        "1. 一轮只问一件事。问完等客户答,不连发多个问题。\n"
        "2. 短句为主,允许带语气词(\"嗯\"\"哎\"\"嘞\"\"哈\"),让话听起来活。\n"
        "   - 冷:\"您车是全款还是按揭?\"\n"
        "   - 活:\"嗯,您那车是全款买的还是按揭的?\"\n"
        "3. 应答客户的话。客户说完先接一句再往下,不要无视。\n"
        "   - 客户\"有车\" → 先接\"好嘞\",再问\"那您这车是全款买的还是按揭的?\"\n"
        "4. 过渡自然。阶段切换用半句话带过,不正式宣告。\n"
        "   - ✅ \"那我加您个微信吧,后续微信上聊。\"\n"
        "   - ❌ \"现在我们进入第二阶段,我将为您发送微信好友请求。\"\n"
        "5. 偶尔寒暄一两句家常,但不超过一句,不展开。\n"
        "6. 回答简短。客户问利率 → \"看资质,加了微信我发您\"。\n\n"
        "不该做的:\n"
        "1. 不堆砌信息。自报家门只说\"X 公司的,做车抵贷,您有资金需求吗\",不介绍额度/利率/审批速度。\n"
        "2. 不预告流程。不要说\"接下来我会问几个问题\"\"流程分几步\"。\n"
        "3. 不复读客户答过的信息。\n"
        "4. 不用 emoji、不用 markdown 加粗。电话里没有这些。\n"
        "5. 不机械礼貌。不要每句都\"您好\"\"请问\"\"谢谢\",真实打电话不会这样。\n\n"
        "正例:\n"
        "  客户接听 → \"喂,您好,请问是张先生吗?\"\n"
        "  客户\"是我\" → \"哦您好,我是 X 公司的,这边做车抵贷。您现在说话方便吗?\"\n"
        "  客户\"方便\" → \"想了解下您最近有没有资金周转的需求?\"\n"
        "  客户\"有\" → \"好嘞,那问下您名下有车吗?\"\n"
        "  客户\"有\" → \"嗯,您那车是全款买的还是按揭的?\"\n"
        "  客户\"按揭的\" → \"哦,那还清了吗,绿本拿到了没?\"\n\n"
        "开场白流程(必须按顺序):\n"
        "1. 先确认身份:\"喂,您好,请问是 {客户姓名} 吗?\"(姓名从外呼系统已知)\n"
        "2. 客户确认 → 自报家门:\"我是 X 公司的,这边做车抵贷\"\n"
        "3. 简短寒暄一句:\"您现在说话方便吗?\"\n"
        "4. 客户表示方便 → 询问意向"
    ),

    "im_style": (
        "## 对话风格(IM 阶段,最高优先级)\n"
        "微信文字沟通,异步、可稍长、可分段。核心:清楚、礼貌、不催。\n\n"
        "该做的:\n"
        "1. 可以一条消息发 2-3 句,把请求说完整。\n"
        "   - ✅ \"您好,麻烦把行驶证正面拍个照发过来吧,我帮您看一下。\"\n"
        "2. 可以用 emoji 适度(😊 👍),偶尔用,不要每句都加。\n"
        "3. 可以用 markdown 加粗关键信息。\n"
        "4. 等待客户回复。IM 是异步的,客户可能不立即回,不要连发追问。\n"
        "5. 引导发图片要清楚:\"拍个照片发过来\"。\n"
        "6. 收集身份证时提醒\"仅用于本次申请,不会泄露\"。\n\n"
        "不该做的:\n"
        "1. 不用电话语气词(\"嗯\"\"哎\"\"嘞\"\"哈\"),IM 里显得轻浮。\n"
        "2. 不连发多条短消息,合并成一条。\n"
        "3. 不复读客户发的信息。客户发了行驶证,直接说审核结果,不要\"已收到您的照片\"。\n"
        "4. 不过度寒暄,\"您好\"\"谢谢\"足够,不要家常。\n"
        "5. 不预告流程。不要说\"接下来我会收集您的信息\"。\n\n"
        "正例:\n"
        "  \"您好,麻烦把**行驶证正面**拍个照发过来吧 😊\"\n"
        "  客户发图 → \"审核通过了,车没问题。接下来麻烦提供**姓名、身份证号和家庭住址**。\"\n"
        "  客户提供 → \"好的,信息齐了,**即将提交订单,请确认信息无误**。\""
    ),

    "work_principles": (
        "## 工作原则\n"
        "1. 进入具体业务流程前,先用 load_skill 加载对应的流程知识\n"
        "2. 用 todo_write 建立阶段清单,推进过程中实时更新状态\n"
        "3. 客户岔开话题 → 先回应,再自然拉回流程\n"
        "4. 客户明确拒绝不得强推;初筛不通过礼貌结束\n"
        "5. 提交订单前必须确认所有必填字段已收集齐,并向客户做最终确认\n"
        "6. 阶段切换(电话→IM)时,明确告知客户下一步在哪聊"
    ),
}

# stage → 加载哪些 section
STAGE_SECTIONS = {
    "phone": ["identity", "phone_style", "work_principles"],
    "im":    ["identity", "im_style",    "work_principles"],
}

def assemble_system_prompt(stage: str = "phone") -> str:
    """按阶段组装 system prompt (s10)。stage=phone 加载电话风格,stage=im 加载 IM 风格。"""
    sections = STAGE_SECTIONS.get(stage, STAGE_SECTIONS["phone"])
    parts = [PROMPT_SECTIONS[name] for name in sections]
    # 技能目录始终加载(便宜)
    parts.append(f"可用技能(按需 load_skill 展开全文):\n{list_skills()}")
    return "\n\n".join(parts)

# 向后兼容:CLI 入口仍可引用 SYSTEM(默认 phone)
SYSTEM = assemble_system_prompt("phone")


# ═══════════════════════════════════════════════════════════
#  Tools — 给 agent 的手 (s02)
# ═══════════════════════════════════════════════════════════

# 后台任务 (s08):加好友检测线程
def _friend_check_thread():
    """模拟客户在 5-12 秒后添加好友成功"""
    time.sleep(random.uniform(5, 12))
    FRIEND_STATUS["status"] = "added"

def run_send_friend_request() -> str:
    """发送添加微信好友请求,启动后台检测"""
    FRIEND_STATUS["request_count"] += 1
    count = FRIEND_STATUS["request_count"]
    if FRIEND_STATUS["status"] != "added":
        FRIEND_STATUS["status"] = "pending"
        t = threading.Thread(target=_friend_check_thread, daemon=True)
        t.start()
    print(f"\033[90m[加好友] 已发送请求(第{count}次),后台5秒检测中...\033[0m")
    return f"已发送添加好友请求(第{count}次)。后台正在每5秒检测一次是否添加成功。"

def run_check_friend_added() -> str:
    """检查客户是否已添加微信好友。已添加则自动切换 stage=im (s10 system prompt 重组)。"""
    global STAGE
    status = FRIEND_STATUS["status"]
    added = status == "added"
    if added and STAGE != "im":
        STAGE = "im"
        print(f"\033[35m[Stage] 检测到好友已添加,system prompt 切换到 IM 风格\033[0m")
    return json.dumps({"status": status, "added": added}, ensure_ascii=False)

def run_upload_driving_license(license_image: str = "") -> str:
    """上传行驶证照片进行车辆审核(模拟)"""
    # 模拟审核:默认通过。失败时返回 {"passed": false, "reason": "..."}
    result = {"passed": True, "reason": "行驶证审核通过,车辆符合办理条件"}
    print(f"\033[32m[审核] 行驶证审核通过\033[0m")
    return json.dumps(result, ensure_ascii=False)

def run_submit_order(order_data: dict) -> str:
    """提交订单到业务系统(模拟)"""
    print(f"\033[32m[订单系统] 订单已提交:\n{json.dumps(order_data, ensure_ascii=False, indent=2)}\033[0m")
    order_id = f"ORD_{abs(hash(json.dumps(order_data, sort_keys=True)))%100000:05d}"
    return f"订单提交成功,订单号: {order_id}"

def run_transfer_human(reason: str) -> str:
    """转人工坐席"""
    return f"已转人工坐席,原因: {reason}。坐席将在 30 秒内接入。"


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
    {"name": "send_friend_request", "description": "发送添加微信好友请求。调用后后台会每5秒检测一次是否添加成功。若客户没看到服务通知提示可重试,最多3次。",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "check_friend_added", "description": "检查客户是否已添加微信好友。返回 added=true/false 和当前状态。",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "upload_driving_license", "description": "上传行驶证照片进行车辆审核。返回 passed(是否通过)和 reason(失败原因,客户追问时告知)。",
     "input_schema": {"type": "object", "properties": {"license_image": {"type": "string", "description": "行驶证图片描述或标识"}}, "required": ["license_image"]}},
    {"name": "submit_order", "description": "提交订单。前置条件:行驶证审核通过且个人信息已收集齐。order_data 必须包含 name/id_card/address。",
     "input_schema": {"type": "object", "properties": {"order_data": {"type": "object", "properties": {
        "name": {"type": "string"}, "id_card": {"type": "string"}, "address": {"type": "string"},
     }}}, "required": ["order_data"]}},
    {"name": "transfer_human", "description": "转人工坐席。用于:客户投诉、超出权限、加好友3次未成功等。",
     "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
]

TOOL_HANDLERS = {
    "load_skill": load_skill,
    "todo_write": run_todo_write,
    "send_friend_request": run_send_friend_request,
    "check_friend_added": run_check_friend_added,
    "upload_driving_license": run_upload_driving_license,
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
REQUIRED_FIELDS = ["name", "id_card", "address"]

def permission_hook(block):
    """PreToolUse: 拦截不合规的订单提交"""
    if block.name == "submit_order":
        order = block.input.get("order_data", {})
        missing = [f for f in REQUIRED_FIELDS if not order.get(f)]
        if missing:
            print(f"\033[31m⛔ [Permission] 拦截 submit_order: 缺少字段 {missing}\033[0m")
            return f"Permission denied: 缺少必填字段 {missing},请先收集齐再提交。当前已收集: {list(order.keys())}"
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
        # 每轮按当前 STAGE 重组 system prompt (s10)
        system = assemble_system_prompt(STAGE)
        response = client.messages.create(
            model=MODEL, system=system, messages=messages,
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
    print("  车抵贷电销+IM Agent Demo")
    print("  你扮演客户,Agent 主动联系你推进车抵贷流程")
    print("  试试:告诉 Agent 你的车况,或直接说需求")
    print("  输入 q 退出")
    print("=" * 60)

    # 模拟:坐席外呼接通,agent 先开口
    history = [{"role": "user",
                "content": "(场景:你是车抵贷电销 Agent,刚刚拨通了客户的电话。客户已接听。请开始对话——先自报家门说明来意(公司做车抵贷,询问是否有资金需求),然后根据客户回应推进流程。需要时用 load_skill 加载 loan-sales 流程。)"}]

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
