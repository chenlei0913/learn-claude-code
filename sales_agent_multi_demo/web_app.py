#!/usr/bin/env python3
"""
车抵贷电销+IM 多 Agent Demo — Web 测试界面
两个独立 Agent 通过 handoff_to_im 协作:
  PhoneAgent(电话阶段:初筛+加好友) → IMAgent(IM 阶段:收资料+提交订单)

Run: python sales_agent_multi_demo/web_app.py
Needs: 根目录 .env 已配置 ANTHROPIC_API_KEY 和 MODEL_ID
"""

import json, os, sys, uuid, importlib.util, threading, random, time
from pathlib import Path

# 先用 importlib 加载 code.py,再清理 sys.path 避免与标准库 code 模块冲突
_script_dir = str(Path(__file__).parent)
_code_path = Path(__file__).parent / "code.py"
_spec = importlib.util.spec_from_file_location("sales_agent_multi_code", _code_path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
# 移除脚本目录,防止后续 import code 命中本地 code.py
while _script_dir in sys.path:
    sys.path.remove(_script_dir)

from flask import Flask, request, jsonify, render_template

# ═══════════════════════════════════════════════════════════
#  从 code.py 导入常量和函数
# ═══════════════════════════════════════════════════════════

PHONE_SYSTEM = _mod.PHONE_SYSTEM
PHONE_TOOLS = _mod.PHONE_TOOLS
PHONE_HANDLERS = _mod.PHONE_HANDLERS
IM_SYSTEM = _mod.IM_SYSTEM
IM_TOOLS = _mod.IM_TOOLS
IM_HANDLERS = _mod.IM_HANDLERS

load_skill = _mod.load_skill
list_skills = _mod.list_skills
run_todo_write = _mod.run_todo_write
_normalize_todos = _mod._normalize_todos
run_send_friend_request = _mod.run_send_friend_request
run_check_friend_added = _mod.run_check_friend_added
run_handoff_to_im = _mod.run_handoff_to_im
run_upload_driving_license = _mod.run_upload_driving_license
run_submit_order = _mod.run_submit_order
run_transfer_human = _mod.run_transfer_human

REQUIRED_FIELDS = _mod.REQUIRED_FIELDS
SHARED_STATE = _mod.SHARED_STATE
FRIEND_STATUS = _mod.FRIEND_STATUS
CURRENT_TODOS = _mod.CURRENT_TODOS

client = _mod.client
MODEL = _mod.MODEL
set_handoff_callback = _mod.set_handoff_callback
SKILL_REGISTRY = _mod.SKILL_REGISTRY

app = Flask(__name__)


# ═══════════════════════════════════════════════════════════
#  Session 管理 — 每个会话独立状态
# ═══════════════════════════════════════════════════════════

SESSIONS: dict[str, dict] = {}


def build_initial_prompt(customer_name: str = "", customer_phone: str = "") -> str:
    """构建 PhoneAgent 的初始 prompt(外呼系统已知客户姓名+手机号)"""
    name_part = f"客户姓名: {customer_name}" if customer_name else "客户姓名: 未知"
    phone_part = f"客户手机号: {customer_phone}" if customer_phone else ""
    return (
        f"(场景:你是 PhoneAgent,刚刚拨通了客户的电话,客户已接听。\n"
        f"外呼系统信息:{name_part}{'，' + phone_part if phone_part else ''}。\n"
        "请开始对话——按开场白流程:先确认身份(\"请问是 {客户姓名} 吗\"),"
        "客户确认后自报家门(公司做车抵贷),寒暄一句,再询问是否有资金需求。\n"
        "根据客户回应推进流程。需要时用 load_skill 加载 loan-sales 流程。)"
    )


def get_or_create_session(session_id: str | None = None):
    if session_id and session_id in SESSIONS:
        return session_id, SESSIONS[session_id]
    session_id = session_id or uuid.uuid4().hex[:8]
    SESSIONS[session_id] = {
        "phone_history": [],         # PhoneAgent 的独立 history
        "im_history": [],            # IMAgent 的独立 history
        "active_agent": "phone",     # "phone" | "im"
        "todos": [],
        "started": False,
        "friend_status": "pending",
        "friend_request_count": 0,
        "customer_name": "",
        "customer_phone": "",
        "handoff_summary": "",       # PhoneAgent 传给 IMAgent 的信息
    }
    return session_id, SESSIONS[session_id]


# ═══════════════════════════════════════════════════════════
#  后台任务 (s08) — 加好友检测,5-12秒后 added
# ═══════════════════════════════════════════════════════════

def _start_friend_check_thread(session: dict):
    """启动后台线程:模拟客户在 5-12 秒后添加好友成功"""
    def _check():
        time.sleep(random.uniform(5, 12))
        session["friend_status"] = "added"
    t = threading.Thread(target=_check, daemon=True)
    t.start()


# ═══════════════════════════════════════════════════════════
#  Web 版工具执行 — 基于会话状态,收集事件
# ═══════════════════════════════════════════════════════════

def execute_tool(name: str, input_data: dict, session: dict, events: list) -> str:
    """执行单个工具,返回 output 文本。事件追加到 events 列表。"""
    if name == "load_skill":
        skill_name = input_data.get("name", "")
        output = load_skill(skill_name)
        events.append({
            "type": "tool_detail",
            "name": name,
            "detail": f"加载技能: {skill_name}",
            "output_preview": output[:300],
        })

    elif name == "todo_write":
        normalized, error = _normalize_todos(input_data.get("todos", []))
        if error:
            output = error
        else:
            session["todos"] = [dict(t) for t in normalized]
            events.append({"type": "todos_updated", "todos": session["todos"]})
            output = f"已更新 {len(normalized)} 个阶段状态"

    elif name == "send_friend_request":
        session["friend_request_count"] += 1
        count = session["friend_request_count"]
        if session["friend_status"] != "added":
            session["friend_status"] = "pending"
            _start_friend_check_thread(session)
        output = f"已发送添加好友请求(第 {count} 次)。后台正在每5秒检测一次是否添加成功。"
        events.append({
            "type": "tool_detail",
            "name": name,
            "detail": f"发送好友请求(第{count}次),后台检测已启动",
            "output_preview": output,
        })

    elif name == "check_friend_added":
        status = session.get("friend_status", "pending")
        added = status == "added"
        output = json.dumps({"status": status, "added": added}, ensure_ascii=False)
        events.append({
            "type": "tool_detail",
            "name": name,
            "detail": f"好友状态: {status}",
            "output_preview": output,
        })

    elif name == "handoff_to_im":
        customer_summary = input_data.get("customer_summary", "")
        output = f"已切换到 IMAgent。电话阶段总结已传递: {customer_summary[:100]}..."
        events.append({
            "type": "tool_detail",
            "name": name,
            "detail": "handoff_to_im 触发,即将切换到 IMAgent",
            "output_preview": output,
        })

    elif name == "upload_driving_license":
        # 模拟审核:默认通过
        result = {"passed": True, "reason": "行驶证审核通过,车辆符合办理条件"}
        output = json.dumps(result, ensure_ascii=False)
        events.append({
            "type": "tool_detail",
            "name": name,
            "detail": "行驶证审核:通过",
            "output_preview": output,
        })

    elif name == "submit_order":
        order_data = input_data.get("order_data", {})
        order_id = f"ORD_{abs(hash(json.dumps(order_data, sort_keys=True))) % 100000:05d}"
        output = f"订单提交成功,订单号: {order_id}"
        events.append({
            "type": "order_submitted",
            "order_id": order_id,
            "order_data": order_data,
        })

    elif name == "transfer_human":
        reason = input_data.get("reason", "")
        output = f"已转人工坐席,原因: {reason}。坐席将在 30 秒内接入。"
        events.append({
            "type": "transfer_human",
            "reason": reason,
        })

    else:
        output = f"Unknown tool: {name}"

    return output


# ═══════════════════════════════════════════════════════════
#  Web 版 Agent Loop — 收集事件版,支持 handoff 切换
# ═══════════════════════════════════════════════════════════

def web_agent_loop(session: dict) -> tuple[list[str], list, list]:
    """根据 active_agent 路由到对应 agent loop,收集事件。
    检测到 handoff_to_im 时:切换到 IMAgent 并立即跑 IMAgent loop 让它开口。
    返回 (agent_texts, all_events, segments)
      - segments: [{agent: "phone"|"im", text: "..."}, ...] 用于前端按 agent 标注
    """
    all_events: list = []
    segments: list = []          # [{agent, text}, ...]
    agent_texts: list[str] = []

    current_agent = session["active_agent"]
    if current_agent == "phone":
        system, tools, history = PHONE_SYSTEM, PHONE_TOOLS, session["phone_history"]
    else:
        system, tools, history = IM_SYSTEM, IM_TOOLS, session["im_history"]

    while True:
        response = client.messages.create(
            model=MODEL, system=system, messages=history,
            tools=tools, max_tokens=8000,
        )
        history.append({"role": "assistant", "content": response.content})

        # 收集文本输出 + segments
        for block in response.content:
            if getattr(block, "type", None) == "text" and block.text.strip():
                agent_texts.append(block.text)
                segments.append({"agent": current_agent, "text": block.text})

        # 非工具调用 → 结束
        if response.stop_reason != "tool_use":
            break

        # 检测是否有 handoff_to_im 调用(提取 customer_summary)
        handoff_summary = None
        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "handoff_to_im":
                handoff_summary = block.input.get("customer_summary", "")
                break

        results = []
        for block in response.content:
            if getattr(block, "type", None) != "tool_use":
                continue

            # 记录工具调用事件
            all_events.append({
                "type": "tool_call",
                "name": block.name,
                "input": block.input,
            })

            # ── Permission Hook: submit_order 门禁 ──
            blocked = None
            if block.name == "submit_order":
                order = block.input.get("order_data", {})
                missing = [f for f in REQUIRED_FIELDS if not order.get(f)]
                if missing:
                    blocked = (
                        f"Permission denied: 缺少必填字段 {missing},"
                        f"请先收集齐再提交。当前已收集: {list(order.keys())}"
                    )
                    all_events.append({
                        "type": "permission_blocked",
                        "tool": "submit_order",
                        "reason": f"缺少字段: {', '.join(missing)}",
                    })

            if blocked:
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(blocked),
                })
                continue

            # ── 执行工具(事件追加到 all_events) ──
            output = execute_tool(block.name, block.input, session, all_events)
            all_events.append({
                "type": "tool_result",
                "name": block.name,
                "output": output,
            })
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": output,
            })

        history.append({"role": "user", "content": results})

        # ── handoff 处理:切换到 IMAgent ──
        if handoff_summary is not None:
            session["active_agent"] = "im"
            session["handoff_summary"] = handoff_summary
            # 用 handoff summary 初始化 im_history
            session["im_history"] = [{
                "role": "user",
                "content": (
                    f"(场景:你是 IMAgent,PhoneAgent 已完成电话阶段的初筛和加好友,"
                    f"现在切换到微信沟通。\n"
                    f"PhoneAgent 传来的客户信息总结:\n{handoff_summary}\n\n"
                    "请基于以上信息,开始与客户在微信上沟通。第一步是收集行驶证照片。\n"
                    "需要时用 load_skill 加载 loan-sales 流程。)"
                ),
            }]
            # 发送 agent_changed 事件
            all_events.append({
                "type": "agent_changed",
                "from": "phone",
                "to": "im",
                "summary": handoff_summary,
            })
            # 立即跑 IMAgent 的 loop 让它开口
            im_texts, im_events, im_segments = web_agent_loop(session)
            agent_texts.extend(im_texts)
            segments.extend(im_segments)
            all_events.extend(im_events)
            # PhoneAgent loop 结束
            break

    return agent_texts, all_events, segments


