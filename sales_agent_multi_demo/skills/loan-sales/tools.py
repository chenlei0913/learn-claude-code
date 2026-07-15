#!/usr/bin/env python3
"""
车抵贷业务的专用工具实现。

每个工具用 @tool 装饰器声明 schema 和行为。
handler 签名统一为 (input_data: dict, session: dict, events: list) -> str

通用工具(load_skill / todo_write / handoff_to_im / transfer_human)
在 skills/loader.py 中内置,这里只放业务专用工具。
"""

from __future__ import annotations
import json
import random
import threading
import time
from typing import Optional

# 从父级 loader 导入 @tool 装饰器
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from loader import tool


# ═══════════════════════════════════════════════════════════
#  后台任务:模拟客户在 5-12 秒后添加好友成功
# ═══════════════════════════════════════════════════════════

def _start_friend_check_thread(session: dict):
    """启动后台线程:模拟客户在 5-12 秒后添加好友成功"""
    def _check():
        time.sleep(random.uniform(5, 12))
        session["friend_status"] = "added"
    t = threading.Thread(target=_check, daemon=True)
    t.start()


# ═══════════════════════════════════════════════════════════
#  PhoneAgent 工具
# ═══════════════════════════════════════════════════════════

@tool(
    name="send_friend_request",
    description=(
        "发送添加微信好友请求。后台每5秒检测是否添加成功。"
        "最多3次(由权限层强制)。如果客户改了手机号,传 phone 参数更新。"
    ),
    schema={
        "type": "object",
        "properties": {
            "phone": {
                "type": "string",
                "description": "客户微信绑定的手机号(仅当客户更正了号码时传入)",
            },
        },
    },
)
def send_friend_request(input_data: dict, session: dict, events: list) -> str:
    # 计数(限流由 ToolDef.invoke 统一处理,这里只记录)
    count_key = "_tool_call_count_send_friend_request"
    session[count_key] = session.get(count_key, 0) + 1
    count = session[count_key]

    # 如果客户更正了手机号,更新 session
    new_phone = (input_data.get("phone") or "").strip()
    phone_note = ""
    if new_phone and new_phone != session.get("customer_phone", ""):
        session["customer_phone"] = new_phone
        phone_note = f"(已更新手机号为 {new_phone})"

    if session.get("friend_status") != "added":
        session["friend_status"] = "pending"
        _start_friend_check_thread(session)

    target_phone = session.get("customer_phone", "")
    tail = target_phone[-4:] if len(target_phone) >= 4 else target_phone
    output = (
        f"已发送添加好友请求(第 {count} 次),目标手机号尾号 {tail}"
        f"{' ' + phone_note if phone_note else ''}。"
        f"后台正在每5秒检测一次是否添加成功。"
    )

    events.append({
        "type": "tool_detail",
        "name": "send_friend_request",
        "detail": f"发送好友请求(第{count}次),尾号{tail}{phone_note}",
        "output_preview": output,
    })
    return output


@tool(
    name="check_friend_added",
    description="检查客户是否已添加微信好友。返回 added=true/false。added=true 时必须立即同轮调用 handoff_to_im。",
    schema={
        "type": "object",
        "properties": {},
    },
)
def check_friend_added(input_data: dict, session: dict, events: list) -> str:
    status = session.get("friend_status", "pending")
    added = status == "added"
    result = {"status": status, "added": added}
    if added:
        result["next_action"] = (
            "好友已添加成功,请立即在同一轮:"
            "1) 输出 text 告诉客户加上了;"
            "2) 调用 handoff_to_im 工具(按 handoff_schema 填 payload)"
        )
    output = json.dumps(result, ensure_ascii=False)
    events.append({
        "type": "tool_detail",
        "name": "check_friend_added",
        "detail": f"好友状态: {status}",
        "output_preview": output,
    })
    return output


# ═══════════════════════════════════════════════════════════
#  IMAgent 工具
# ═══════════════════════════════════════════════════════════

@tool(
    name="upload_driving_license",
    description="上传行驶证照片进行车辆审核。返回 passed(是否通过)和 reason(失败原因)。",
    schema={
        "type": "object",
        "properties": {
            "license_image": {
                "type": "string",
                "description": "行驶证照片(图片标识/base64/URL)",
            },
        },
        "required": ["license_image"],
    },
)
def upload_driving_license(input_data: dict, session: dict, events: list) -> str:
    # 模拟审核:默认通过。接生产时替换为真实 OCR + 规则引擎
    result = {"passed": True, "reason": "行驶证审核通过,车辆符合办理条件"}
    output = json.dumps(result, ensure_ascii=False)
    events.append({
        "type": "tool_detail",
        "name": "upload_driving_license",
        "detail": "行驶证审核:通过",
        "output_preview": output,
    })
    return output


@tool(
    name="submit_order",
    description="提交订单。order_data 必须包含 name/id_card/address(权限层强制校验)。",
    schema={
        "type": "object",
        "properties": {
            "order_data": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "id_card": {"type": "string"},
                    "address": {"type": "string"},
                },
            },
        },
        "required": ["order_data"],
    },
)
def submit_order(input_data: dict, session: dict, events: list) -> str:
    order_data = input_data.get("order_data", {})
    # 生成订单号
    order_id = f"ORD_{abs(hash(json.dumps(order_data, sort_keys=True))) % 100000:05d}"
    output = f"订单提交成功,订单号: {order_id}"

    # 记录会话终态
    session["order_submitted"] = True
    session["order_id_final"] = order_id
    session["order_data_final"] = order_data

    events.append({
        "type": "order_submitted",
        "order_id": order_id,
        "order_data": order_data,
    })
    return output
