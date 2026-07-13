#!/usr/bin/env python3
"""
车抵贷电销+IM Agent Demo — Web 测试界面
基于 code.py 的核心逻辑,提供 Flask Web API 供前端测试

Run: python sales_agent_demo/web_app.py
Needs: 根目录 .env 已配置 ANTHROPIC_API_KEY 和 MODEL_ID
"""

import json, os, sys, uuid, importlib.util, threading, random, time
from pathlib import Path

# 先用 importlib 加载 code.py,再清理 sys.path 避免与标准库 code 模块冲突
_script_dir = str(Path(__file__).parent)
_code_path = Path(__file__).parent / "code.py"
_spec = importlib.util.spec_from_file_location("sales_agent_code", _code_path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
# 移除脚本目录,防止后续 import code 命中本地 code.py
while _script_dir in sys.path:
    sys.path.remove(_script_dir)

from flask import Flask, request, jsonify, render_template

SYSTEM = _mod.SYSTEM
TOOLS = _mod.TOOLS
REQUIRED_FIELDS = _mod.REQUIRED_FIELDS
SKILL_REGISTRY = _mod.SKILL_REGISTRY
list_skills = _mod.list_skills
load_skill = _mod.load_skill
_normalize_todos = _mod._normalize_todos
assemble_system_prompt = _mod.assemble_system_prompt   # s10: 按阶段组装 system prompt
client = _mod.client
MODEL = _mod.MODEL

app = Flask(__name__)


# ═══════════════════════════════════════════════════════════
#  Session 管理
# ═══════════════════════════════════════════════════════════

SESSIONS: dict[str, dict] = {}


def build_initial_prompt(customer_name: str = "", customer_phone: str = "") -> str:
    """根据客户信息构建初始 prompt(外呼系统已知客户姓名+手机号)"""
    name_part = f"客户姓名: {customer_name}" if customer_name else "客户姓名: 未知"
    phone_part = f"客户手机号: {customer_phone}" if customer_phone else ""
    return (
        f"(场景:你是车抵贷电销 Agent,刚刚拨通了客户的电话,客户已接听。\n"
        f"外呼系统信息:{name_part}{'，' + phone_part if phone_part else ''}。\n"
        "请开始对话——按开场白流程:先确认身份(\"请问是 {客户姓名} 吗\"),"
        "客户确认后自报家门(公司做车抵贷),简短寒暄一句,再询问是否有资金需求。\n"
        "根据客户回应推进流程。需要时用 load_skill 加载 loan-sales 流程。)"
    )


def get_or_create_session(session_id: str | None = None):
    if session_id and session_id in SESSIONS:
        return session_id, SESSIONS[session_id]
    session_id = session_id or uuid.uuid4().hex[:8]
    SESSIONS[session_id] = {
        "history": [],                      # 由 /api/start 填入初始 prompt
        "todos": [],
        "started": False,
        "friend_status": "pending",        # pending / added
        "friend_request_count": 0,          # 发送好友请求次数,最多3
        "stage": "phone",                   # s10: "phone" | "im",加好友成功后切 im
        "customer_name": "",                # 客户姓名(外呼系统已知)
        "customer_phone": "",               # 客户手机号
    }
    return session_id, SESSIONS[session_id]


# ═══════════════════════════════════════════════════════════
#  后台任务 (s08) — 加好友检测,每5秒一次
# ═══════════════════════════════════════════════════════════

def _start_friend_check_thread(session: dict):
    """启动后台线程:模拟客户在 5-12 秒后添加好友成功"""
    def _check():
        # 模拟:5-12秒后客户添加成功
        time.sleep(random.uniform(5, 12))
        session["friend_status"] = "added"
    t = threading.Thread(target=_check, daemon=True)
    t.start()


# ═══════════════════════════════════════════════════════════
#  Web 版工具执行 — 收集事件而非 print
# ═══════════════════════════════════════════════════════════

def execute_tool(name: str, input_data: dict, session: dict) -> tuple[str, list]:
    """执行单个工具,返回 (output_text, events)"""
    events: list = []

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
        # s10: 加好友成功 → 切 stage=im,system prompt 重组为 IM 风格
        if added and session.get("stage") != "im":
            session["stage"] = "im"
            events.append({
                "type": "stage_changed",
                "from": "phone",
                "to": "im",
                "detail": "好友已添加,system prompt 切换到 IM 风格",
            })
        output = json.dumps({"status": status, "added": added}, ensure_ascii=False)
        events.append({
            "type": "tool_detail",
            "name": name,
            "detail": f"好友状态: {status}",
            "output_preview": output,
        })

    elif name == "upload_driving_license":
        # 模拟审核:默认通过。失败时返回 {"passed": false, "reason": "..."}
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

    return output, events


# ═══════════════════════════════════════════════════════════
#  Web 版 Agent Loop
# ═══════════════════════════════════════════════════════════

def web_agent_loop(session: dict) -> tuple[list[str], list]:
    """执行 agent loop,收集所有事件(不 print)。每轮按 session['stage'] 重组 system (s10)。"""
    messages = session["history"]
    all_events: list = []
    agent_texts: list[str] = []

    while True:
        # s10: 每轮按当前 stage 重组 system prompt
        system = assemble_system_prompt(session.get("stage", "phone"))
        response = client.messages.create(
            model=MODEL, system=system, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})

        # 收集文本输出
        for block in response.content:
            if getattr(block, "type", None) == "text" and block.text.strip():
                agent_texts.append(block.text)

        # 非工具调用 → 结束
        if response.stop_reason != "tool_use":
            break

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            # 记录工具调用
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

            # ── 执行工具 ──
            output, tool_events = execute_tool(block.name, block.input, session)
            all_events.extend(tool_events)
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

        messages.append({"role": "user", "content": results})

    # Stop hook
    tool_count = sum(
        1 for m in messages
        for b in (m.get("content") if isinstance(m.get("content"), list) else [])
        if isinstance(b, dict) and b.get("type") == "tool_result"
    )
    all_events.append({"type": "stop", "tool_count": tool_count})

    return agent_texts, all_events


