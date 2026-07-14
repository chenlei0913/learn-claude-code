#!/usr/bin/env python3
"""
车抵贷电销+IM 多 Agent Demo — Web 测试界面
两个独立 Agent 通过 handoff_to_im 协作:
  PhoneAgent(电话阶段:初筛+加好友) → IMAgent(IM 阶段:收资料+提交订单)

Run: python sales_agent_multi_demo/web_app.py
Needs: 根目录 .env 已配置 ANTHROPIC_API_KEY 和 MODEL_ID
"""

import json, os, sys, uuid, importlib.util, threading, random, time, queue
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

from flask import Flask, request, jsonify, render_template, Response, stream_with_context

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
        "\n\n"
        "重要:你的 text 输出就是**说给客户听的话**,会直接播放/显示给客户。"
        "不要输出任何自我陈述、内心独白、流程说明、计划描述(如\"现在等待客户回应\""
        "\"接下来我要询问意向\"等)。这些思考放到 thinking 块里。"
        "text 里只允许出现你当面跟客户说的话。"
    )


def get_or_create_session(session_id: str | None = None):
    if session_id and session_id in SESSIONS:
        return session_id, SESSIONS[session_id]
    session_id = session_id or uuid.uuid4().hex[:8]
    SESSIONS[session_id] = {
        "phone_history": [],         # PhoneAgent 的独立 history
        "im_history": [],            # IMAgent 的独立 history
        "active_agent": "phone",     # "phone" | "im"
        "phone_todos": [],           # PhoneAgent 的流程进度
        "im_todos": [],              # IMAgent 的流程进度
        "todos": [],                 # 合并视图(兼容)
        "started": False,
        "friend_status": "pending",
        "friend_request_count": 0,
        "customer_name": "",
        "customer_phone": "",
        "handoff_summary": "",       # PhoneAgent 传给 IMAgent 的信息
        "reply_timeout_seconds": 10, # 电话阶段回复超时秒数(后端调度线程用)
        "phone_timeout_count": 0,    # 电话阶段连续超时次数
        "timeout_timer": None,       # threading.Timer 对象
        "agent_lock": threading.Lock(),  # agent_loop 互斥锁(防超时轮和用户轮并发)
        "events_queue": None,        # queue.Queue,超时轮 agent 输出推到这里,供 /api/events 长连接读取
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
#  回复超时调度 (s14 思路) — 独立 Timer 线程 + 自动触发 agent_loop
#  调度(scheduler): start_reply_timeout 在 agent 回复后启动倒计时
#  交付(deliver): Timer 触发后后台线程跑 agent_loop,输出推到 events_queue
#  消费(consume): /api/events SSE 长连接从 events_queue 读取推给前端
# ═══════════════════════════════════════════════════════════

REPLY_TIMEOUT_SECONDS = 10  # 默认超时秒数
MAX_TIMEOUT_COUNT = 3       # 最多超时次数,超过后结束通话

def cancel_reply_timeout(session: dict):
    """取消回复超时定时器(客户回复或 handoff 时调用)"""
    timer = session.get("timeout_timer")
    if timer:
        timer.cancel()
        session["timeout_timer"] = None

def start_reply_timeout(session: dict):
    """agent 回复后启动回复超时定时器(s14 scheduler)"""
    cancel_reply_timeout(session)
    if session["active_agent"] != "phone":
        return
    if session["phone_timeout_count"] >= MAX_TIMEOUT_COUNT:
        return  # 通话已结束

    def on_timeout():
        """Timer 回调:超时触发,后台线程跑 agent_loop(s14 deliver)"""
        session["phone_timeout_count"] += 1
        count = session["phone_timeout_count"]
        seconds = session.get("reply_timeout_seconds", REPLY_TIMEOUT_SECONDS)
        if count >= MAX_TIMEOUT_COUNT:
            timeout_msg = "[系统: 客户已3次未回复,建议结束通话]"
        else:
            timeout_msg = f"[系统: 客户{seconds}秒未回复,第{count}次]"
        print(f"[timeout] 第{count}次超时,注入: {timeout_msg}", flush=True)
        # 获取 agent_lock,防止和用户请求并发
        with session["agent_lock"]:
            session["phone_history"].append({"role": "user", "content": timeout_msg})
            eq = session.get("events_queue")
            if eq is None:
                return  # 没有长连接,丢弃
            # 跑 agent_loop,输出推到 events_queue
            try:
                for sse_text in web_agent_loop_stream(session):
                    eq.put(sse_text)
            except Exception as e:
                import traceback
                eq.put(_sse("error", {
                    "error": str(e),
                    "traceback": traceback.format_exc(),
                }))
            # agent_loop 结束,启动下一轮超时定时器
            start_reply_timeout(session)

    timer = threading.Timer(
        session.get("reply_timeout_seconds", REPLY_TIMEOUT_SECONDS),
        on_timeout,
    )
    timer.daemon = True
    session["timeout_timer"] = timer
    timer.start()


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
            agent = session["active_agent"]
            # 给每项打上 agent 标签,按 agent 分开存储
            tagged = [dict(t, agent=agent) for t in normalized]
            if agent == "phone":
                session["phone_todos"] = tagged
            else:
                session["im_todos"] = tagged
            # 合并视图(电话阶段 + IM 阶段)
            session["todos"] = session["phone_todos"] + session["im_todos"]
            events.append({"type": "todos_updated", "todos": session["todos"]})
            output = f"已更新 {len(normalized)} 个阶段状态"

    elif name == "send_friend_request":
        session["friend_request_count"] += 1
        count = session["friend_request_count"]
        # 如果客户更正了手机号,更新 session
        new_phone = input_data.get("phone", "").strip()
        phone_note = ""
        if new_phone and new_phone != session.get("customer_phone", ""):
            session["customer_phone"] = new_phone
            phone_note = f"(已更新手机号为 {new_phone})"
        if session["friend_status"] != "added":
            session["friend_status"] = "pending"
            _start_friend_check_thread(session)
        target_phone = session.get("customer_phone", "")
        tail = target_phone[-4:] if len(target_phone) >= 4 else target_phone
        output = f"已发送添加好友请求(第 {count} 次),目标手机号尾号 {tail}{(' ' + phone_note) if phone_note else ''}。后台正在每5秒检测一次是否添加成功。"
        events.append({
            "type": "tool_detail",
            "name": name,
            "detail": f"发送好友请求(第{count}次),尾号{tail}{phone_note}",
            "output_preview": output,
        })

    elif name == "check_friend_added":
        status = session.get("friend_status", "pending")
        added = status == "added"
        result = {"status": status, "added": added}
        if added:
            result["next_action"] = "好友已添加成功,请立即在同一轮调用 handoff_to_im 工具切换到 IMAgent"
        output = json.dumps(result, ensure_ascii=False)
        events.append({
            "type": "tool_detail",
            "name": name,
            "detail": f"好友状态: {status}",
            "output_preview": output,
        })

    elif name == "handoff_to_im":
        customer_summary = input_data.get("customer_summary", "")
        output = f"已切换到 IMAgent。电话阶段总结已传递: {customer_summary[:100]}..."
        # 强制更新 phone_todos:加微信好友 + handoff到IM 标记为 completed
        for t in session["phone_todos"]:
            content = t.get("content", "")
            if any(k in content for k in ["加微信", "加好友", "好友"]):
                t["status"] = "completed"
            if any(k in content for k in ["handoff", "切换", "移交", "到IM"]):
                t["status"] = "completed"
        session["todos"] = session["phone_todos"] + session["im_todos"]
        events.append({"type": "todos_updated", "todos": session["todos"]})
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
#  Web 版 Agent Loop — SSE 流式版,支持 thinking + text + tool_use
# ═══════════════════════════════════════════════════════════

def _sse(event: str, data: dict) -> str:
    """格式化为 SSE 事件块"""
    # 调试日志:记录非 delta 事件(delta 太多会刷屏)
    if event not in ('thinking_delta', 'text_delta'):
        # 提取关键字段
        keys = {k: data[k] for k in ('agent', 'name', 'from', 'to', 'active_agent', 'blocked', 'reason') if k in data}
        print(f"[SSE-LOG] {event} {keys}", flush=True)
    elif event == 'text_delta':
        # text_delta 只记录长度,不打内容
        print(f"[SSE-LOG] text_delta len={len(data.get('text', ''))} text={repr(data.get('text','')[:40])}", flush=True)
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def web_agent_loop_stream(session: dict):
    """SSE 流式版 agent loop。yield 出 SSE 文本块。
    检测到 handoff_to_im 时切换到 IMAgent 并继续流式。
    """
    current_agent = session["active_agent"]
    if current_agent == "phone":
        system, tools, history = PHONE_SYSTEM, PHONE_TOOLS, session["phone_history"]
    else:
        system, tools, history = IM_SYSTEM, IM_TOOLS, session["im_history"]

    yield _sse("agent_start", {"agent": current_agent})

    while True:
        # ── 用 stream API 流式拉取模型输出 ──
        try:
            stream_ctx = client.messages.stream(
                model=MODEL, system=system, messages=history,
                tools=tools, max_tokens=8000,
            )
        except TypeError:
            # 旧版 SDK 不支持 stream(),fallback 到非流式
            response = client.messages.create(
                model=MODEL, system=system, messages=history,
                tools=tools, max_tokens=8000,
            )
            history.append({"role": "assistant", "content": response.content})
            for block in response.content:
                if getattr(block, "type", None) == "text" and block.text.strip():
                    yield _sse("text_start", {"agent": current_agent})
                    yield _sse("text_delta", {"text": block.text})
                    yield _sse("text_end", {})
            stop_reason = response.stop_reason
            tool_blocks = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
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

        # ── 检测 handoff_to_im ──
        handoff_summary = None
        for tb in tool_blocks:
            if tb["name"] == "handoff_to_im":
                handoff_summary = tb["input"].get("customer_summary", "")
                break

        # ── 执行工具 ──
        results = []
        for tb in tool_blocks:
            name = tb["name"]
            input_data = tb["input"]
            tool_use_id = tb.get("id", "")
            if not isinstance(tool_use_id, str):
                tool_use_id = f"toolu_{current_agent}_{int(time.time()*1000)}_{tool_blocks.index(tb)}"

            yield _sse("tool_call", {"name": name, "input": input_data})

            # ── Permission Hook: submit_order 门禁 ──
            blocked = None
            if name == "submit_order":
                order = input_data.get("order_data", {})
                missing = [f for f in REQUIRED_FIELDS if not order.get(f)]
                if missing:
                    blocked = (
                        f"Permission denied: 缺少必填字段 {missing},"
                        f"请先收集齐再提交。当前已收集: {list(order.keys())}"
                    )
                    yield _sse("permission_blocked", {
                        "tool": "submit_order",
                        "reason": f"缺少字段: {', '.join(missing)}",
                    })

            if blocked:
                results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": str(blocked),
                })
                yield _sse("tool_result", {"name": name, "output": str(blocked), "blocked": True})
                continue

            events_buf = []
            output = execute_tool(name, input_data, session, events_buf)
            for ev in events_buf:
                yield _sse(ev["type"], ev)
            yield _sse("tool_result", {"name": name, "output": output})

            results.append({
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": output,
            })

        history.append({"role": "user", "content": results})

        # ── handoff:切换到 IMAgent,递归继续流式 ──
        if handoff_summary is not None:
            session["active_agent"] = "im"
            session["handoff_summary"] = handoff_summary
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
            yield _sse("agent_changed", {
                "from": "phone", "to": "im", "summary": handoff_summary,
            })
            yield from web_agent_loop_stream(session)
            break

    yield _sse("done", {"active_agent": session["active_agent"]})


