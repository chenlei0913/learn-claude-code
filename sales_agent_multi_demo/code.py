#!/usr/bin/env python3
"""
车抵贷电销+IM 多 Agent Demo — 两个独立 Agent 通过 handoff 协作

架构:
  ┌──────────────┐      handoff_to_im      ┌──────────────┐
  │  PhoneAgent  │ ───────────────────────▶ │   IMAgent    │
  │  独立 system  │   传递:客户信息+初筛结果  │  独立 system  │
  │  独立 tools   │                          │  独立 tools   │
  │  独立 history │                          │  独立 history │
  └──────────────┘                          └──────────────┘
         ▲                                         ▲
         │              Orchestrator               │
         │  shared_state + active_agent + dispatch │
         └─────────────────────────────────────────┘

与单 Agent 版对比:
  单 Agent: 一个 history,STAGE 切换 system prompt(s10)
  多 Agent: 两个 history,handoff 工具显式传递上下文(s06 思路)

对应项目章节:
  s01 Agent Loop      — 每个 agent 各自一个循环
  s02 Tool Use        — 每个 agent 有独立工具集
  s03 Permission      — submit_order 的硬门禁
  s04 Hooks           — PreToolUse 拦截
  s05 TodoWrite       — 跨 agent 共享流程进度
  s06 Subagent        — handoff 传递上下文(思路)
  s07 Skill Loading   — 流程知识按需注入
  s08 Background Tasks — 加好友检测线程

Run: python sales_agent_multi_demo/code.py
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


# ═══════════════════════════════════════════════════════════
#  Skill Loading (s07) — 流程知识按需注入
# ═══════════════════════════════════════════════════════════

def _parse_frontmatter(text: str) -> tuple[dict, str]:
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
    if not SKILLS_DIR.exists():
        return
    for d in sorted(SKILLS_DIR.iterdir()):
        if not d.is_dir():
            continue
        manifest = d / "SKILL.md"
        if manifest.exists():
            raw = manifest.read_text(encoding="utf-8")
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
    skill = SKILL_REGISTRY.get(name)
    if not skill:
        return f"技能不存在: {name}"
    return skill["content"]


# ═══════════════════════════════════════════════════════════
#  共享状态 — Orchestrator 管理,两个 Agent 都能读写
# ═══════════════════════════════════════════════════════════

SHARED_STATE = {
    "customer_name": "",
    "customer_phone": "",
    "screening_passed": False,      # 初筛是否通过
    "friend_added": False,          # 是否已加好友
    "collected_fields": {},         # IM 阶段收集的字段
}

CURRENT_TODOS: list[dict] = []      # 跨 agent 共享的流程进度
FRIEND_STATUS: dict = {"status": "pending", "request_count": 0}


# ═══════════════════════════════════════════════════════════
#  后台任务 (s08) — 加好友检测线程
# ═══════════════════════════════════════════════════════════

def _friend_check_thread():
    time.sleep(random.uniform(5, 12))
    FRIEND_STATUS["status"] = "added"
    SHARED_STATE["friend_added"] = True


# ═══════════════════════════════════════════════════════════
#  TodoWrite (s05) — 跨 agent 共享的流程进度
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
#  PhoneAgent 工具实现
# ═══════════════════════════════════════════════════════════

def run_send_friend_request() -> str:
    FRIEND_STATUS["request_count"] += 1
    count = FRIEND_STATUS["request_count"]
    if FRIEND_STATUS["status"] != "added":
        FRIEND_STATUS["status"] = "pending"
        t = threading.Thread(target=_friend_check_thread, daemon=True)
        t.start()
    print(f"\033[90m[加好友] 已发送请求(第{count}次),后台5秒检测中...\033[0m")
    return f"已发送添加好友请求(第{count}次)。后台正在每5秒检测一次是否添加成功。"

def run_check_friend_added() -> str:
    status = FRIEND_STATUS["status"]
    added = status == "added"
    if added:
        SHARED_STATE["friend_added"] = True
    return json.dumps({"status": status, "added": added}, ensure_ascii=False)

def run_transfer_human(reason: str) -> str:
    return f"已转人工坐席,原因: {reason}。坐席将在 30 秒内接入。"


# ═══════════════════════════════════════════════════════════
#  IMAgent 工具实现
# ═══════════════════════════════════════════════════════════

def run_upload_driving_license(license_image: str = "") -> str:
    result = {"passed": True, "reason": "行驶证审核通过,车辆符合办理条件"}
    print(f"\033[32m[审核] 行驶证审核通过\033[0m")
    return json.dumps(result, ensure_ascii=False)

def run_submit_order(order_data: dict) -> str:
    print(f"\033[32m[订单系统] 订单已提交:\n{json.dumps(order_data, ensure_ascii=False, indent=2)}\033[0m")
    order_id = f"ORD_{abs(hash(json.dumps(order_data, sort_keys=True)))%100000:05d}"
    return f"订单提交成功,订单号: {order_id}"


# ═══════════════════════════════════════════════════════════
#  handoff_to_im — PhoneAgent → IMAgent 的切换工具
# ═══════════════════════════════════════════════════════════

# Orchestrator 会在运行时注入 handoff 回调
_handoff_callback = None

def set_handoff_callback(cb):
    global _handoff_callback
    _handoff_callback = cb

def run_handoff_to_im(customer_summary: str) -> str:
    """PhoneAgent 调用此工具触发切换到 IMAgent。
    customer_summary 是 PhoneAgent 对电话阶段收集信息的总结。"""
    if _handoff_callback:
        _handoff_callback(customer_summary)
    return f"已切换到 IMAgent。电话阶段总结已传递: {customer_summary[:100]}..."


# ═══════════════════════════════════════════════════════════
#  PhoneAgent — 独立 system + tools + history
# ═══════════════════════════════════════════════════════════

PHONE_SYSTEM = (
    "你是 PhoneAgent,一名车抵贷电销外呼 Agent,负责电话阶段的全部工作。\n\n"
    "语言要求:全程使用中文,包括 thinking 思考过程也用中文。\n\n"
    "## 你的职责\n"
    "1. 电话外呼:身份确认 → 意向询问 → 车况初筛(有车/全款or按揭/绿本)\n"
    "2. 初筛通过 → 加微信好友(send_friend_request + check_friend_added)\n"
    "3. 加好友成功 → 调用 handoff_to_im 将客户信息传递给 IMAgent,你的工作结束\n"
    "4. 初筛不通过或客户拒绝 → 礼貌结束\n"
    "5. 加好友3次未成功 → 转人工\n\n"
    "## 对话风格(电话阶段)\n"
    "像真人打电话,不是念稿。核心:短、自然、有人味。\n\n"
    "该做的:\n"
    "1. 一轮只问一件事。问完等客户答,不连发多个问题。\n"
    "2. 短句为主,允许带语气词(\"嗯\"\"哎\"\"嘞\"\"哈\"),让话听起来活。\n"
    "3. 应答客户的话。客户说完先接一句再往下。\n"
    "4. 过渡自然。阶段切换用半句话带过,不正式宣告。\n"
    "5. 偶尔寒暄一两句家常,但不超过一句。\n"
    "6. 回答简短。客户问利率 → \"看资质,加了微信我发您\"。\n\n"
    "不该做的:\n"
    "1. 不堆砌信息。自报家门只说\"X 公司的,做车抵贷\"。\n"
    "2. 不预告流程。不要说\"接下来我会问几个问题\"。\n"
    "3. 不复读客户答过的信息。\n"
    "4. 不用 emoji、不用 markdown 加粗。电话里没有这些。\n"
    "5. 不机械礼貌。不要每句都\"您好\"\"请问\"\"谢谢\"。\n"
    "6. 不输出内心独白/流程描述。你的 text 就是**说给客户听的话**。"
    "禁止输出\"现在等待客户回应\"\"接下来我要询问意向\"\"我先确认身份\""
    "等任何描述你计划/动作的句子——这些是思考,不是对话。"
    "text 里只允许出现你当面跟客户说的话。\n\n"
    "## 输出节奏(核心原则)\n"
    "每一轮优先只做一件事:要么调工具,要么对客户说话。\n"
    "- 需要准备(load_skill) → 只调工具,不输出 text\n"
    "- 阶段切换时更新进度(todo_write) → 可以和对客户说的话同轮:先调 todo_write,再输出 text\n"
    "- 正常对话 → 只输出 text,不调工具,说完即停等客户回应\n"
    "如果同一轮已输出 text 并调用了 todo_write,收到 tool_result 后不要再说话,直接结束。\n\n"
    "开场前先做准备:第一轮调 load_skill + todo_write(创建流程清单),第二轮才说开场白。\n\n"
    "## Todo 更新规则(必须执行)\n"
    "不要只在开场创建一次清单!整个对话中每次阶段变化都必须调用 todo_write 更新:\n"
    "- 客户确认身份后 → 标记\"身份确认\"为 completed,标记\"意向询问\"为 in_progress\n"
    "- 客户表达意向后 → 标记\"意向询问\"为 completed,标记\"车况初筛\"为 in_progress\n"
    "- 初筛通过后 → 标记\"车况初筛\"为 completed,标记\"加微信好友\"为 in_progress\n"
    "- 好友添加成功后 → 标记\"加微信好友\"为 completed,标记\"handoff到IM\"为 in_progress\n"
    "- 调用 handoff_to_im 后 → 标记\"handoff到IM\"为 completed\n"
    "每次调用 todo_write 都要传入完整清单(所有阶段,含已完成和未完成)。\n"
    "阶段切换时,优先在同轮先调 todo_write 再输出 text,这样客户能看到进度实时更新。\n"
    "关键:开场轮(load_skill + todo_write 创建清单)之后,下一轮发开场白问客户身份时,必须同轮先调 todo_write 把\"身份确认\"标记为 in_progress,再输出开场白文字。不能只发文字不更新 todo。\n\n"
    "开场白流程(必须按顺序):\n"
    "1. 先确认身份:\"喂,您好,请问是 {客户姓名} 吗?\"\n"
    "2. 客户确认 → 自报家门:\"我是 X 公司的,这边做车抵贷\"\n"
    "3. 寒暄一句:\"您现在说话方便吗?\"\n"
    "4. 客户表示方便 → 询问意向\n\n"
    "正例:\n"
    "  客户接听 → \"喂,您好,请问是张先生吗?\"\n"
    "  客户\"是我\" → \"哦您好,我是 X 公司的,这边做车抵贷。您现在说话方便吗?\"\n"
    "  客户\"方便\" → \"想了解下您最近有没有资金周转的需求?\"\n"
    "  客户\"有\" → \"好嘞,那问下您名下有车吗?\"\n\n"
    "## 加好友前必须先确认手机号(重要)\n"
    "在调用 send_friend_request 之前,必须先跟客户确认手机号尾号:\n"
    "1. 从外呼系统已知客户手机号,取后4位,问客户:\"我加您尾号 {后4位} 这个手机号哈?\"\n"
    "2. 客户确认正确 → 调用 send_friend_request(不传 phone 参数,用原号)\n"
    "3. 客户说不是这个号/给了新号 → 调用 send_friend_request 时传 phone 参数为新号\n"
    "   例:客户说\"我换号了,现在是13800001234\" → send_friend_request(phone=\"13800001234\")\n"
    "4. 没确认手机号前,禁止调用 send_friend_request\n"
    "确认手机号和发送好友请求不能同轮:先问尾号,等客户回应,再调 send_friend_request。\n\n"
    "## 超时处理(电话阶段专属,后端自动调度)\n"
    "电话场景下客户可能没听见或信号不好。后端调度线程会自动监控客户回复,10秒没回复就给你发系统通知,你不需要调任何工具来管理超时。\n\n"
    "收到超时通知时(以\"[系统:\"开头的是系统通知,不是客户说的话):\n"
    "- \"[系统: 客户10秒未回复,第1次]\" 或 \"[系统: 客户10秒未回复,第2次]\" → 像真人那样询问:\"喂?您还在吗?能听到我说话吗?\"\n"
    "- \"[系统: 客户已3次未回复,建议结束通话]\" → 礼貌结束:\"抱歉,可能信号不太好,稍后我再联系您,再见\"\n"
    "超时询问规则:\n"
    "1. 只说一句询问的话,不解释为什么问,不推进业务流程\n"
    "2. 不更新 todo(超时不算业务阶段变化)\n"
    "3. 第3次超时说再见后,不再说任何话,不再调任何工具\n"
    "4. 客户在超时后回复了,正常接话继续业务流程\n"
    "5. 加好友成功 handoff 到 IM 后,超时监控自动停止\n\n"
    "## handoff 触发条件(最高优先级,必须执行)\n"
    "当 check_friend_added 返回 added=true 后,你**必须**在**同一轮**做两件事:\n"
    "1. 输出 text 告诉客户:\"好的,加上了,后续咱们从微信上聊\"\n"
    "2. 同一轮**立即调用** handoff_to_im 工具,customer_summary 参数写清楚:客户姓名、车况、初筛结果\n"
    "禁止只输出 text 而不调用 handoff_to_im!禁止把 handoff 推到下一轮!\n"
    "如果你说了\"加上了\"\"后续微信聊\"之类的话,这一轮必须同时调用 handoff_to_im,否则流程会卡住。\n"
    "调用 handoff_to_im 后你的工作结束,后续由 IMAgent 接管。\n\n"
    f"可用技能(按需 load_skill 展开全文):\n{list_skills()}"
)

PHONE_TOOLS = [
    {"name": "load_skill", "description": "加载某个业务流程的完整定义。",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "todo_write", "description": "建立或更新当前业务的流程阶段清单。",
     "input_schema": {"type": "object", "properties": {"todos": {"type": "array", "items": {"type": "object", "properties": {"content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}, "required": ["content", "status"]}}}, "required": ["todos"]}},
    {"name": "send_friend_request", "description": "发送添加微信好友请求。后台每5秒检测是否添加成功。最多3次。如果客户改了手机号,传 phone 参数更新。",
     "input_schema": {"type": "object", "properties": {"phone": {"type": "string", "description": "客户微信绑定的手机号(仅当客户更正了号码时传入)"}}}},
    {"name": "check_friend_added", "description": "检查客户是否已添加微信好友。返回 added=true/false。",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "handoff_to_im", "description": "切换到 IMAgent。当好友添加成功后调用此工具,将电话阶段收集的客户信息传递给 IMAgent。customer_summary 写清:客户姓名、车况、初筛结果。",
     "input_schema": {"type": "object", "properties": {"customer_summary": {"type": "string", "description": "电话阶段收集的客户信息总结,传递给 IMAgent"}}, "required": ["customer_summary"]}},
    {"name": "transfer_human", "description": "转人工坐席。用于:客户投诉、加好友3次未成功等。",
     "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
]

PHONE_HANDLERS = {
    "load_skill": load_skill,
    "todo_write": run_todo_write,
    "send_friend_request": run_send_friend_request,
    "check_friend_added": run_check_friend_added,
    "handoff_to_im": run_handoff_to_im,
    "transfer_human": run_transfer_human,
}


# ═══════════════════════════════════════════════════════════
#  IMAgent — 独立 system + tools + history
# ═══════════════════════════════════════════════════════════

IM_SYSTEM = (
    "你是 IMAgent,一名车抵贷 IM 沟通 Agent,负责微信阶段的全部工作。\n\n"
    "语言要求:全程使用中文,包括 thinking 思考过程也用中文。\n\n"
    "## 你的职责\n"
    "1. 收集行驶证照片 → 调用 upload_driving_license 审核\n"
    "2. 审核失败 → 告知客户\"这辆车办不了\",客户追问则告知失败原因\n"
    "3. 审核通过 → 收集:姓名、身份证号、家庭住址\n"
    "4. 收集完成 → 调用 submit_order 提交订单\n"
    "5. 提交成功 → 告知客户后续会有专人联系\n\n"
    "## 对话风格(IM 阶段)\n"
    "微信文字沟通,异步、可稍长、可分段。核心:清楚、礼貌、不催。\n\n"
    "该做的:\n"
    "1. 可以一条消息发 2-3 句,把请求说完整。\n"
    "2. 可以用 emoji 适度(😊 👍),偶尔用,不要每句都加。\n"
    "3. 可以用 markdown 加粗关键信息。\n"
    "4. 等待客户回复。IM 是异步的,不要连发追问。\n"
    "5. 引导发图片要清楚:\"拍个照片发过来\"。\n"
    "6. 收集身份证时提醒\"仅用于本次申请,不会泄露\"。\n\n"
    "不该做的:\n"
    "1. 不用电话语气词(\"嗯\"\"哎\"\"嘞\"\"哈\"),IM 里显得轻浮。\n"
    "2. 不连发多条短消息,合并成一条。\n"
    "3. 不复读客户发的信息。客户发了行驶证,直接说审核结果。\n"
    "4. 不过度寒暄,\"您好\"\"谢谢\"足够,不要家常。\n"
    "5. 不预告流程。\n"
    "6. 不输出内心独白/流程描述。你的 text 就是**发给客户的消息**。"
    "禁止输出\"现在等待客户回复\"\"接下来我要收集资料\"等任何描述你计划/动作的句子——"
    "这些是思考,不是对话。text 里只允许出现你发给客户的话。\n\n"
    "## 输出节奏(核心原则)\n"
    "每一轮优先只做一件事:要么调工具,要么给客户发消息。\n"
    "- 需要准备(load_skill / upload_driving_license 审核) → 只调工具,不发 text\n"
    "- 阶段切换时更新进度(todo_write) → 可以和给客户的消息同轮:先调 todo_write,再输出 text\n"
    "- 正常对话 → 只发 text,不调工具,发完即停等客户回复\n"
    "如果同一轮已输出 text 并调用了 todo_write,收到 tool_result 后不要再说话,直接结束。\n\n"
    "开场前先做准备:第一轮调 load_skill + todo_write(创建流程清单),第二轮才发第一条消息。\n\n"
    "## Todo 更新规则(必须执行)\n"
    "不要只在开场创建一次清单!整个对话中每次阶段变化都必须调用 todo_write 更新:\n"
    "- 开始收集行驶证(向客户要照片的那一轮) → 标记\"收集行驶证\"为 in_progress\n"
    "- 行驶证审核完成 → 标记\"收集行驶证\"为 completed,标记\"车辆审核\"为 completed\n"
    "- 开始收集客户信息(向客户要姓名身份证的那一轮) → 标记\"收集客户信息\"为 in_progress\n"
    "- 信息收集完成 → 标记\"收集客户信息\"为 completed,标记\"提交订单\"为 in_progress\n"
    "- 订单提交成功 → 标记\"提交订单\"为 completed\n"
    "每次调用 todo_write 都要传入完整清单(所有阶段,含已完成和未完成)。\n"
    "阶段切换时,优先在同轮先调 todo_write 再输出 text,这样客户能看到进度实时更新。\n"
    "关键:开场轮(load_skill + todo_write 创建清单)之后,下一轮向客户要行驶证时,必须同轮先调 todo_write 把\"收集行驶证\"标记为 in_progress,再输出要照片的文字。不能只发文字不更新 todo。\n\n"
    "正例:\n"
    "  \"您好,麻烦把**行驶证正面**拍个照发过来吧 😊\"\n"
    "  客户发图 → \"审核通过了,车没问题。接下来麻烦提供**姓名、身份证号和家庭住址**。\"\n\n"
    "## 重要:你会收到 PhoneAgent 传来的 handoff summary\n"
    "你的第一条 user message 会包含电话阶段收集的客户信息(姓名、车况、初筛结果)。\n"
    "请基于这些信息继续沟通,不要重新问电话阶段已经问过的问题。\n\n"
    f"可用技能(按需 load_skill 展开全文):\n{list_skills()}"
)

IM_TOOLS = [
    {"name": "load_skill", "description": "加载某个业务流程的完整定义。",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "todo_write", "description": "建立或更新当前业务的流程阶段清单。",
     "input_schema": {"type": "object", "properties": {"todos": {"type": "array", "items": {"type": "object", "properties": {"content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}, "required": ["content", "status"]}}}, "required": ["todos"]}},
    {"name": "upload_driving_license", "description": "上传行驶证照片进行车辆审核。返回 passed(是否通过)和 reason(失败原因)。",
     "input_schema": {"type": "object", "properties": {"license_image": {"type": "string"}}, "required": ["license_image"]}},
    {"name": "submit_order", "description": "提交订单。order_data 必须包含 name/id_card/address。",
     "input_schema": {"type": "object", "properties": {"order_data": {"type": "object", "properties": {
        "name": {"type": "string"}, "id_card": {"type": "string"}, "address": {"type": "string"},
     }}}, "required": ["order_data"]}},
    {"name": "transfer_human", "description": "转人工坐席。",
     "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
]

IM_HANDLERS = {
    "load_skill": load_skill,
    "todo_write": run_todo_write,
    "upload_driving_license": run_upload_driving_license,
    "submit_order": run_submit_order,
    "transfer_human": run_transfer_human,
}


# ═══════════════════════════════════════════════════════════
#  Hooks (s04) + Permission (s03)
# ═══════════════════════════════════════════════════════════

HOOKS = {"PreToolUse": [], "PostToolUse": [], "Stop": []}

def register_hook(event: str, callback):
    HOOKS[event].append(callback)

def trigger_hooks(event: str, *args):
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None

REQUIRED_FIELDS = ["name", "id_card", "address"]

def permission_hook(block):
    if block.name == "submit_order":
        order = block.input.get("order_data", {})
        missing = [f for f in REQUIRED_FIELDS if not order.get(f)]
        if missing:
            print(f"\033[31m⛔ [Permission] 拦截 submit_order: 缺少字段 {missing}\033[0m")
            return f"Permission denied: 缺少必填字段 {missing},请先收集齐再提交。"
    return None

def log_hook(block):
    print(f"\033[90m[HOOK] {block.name}\033[0m")
    return None

register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)


# ═══════════════════════════════════════════════════════════
#  Agent Loop (s01) — 每个 agent 各自一个循环
# ═══════════════════════════════════════════════════════════

def phone_agent_loop(messages: list):
    """PhoneAgent 的 agent loop — 用 PHONE_SYSTEM + PHONE_TOOLS"""
    while True:
        response = client.messages.create(
            model=MODEL, system=PHONE_SYSTEM, messages=messages,
            tools=PHONE_TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            trigger_hooks("Stop", messages)
            return

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": str(blocked)})
                continue
            handler = PHONE_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"
            trigger_hooks("PostToolUse", block, output)
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": output})
        messages.append({"role": "user", "content": results})

        # handoff_to_im 被调用后,PhoneAgent 循环结束
        if any(b.type == "tool_use" and b.name == "handoff_to_im"
               for b in response.content if hasattr(b, "type")):
            print(f"\033[35m[Handoff] PhoneAgent → IMAgent,PhoneAgent 循环结束\033[0m")
            return


def im_agent_loop(messages: list):
    """IMAgent 的 agent loop — 用 IM_SYSTEM + IM_TOOLS"""
    while True:
        response = client.messages.create(
            model=MODEL, system=IM_SYSTEM, messages=messages,
            tools=IM_TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            trigger_hooks("Stop", messages)
            return

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": str(blocked)})
                continue
            handler = IM_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"
            trigger_hooks("PostToolUse", block, output)
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": output})
        messages.append({"role": "user", "content": results})


# ═══════════════════════════════════════════════════════════
#  Orchestrator — 管理两个 Agent 的切换
# ═══════════════════════════════════════════════════════════

class Orchestrator:
    """多 Agent 协调器:管理 active agent、shared state、handoff"""

    def __init__(self, customer_name: str = "", customer_phone: str = ""):
        SHARED_STATE["customer_name"] = customer_name
        SHARED_STATE["customer_phone"] = customer_phone

        self.active_agent = "phone"          # "phone" | "im"
        self.phone_history: list = []
        self.im_history: list = []

        # 注册 handoff 回调
        set_handoff_callback(self._do_handoff)

        # 初始化 PhoneAgent 的 history
        name_part = f"客户姓名: {customer_name}" if customer_name else "客户姓名: 未知"
        phone_part = f"客户手机号: {customer_phone}" if customer_phone else ""
        self.phone_history.append({"role": "user", "content": (
            f"(场景:你是 PhoneAgent,刚刚拨通了客户的电话,客户已接听。\n"
            f"外呼系统信息:{name_part}{'，' + phone_part if phone_part else ''}。\n"
            "请开始对话——按开场白流程:先确认身份(\"请问是 {客户姓名} 吗\"),"
            "客户确认后自报家门(公司做车抵贷),寒暄一句,再询问是否有资金需求。\n"
            "根据客户回应推进流程。需要时用 load_skill 加载 loan-sales 流程。)"
        )})

    def _do_handoff(self, customer_summary: str):
        """PhoneAgent → IMAgent 的切换:用 handoff summary 初始化 IMAgent history"""
        self.active_agent = "im"
        print(f"\033[35m[Orchestrator] 切换到 IMAgent\033[0m")
        print(f"\033[35m[Orchestrator] handoff summary: {customer_summary[:200]}\033[0m")

        # 用 handoff summary 初始化 IMAgent 的 history
        self.im_history = [{"role": "user", "content": (
            f"(场景:你是 IMAgent,PhoneAgent 已完成电话阶段的初筛和加好友,现在切换到微信沟通。\n"
            f"PhoneAgent 传来的客户信息总结:\n{customer_summary}\n\n"
            "请基于以上信息,开始与客户在微信上沟通。第一步是收集行驶证照片。\n"
            "需要时用 load_skill 加载 loan-sales 流程。)"
        )}]
        # 立即让 IMAgent 开口
        im_agent_loop(self.im_history)

    def process_user_message(self, message: str) -> str:
        """处理用户消息,路由到当前活跃的 agent"""
        if self.active_agent == "phone":
            self.phone_history.append({"role": "user", "content": message})
            phone_agent_loop(self.phone_history)
            return self._extract_text(self.phone_history[-1])
        else:
            self.im_history.append({"role": "user", "content": message})
            im_agent_loop(self.im_history)
            return self._extract_text(self.im_history[-1])

    def start(self) -> str:
        """启动 PhoneAgent,让它先开口"""
        phone_agent_loop(self.phone_history)
        return self._extract_text(self.phone_history[-1])

    @staticmethod
    def _extract_text(message: dict) -> str:
        content = message.get("content")
        if isinstance(content, list):
            texts = [b.text for b in content if hasattr(b, "text")]
            return "\n".join(texts)
        return str(content) if content else ""


# ═══════════════════════════════════════════════════════════
#  主程序 — 你扮演客户,两个 Agent 跟你对话
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  车抵贷电销+IM 多 Agent Demo")
    print("  PhoneAgent: 电话初筛 + 加好友")
    print("  IMAgent:    IM收资料 + 提交订单")
    print("  你扮演客户,与 Agent 对话推进流程")
    print("  输入 q 退出")
    print("=" * 60)

    orchestrator = Orchestrator(customer_name="张先生")

    # PhoneAgent 先开口
    reply = orchestrator.start()
    print(f"\n\033[36m[PhoneAgent]\033[0m {reply}")

    # 后续轮次:用户输入(扮演客户)
    while True:
        agent_label = "[PhoneAgent]" if orchestrator.active_agent == "phone" else "[IMAgent]"
        try:
            user_input = input(f"\n\033[33m[你/客户]\033[0m ")
        except (EOFError, KeyboardInterrupt):
            break
        if user_input.strip().lower() in ("q", "exit", ""):
            break

        reply = orchestrator.process_user_message(user_input)
        # handoff 后 active_agent 可能已切换,用最新的 label
        agent_label = "[PhoneAgent]" if orchestrator.active_agent == "phone" else "[IMAgent]"
        print(f"\n\033[36m{agent_label}\033[0m {reply}")