# ═══════════════════════════════════════════════════════════
#  Routes
# ═══════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html", model=MODEL, skills=list_skills())


@app.route("/api/start", methods=["POST"])
def start_session():
    """开始会话:接收客户姓名+手机号,初始化 phone_history,触发 PhoneAgent 第一轮开口"""
    data = request.get_json(force=True)
    customer_name = (data.get("customer_name") or "").strip()
    customer_phone = (data.get("customer_phone") or "").strip()

    if not customer_name:
        return jsonify({"error": "请填写客户姓名"}), 400

    session_id, session = get_or_create_session()
    session["customer_name"] = customer_name
    session["customer_phone"] = customer_phone
    session["phone_history"] = [{"role": "user", "content": build_initial_prompt(customer_name, customer_phone)}]
    session["active_agent"] = "phone"
    session["started"] = True

    try:
        agent_texts, events, segments = web_agent_loop(session)
        return jsonify({
            "session_id": session_id,
            "agent_text": "\n\n".join(agent_texts),
            "agent_segments": segments,
            "events": events,
            "todos": session["todos"],
            "active_agent": session["active_agent"],
        })
    except Exception as e:
        import traceback
        return jsonify({
            "error": str(e),
            "traceback": traceback.format_exc(),
        }), 500


@app.route("/api/chat", methods=["POST"])
def chat():
    """接收 session_id + message,根据 active_agent 路由到对应 agent loop"""
    data = request.get_json(force=True)
    session_id = data.get("session_id")
    message = data.get("message", "")

    session_id, session = get_or_create_session(session_id)

    if session["started"] and message.strip():
        # 路由消息到当前活跃 agent 的 history
        if session["active_agent"] == "phone":
            session["phone_history"].append({"role": "user", "content": message})
        else:
            session["im_history"].append({"role": "user", "content": message})

    session["started"] = True

    try:
        agent_texts, events, segments = web_agent_loop(session)
        return jsonify({
            "session_id": session_id,
            "agent_text": "\n\n".join(agent_texts),
            "agent_segments": segments,
            "events": events,
            "todos": session["todos"],
            "active_agent": session["active_agent"],
        })
    except Exception as e:
        import traceback
        return jsonify({
            "error": str(e),
            "traceback": traceback.format_exc(),
        }), 500


@app.route("/api/reset", methods=["POST"])
def reset():
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id")
    if session_id and session_id in SESSIONS:
        del SESSIONS[session_id]
    new_id, _ = get_or_create_session()
    return jsonify({"session_id": new_id, "message": "会话已重置"})


# ═══════════════════════════════════════════════════════════
#  主程序
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 55)
    print("  车抵贷电销+IM 多 Agent Demo — Web 界面")
    print("  PhoneAgent: 电话初筛 + 加好友")
    print("  IMAgent:    IM收资料 + 提交订单")
    print(f"  模型: {MODEL}")
    print(f"  技能: {', '.join(SKILL_REGISTRY.keys()) or '(无)'}")
    print("  打开 http://localhost:5001")
    print("=" * 55)
    app.run(host="0.0.0.0", port=5001, debug=False)