# ═══════════════════════════════════════════════════════════
#  Routes
# ═══════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html", model=MODEL, skills=list_skills())


@app.route("/api/start", methods=["POST"])
def start_session():
    """开始会话:SSE 流式返回 PhoneAgent 的输出"""
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

    def generate():
        yield _sse("session", {"session_id": session_id})
        with session["agent_lock"]:
            try:
                yield from web_agent_loop_stream(session)
            except Exception as e:
                import traceback
                yield _sse("error", {
                    "error": str(e),
                    "traceback": traceback.format_exc(),
                })
        # 开场白结束后启动回复超时定时器
        start_reply_timeout(session)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/api/chat", methods=["POST"])
def chat():
    """接收 session_id + message,SSE 流式返回 agent 输出"""
    data = request.get_json(force=True)
    session_id = data.get("session_id")
    message = data.get("message", "")

    session_id, session = get_or_create_session(session_id)

    # 真实用户回复:取消超时定时器,重置计数
    cancel_reply_timeout(session)
    if session["started"] and message.strip():
        session["phone_timeout_count"] = 0
        if session["active_agent"] == "phone":
            session["phone_history"].append({"role": "user", "content": message})
        else:
            session["im_history"].append({"role": "user", "content": message})

    session["started"] = True

    def generate():
        yield _sse("session", {"session_id": session_id})
        # 获取 agent_lock,防止和超时轮并发
        with session["agent_lock"]:
            try:
                yield from web_agent_loop_stream(session)
            except Exception as e:
                import traceback
                yield _sse("error", {
                    "error": str(e),
                    "traceback": traceback.format_exc(),
                })
        # agent_loop 结束,启动回复超时定时器(s14 scheduler)
        start_reply_timeout(session)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/api/reset", methods=["POST"])
