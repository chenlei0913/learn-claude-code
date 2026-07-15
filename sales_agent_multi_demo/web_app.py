#!/usr/bin/env python3
"""
通用 Agent Kernel — Web 测试界面。

从 code.py(Kernel)和 skills/loader.py(Skill 加载)构建,
所有业务知识从 skill 动态加载,本文件不含任何业务词。

Run: python sales_agent_multi_demo/web_app.py
Needs: 根目录 .env 已配置 ANTHROPIC_API_KEY 和 MODEL_ID
"""

import json, os, sys, uuid, threading, queue, time, traceback
from pathlib import Path

# 加载 code.py(Kernel)
_script_dir = str(Path(__file__).parent)
sys.path.insert(0, _script_dir)

from flask import Flask, request, jsonify, render_template, Response, stream_with_context

# 从 Kernel 导入通用件
from code import (
    client, MODEL, SKILLS_DIR, DEFAULT_SKILL,
    get_skill, list_skills, SkillLoader,
    register_hook, trigger_hooks,
    agent_loop, _do_handoff,
)
from loader import Skill, ToolDef, register_builtins

app = Flask(__name__)


# ═══════════════════════════════════════════════════════════
#  Session 管理 — 每个会话独立状态
# ═══════════════════════════════════════════════════════════

SESSIONS: dict[str, dict] = {}


def get_or_create_session(session_id: str | None = None):
    if session_id and session_id in SESSIONS:
        return session_id, SESSIONS[session_id]
    session_id = session_id or uuid.uuid4().hex[:8]

    skill = get_skill(DEFAULT_SKILL)
    first_agent = skill.agent_names()[0] if skill.agent_names() else "phone"

    SESSIONS[session_id] = {
        "phone_history": [],
        "im_history": [],
        "active_agent": first_agent,
        "phone_todos": [],
        "im_todos": [],
        "todos": [],
        "started": False,
        "friend_status": "pending",
        "friend_request_count": 0,
        "customer_name": "",
        "customer_phone": "",
        "handoff_payload": {},
        "handoff_summary": "",
        "reply_timeout_seconds": 10,
        "phone_timeout_count": 0,
        "timeout_timer": None,
        "agent_lock": threading.Lock(),
        "events_queue": None,
        "order_submitted": False,
        "order_id_final": "",
        "order_data_final": {},
        "transferred": False,
        "transfer_reason_final": "",
        "summary_sent": False,
        # Kernel 需要的上下文
        "_skill": skill,
        "_skill_loader": SkillLoader(SKILLS_DIR),
    }
    return session_id, SESSIONS[session_id]


def build_initial_prompt(session: dict, customer_name: str, customer_phone: str) -> str:
    """构建第一个 agent 的初始 prompt(通用)"""
    skill: Skill = session["_skill"]
    active_agent = session["active_agent"]
    agent_name = skill.get_agent(active_agent).name if skill.get_agent(active_agent) else active_agent

    name_part = f"客户姓名: {customer_name}" if customer_name else "客户姓名: 未知"
    phone_part = f"客户手机号: {customer_phone}" if customer_phone else ""
    return (
        f"(场景:你是 {agent_name},刚刚接通了客户的联系。"
        f"外呼系统信息:{name_part}{'，' + phone_part if phone_part else ''}。\n"
        "请开始对话。第一轮调 load_skill 加载流程定义 + todo_write 建立流程清单,"
        "第二轮才开始对客户说话。)"
    )


# ═══════════════════════════════════════════════════════════
#  回复超时调度 (s14 思路) — 仅电话阶段生效
# ═══════════════════════════════════════════════════════════

REPLY_TIMEOUT_SECONDS = 10
MAX_TIMEOUT_COUNT = 3


def cancel_reply_timeout(session: dict):
    timer = session.get("timeout_timer")
    if timer:
        timer.cancel()
        session["timeout_timer"] = None