# ═══════════════════════════════════════════════════════════
#  Routes
# ═══════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html", model=MODEL, skills=list_skills())


@app.route("/api/start", methods=["POST"])
def start_session():
    """开始会话:接收客户姓名+手机号,注入初始 prompt,触发 Agent 第一轮开口"""
    data = request.get_json(force=True)
    customer_name = (data.get("customer_name") or "").strip()
    customer_phone = (data.get("customer_phone") or "").strip()

    if not customer_name:
        return jsonify({"error": "请填写客户姓名"}), 400

    session_id, session = get_or_create_session()
    session["customer_name"] = customer_name
    session["customer_phone"] = customer_phone
    session["history"] = [{"role": "user", "content": build_initial_prompt(customer_name, customer_phone)}]
    session["started"] = True

    try:
        agent_texts, events = web_agent_loop(session)
        return jsonify({
            "session_id": session_id,
            "agent_text": "\n\n".join(agent_texts),
            "events": events,
            "todos": session["todos"],
            "stage": session.get("stage", "phone"),
        })
    except Exception as e:
        import traceback
        return jsonify({
            "error": str(e),
            "traceback": traceback.format_exc(),
        }), 500


@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json(force=True)
    session_id = data.get("session_id")
    message = data.get("message", "")

    session_id, session = get_or_create_session(session_id)

    if session["started"] and message.strip():
        session["history"].append({"role": "user", "content": message})

    session["started"] = True

    try:
        agent_texts, events = web_agent_loop(session)
        return jsonify({
            "session_id": session_id,
            "agent_text": "\n\n".join(agent_texts),
            "events": events,
            "todos": session["todos"],
            "stage": session.get("stage", "phone"),   # s10: 当前阶段
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
    print("  车抵贷电销+IM Agent Demo — Web 界面")
    print(f"  模型: {MODEL}")
    print(f"  技能: {', '.join(SKILL_REGISTRY.keys()) or '(无)'}")
    print("  打开 http://localhost:5000")
    print("=" * 55)
    app.run(host="0.0.0.0", port=5000, debug=False)