def reset():
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id")
    if session_id and session_id in SESSIONS:
        old = SESSIONS[session_id]
        cancel_reply_timeout(old)  # 清理定时器
        del SESSIONS[session_id]
    new_id, _ = get_or_create_session()
    return jsonify({"session_id": new_id, "message": "会话已重置"})


@app.route("/api/events")
def events():
    """SSE 长连接:推送超时轮 agent 输出给前端(s14 consume)。
    前端建立长连接后,后端超时触发的 agent_loop 输出通过此连接推送。"""
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
                # 心跳,保持连接
                yield ": heartbeat\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/api/test_im")
def test_im():
    """测试端点:直接构造一个已 handoff 的 IMAgent session,SSE 流式返回。
    用于排查 IMAgent 阶段文本是否正常显示。"""
    session = {
        "customer_name": "测试IM", "customer_phone": "13800000001",
        "active_agent": "im",
        "handoff_summary": "客户张先生,电话13800000001,同意加微信,微信账号:zhang_test",
        "phone_history": [{"role": "user", "content": "开始"}],
        "im_history": [{
            "role": "user",
            "content": (
                "(场景:你是 IMAgent,PhoneAgent 已完成电话阶段的初筛和加好友,"
                "现在切换到微信沟通。\n"
                "PhoneAgent 传来的客户信息总结:\n客户张先生,同意加微信\n\n"
                "请基于以上信息,开始与客户在微信上沟通。第一步是收集行驶证照片。\n"
                "需要时用 load_skill 加载 loan-sales 流程。)"
            ),
        }],
        "phone_todos": [], "im_todos": [], "todos": [], "started": True,
    }

    def generate():
        yield _sse("session", {"session_id": "test-im-session"})
        yield _sse("agent_changed", {
            "from": "phone", "to": "im",
            "summary": "客户张先生,同意加微信",
        })
        try:
            yield from web_agent_loop_stream(session)
        except Exception as e:
            import traceback
            yield _sse("error", {
                "error": str(e),
                "traceback": traceback.format_exc(),
            })

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


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