def start_reply_timeout(session: dict):
    """agent 回复后启动回复超时定时器(仅 phone 阶段)"""
    cancel_reply_timeout(session)
    if session["active_agent"] != "phone":
        return
    if session["phone_timeout_count"] >= MAX_TIMEOUT_COUNT:
        return

    def on_timeout():
        session["phone_timeout_count"] += 1
        count = session["phone_timeout_count"]
        seconds = session.get("reply_timeout_seconds", REPLY_TIMEOUT_SECONDS)
        if count >= MAX_TIMEOUT_COUNT:
            timeout_msg = "[系统: 客户已3次未回复,建议结束通话]"
        else:
            timeout_msg = f"[系统: 客户{seconds}秒未回复,第{count}次]"
        print(f"[timeout] 第{count}次超时,注入: {timeout_msg}", flush=True)

        with session["agent_lock"]:
            session["phone_history"].append({"role": "user", "content": timeout_msg})
            eq = session.get("events_queue")
            if eq is None:
                return
            try:
                for sse_text in web_agent_loop_stream(session):
                    eq.put(sse_text)
            except Exception as e:
                eq.put(_sse("error", {
                    "error": str(e),
                    "traceback": traceback.format_exc(),
                }))
            start_reply_timeout(session)

    timer = threading.Timer(
        session.get("reply_timeout_seconds", REPLY_TIMEOUT_SECONDS),
        on_timeout,
    )
    timer.daemon = True
    session["timeout_timer"] = timer
    timer.start()


# ═══════════════════════════════════════════════════════════
#  Web 版 Agent Loop — SSE 流式,skill-driven
# ═══════════════════════════════════════════════════════════

def _sse(event: str, data: dict) -> str:
    if event not in ('thinking_delta', 'text_delta', 'session_summary'):
        keys = {k: data[k] for k in ('agent', 'name', 'from', 'to', 'active_agent', 'blocked', 'reason') if k in data}
        print(f"[SSE-LOG] {event} {keys}", flush=True)
    elif event == 'text_delta':
        print(f"[SSE-LOG] text_delta len={len(data.get('text', ''))}", flush=True)
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def build_session_summary(session: dict) -> dict:
    if session.get("order_submitted"):
        status = "completed"
    elif session.get("transferred"):
        status = "transferred"
    else:
        status = "unknown"
    return {
        "status": status,
        "customer_name": session.get("customer_name", ""),
        "customer_phone": session.get("customer_phone", ""),
        "handoff_summary": session.get("handoff_summary", ""),
        "friend_status": session.get("friend_status", "pending"),
        "order_submitted": session.get("order_submitted", False),
        "order_id": session.get("order_id_final", ""),
        "order_data": session.get("order_data_final", {}),
        "transferred": session.get("transferred", False),
        "transfer_reason": session.get("transfer_reason_final", ""),
    }


def web_agent_loop_stream(session: dict):
    """SSE 流式 agent loop,从 skill 动态装配 system + tools。

    检测到 handoff_to_im 时:切换 active_agent,用 payload 初始化下一个 agent,
    递归继续流式。
    """
    skill: Skill = session["_skill"]
    current_agent = session["active_agent"]

    # 动态装配 system + tools_schema
    system = skill.build_system_prompt(current_agent)
    agent_tools = skill.get_tools_for_agent(current_agent)
    tools_schema = [
        {"name": t.name, "description": t.description, **t.schema}
        for t in agent_tools
    ]

    history_key = f"{current_agent}_history"
    history = session[history_key]

    yield _sse("agent_start", {"agent": current_agent})

    while True:
        # ── 流式拉取模型输出 ──
        try:
            stream_ctx = client.messages.stream(
                model=MODEL, system=system, messages=history,
                tools=tools_schema, max_tokens=8000,
            )
        except TypeError:
            # 旧版 SDK fallback
            response = client.messages.create(
                model=MODEL, system=system, messages=history,
                tools=tools_schema, max_tokens=8000,
            )
            history.append({"role": "assistant", "content": response.content})
            for block in response.content:
                if getattr(block, "type", None) == "text" and block.text.strip():
                    yield _sse("text_start", {"agent": current_agent})
                    yield _sse("text_delta", {"text": block.text})
                    yield _sse("text_end", {})
            stop_reason = response.stop_reason
            tool_blocks = [
                {"id": b.id, "name": b.name, "input": b.input}
                for b in response.content if getattr(b, "type", None) == "tool_use"
            ]
        else:
            tool_blocks = []
            stop_reason = None
            with stream_ctx as stream:
                current_block_type = None
                current_tool_name = None
                current_tool_input_str = ""

                for event in stream:
                    etype = getattr(event, "type", None)

                    if etype == "content_block_start":
                        block = event.content_block
                        btype = getattr(block, "type", None)
                        current_block_type = btype
                        if btype == "thinking":
                            yield _sse("thinking_start", {"agent": current_agent})
                        elif btype == "text":
                            yield _sse("text_start", {"agent": current_agent})
                        elif btype == "tool_use":
                            current_tool_name = getattr(block, "name", "")
                            current_tool_input_str = ""

                    elif etype == "content_block_delta":
                        delta = event.delta
                        dtype = getattr(delta, "type", None)
                        if dtype == "thinking_delta":
                            yield _sse("thinking_delta", {"text": getattr(delta, "thinking", "")})
                        elif dtype == "text_delta":
                            yield _sse("text_delta", {"text": getattr(delta, "text", "")})
                        elif dtype == "input_json_delta":
                            current_tool_input_str += getattr(delta, "partial_json", "")

                    elif etype == "content_block_stop":
                        if current_block_type == "thinking":
                            yield _sse("thinking_end", {})
                        elif current_block_type == "text":
                            yield _sse("text_end", {})
                        elif current_block_type == "tool_use":
                            try:
                                tool_input = json.loads(current_tool_input_str) if current_tool_input_str else {}
                            except json.JSONDecodeError:
                                tool_input = {}
                            tool_blocks.append({
                                "id": getattr(event, "index", len(tool_blocks)),
                                "name": current_tool_name,
                                "input": tool_input,
                            })
                        current_block_type = None
                        current_tool_name = None
                        current_tool_input_str = ""

                    elif etype == "message_stop":
                        pass

                final_msg = stream.get_final_message()
                history.append({"role": "assistant", "content": final_msg.content})
                stop_reason = final_msg.stop_reason

        # ── 非工具调用 → 结束 ──
        if stop_reason != "tool_use":
            break

        # ── 执行工具(统一走 ToolDef.invoke) ──
        results = []
        pending_handoff = None

        for tb in tool_blocks:
            name = tb["name"]
            input_data = tb["input"]
            tool_use_id = tb.get("id", "")
            if not isinstance(tool_use_id, str):
                tool_use_id = f"toolu_{current_agent}_{int(time.time()*1000)}_{tool_blocks.index(tb)}"

            yield _sse("tool_call", {"name": name, "input": input_data})

            tool_def: ToolDef | None = skill.tools.get(name)
            if not tool_def:
                output = f"Unknown tool: {name}"
                results.append({"type": "tool_result", "tool_use_id": tool_use_id, "content": output})
                yield _sse("tool_result", {"name": name, "output": output})
                continue

            # 执行(ToolDef.invoke 内部做权限 + 限流检查)
            events_buf = []
            try:
                output = tool_def.invoke(input_data, session, events_buf)
            except Exception as e:
                output = f"工具执行异常: {e}"
                events_buf.append({"type": "error", "message": str(e)})

            for ev in events_buf:
                # 权限拦截事件单独发
                if ev.get("type") == "permission_blocked":
                    yield _sse("permission_blocked", ev)
                else:
                    yield _sse(ev["type"], ev)

            yield _sse("tool_result", {"name": name, "output": output})
            results.append({"type": "tool_result", "tool_use_id": tool_use_id, "content": output})

            # 检测 handoff
            if name == "handoff_to_im":
                pending_handoff = session.pop("_pending_handoff", None)

        history.append({"role": "user", "content": results})

        # ── handoff:切换到下一个 agent,递归继续流式 ──
        if pending_handoff is not None:
            _do_handoff(skill, session, pending_handoff)
            yield _sse("agent_changed", {
                "from": current_agent,
                "to": session["active_agent"],
                "summary": session.get("handoff_summary", ""),
                "payload": session.get("handoff_payload", {}),
            })
            yield from web_agent_loop_stream(session)
            break

    yield _sse("done", {"active_agent": session["active_agent"]})

    # 会话到达终态且未发送过总结 → 发送 session_summary
    if not session.get("summary_sent") and (
        session.get("order_submitted") or session.get("transferred")
    ):
        session["summary_sent"] = True
        cancel_reply_timeout(session)
        yield _sse("session_summary", build_session_summary(session))


# ═══════════════════════════════════════════════════════════
#  Routes
# ═══════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html", model=MODEL, skills=list_skills())


@app.route("/api/start", methods=["POST"])
def start_session():
    """开始会话:SSE 流式返回第一个 agent 的输出"""
    data = request.get_json(force=True)
    customer_name = (data.get("customer_name") or "").strip()
    customer_phone = (data.get("customer_phone") or "").strip()

    if not customer_name:
        return jsonify({"error": "请填写客户姓名"}), 400

    session_id, session = get_or_create_session()
    session["customer_name"] = customer_name
    session["customer_phone"] = customer_phone

    first_agent = session["active_agent"]
    session[f"{first_agent}_history"] = [
        {"role": "user", "content": build_initial_prompt(session, customer_name, customer_phone)}
    ]
    session["started"] = True

    def generate():
        yield _sse("session", {"session_id": session_id})
        with session["agent_lock"]:
            try:
                yield from web_agent_loop_stream(session)
            except Exception as e:
                yield _sse("error", {"error": str(e), "traceback": traceback.format_exc()})
        start_reply_timeout(session)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.route("/api/chat", methods=["POST"])
def chat():
    """接收 session_id + message,SSE 流式返回 agent 输出"""
    data = request.get_json(force=True)
    session_id = data.get("session_id")
    message = data.get("message", "")

    session_id, session = get_or_create_session(session_id)

    cancel_reply_timeout(session)
    if session["started"] and message.strip():
        session["phone_timeout_count"] = 0
        active = session["active_agent"]
        session[f"{active}_history"].append({"role": "user", "content": message})

    session["started"] = True

    def generate():
        yield _sse("session", {"session_id": session_id})
        with session["agent_lock"]:
            try:
                yield from web_agent_loop_stream(session)
            except Exception as e:
                yield _sse("error", {"error": str(e), "traceback": traceback.format_exc()})
        start_reply_timeout(session)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.route("/api/reset", methods=["POST"])
def reset():
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id")
    if session_id and session_id in SESSIONS:
        cancel_reply_timeout(SESSIONS[session_id])
        del SESSIONS[session_id]
    new_id, _ = get_or_create_session()
    return jsonify({"session_id": new_id, "message": "会话已重置"})


@app.route("/api/events")
def events():
    """SSE 长连接:推送超时轮 agent 输出给前端"""
    session_id = request.args.get("session_id")
    if not session_id or session_id not in SESSIONS:
        return jsonify({"error": "invalid session_id"}), 400
    session = SESSIONS[session_id]
    if session["events_queue"] is None:
        session["events_queue"] = queue.Queue()
    eq = session["events_queue"]

    def generate():
        while True:
            try:
                sse_text = eq.get(timeout=15)
                if sse_text == "__close__":
                    break
                yield sse_text
            except queue.Empty:
                yield ": heartbeat\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


# ═══════════════════════════════════════════════════════════
#  主程序
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 55)
    print("  通用 Agent Kernel — Web 界面")
    print(f"  默认 Skill: {DEFAULT_SKILL}")
    print(f"  模型: {MODEL}")
    print(f"  可用 Skill: {list_skills()}")
    print("  打开 http://localhost:5001")
    print("=" * 55)
    app.run(host="0.0.0.0", port=5001, debug=False)
