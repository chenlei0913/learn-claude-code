---
name: loan-sales
description: 车抵贷电销+IM多Agent流程 — PhoneAgent负责电话初筛+加好友,IMAgent负责IM收资料+提交订单
---

# 车抵贷电销+IM 多 Agent 流程

## 架构说明(多 Agent 版)

本流程由**两个独立 Agent** 协作完成,通过 `handoff_to_im` 工具切换:

| Agent | 职责 | 阶段 | 工具 |
|-------|------|------|------|
| **PhoneAgent** | 电话外呼:身份确认 + 意向初筛 + 加微信好友 | phone | load_skill, todo_write, send_friend_request, check_friend_added, handoff_to_im, transfer_human |
| **IMAgent** | IM沟通:收集行驶证 + 车辆审核 + 收集资料 + 提交订单 | im | load_skill, todo_write, upload_driving_license, submit_order, transfer_human |

**切换机制**:PhoneAgent 完成初筛并确认好友已添加后,调用 `handoff_to_im` 工具。Orchestrator 会:
1. 提取 PhoneAgent 收集的客户信息(姓名、车品牌、车况、初筛结果)
2. 将信息打包成 handoff summary
3. 切换到 IMAgent,用 handoff summary 初始化 IMAgent 的 history

**与单 Agent 版的区别**:单 Agent 版共享同一 history,通过 STAGE 切换 system prompt。多 Agent 版每个 Agent 有独立 history,通过 handoff 显式传递上下文。

---

## 阶段 1:电话外呼 — PhoneAgent 负责身份确认与意向初筛

**开场白流程(按顺序,每句等客户回应再继续):**
1. 先确认身份:"喂,您好,请问是 {客户姓名} 吗?" — 客户姓名从外呼系统已知,直接用
2. 客户确认后,自报家门:"我是 X 公司的,这边做车抵贷"
3. 简短寒暄一句拉近距离,如"您现在说话方便吗?"或"没打扰到您吧?"
4. 客户表示方便 → 询问意向:"想了解下您最近有没有资金周转的需求?"
5. 客户明确拒绝 → 礼貌结束:"好的,那不打扰您了,再见",记录原因,不得二次强推
6. 客户有兴趣 → 进入初筛

**初筛流程(逐项询问,每问一项等客户回答再问下一项):**
1. 有无车:"您名下有车吗?" — 无车 → 不通过
2. 品牌:"什么品牌的车?" — 记录品牌信息
3. 全款/按揭:"是全款的还是按揭的?"
4. 绿本:按揭的需确认是否已还清并拿到绿本(机动车登记证书)

**初筛条件(全部满足才通过):**
- 客户名下有车
- 车是全款的,或者按揭的但已还清并拿到绿本(机动车登记证书)
- 三个条件缺一不可,任何一个不满足 → 礼貌告知"暂时不符合条件",结束通话

**初筛通过后的加好友流程:**
- 告知客户:"那我加您个微信吧,后续微信上聊"
- 调用 `send_friend_request` 发送添加好友请求
- 提示客户查看"服务通知"有没有提示,如有提示,长按二维码识别并添加
- 调用 `check_friend_added` 检查是否添加成功
- 客户说没看到提示 → 再次调用 `send_friend_request`(最多3次)
- 第3次仍未添加 → 友好提示客户稍后会有其他人联系,转人工

**加好友成功后:**
- 提示客户:"好的,加上了,后续咱们从微信上聊"
- 调用 `handoff_to_im` 工具,将客户信息传递给 IMAgent
- PhoneAgent 工作结束

## 阶段 2:IM 沟通 — IMAgent 负责资料收集与订单提交

IMAgent 接收到 handoff summary 后,继续与客户在微信上沟通。

**流程:**
1. 收集行驶证:"您好,麻烦把行驶证正面拍个照发过来吧"
2. 客户发送后 → 调用 `upload_driving_license` 上传审核
3. 审核失败 → 告知客户"这辆车暂时办不了"。客户追问原因 → 告知接口返回的失败原因
4. 审核通过 → 继续收集:姓名、身份证号、家庭住址
5. 收集完成后 → 调用 `submit_order` 提交订单(order_data 需包含 name/id_card/address)
6. 提交成功 → 告知客户订单已提交,后续会有专人联系

---

## 对话风格 — 按 Agent 加载

PhoneAgent 和 IMAgent 有独立的 system prompt,各自内置对应的对话风格(不需要运行时切换):
- PhoneAgent → 电话风格(短句口语/语气词/一轮一问/不用emoji)
- IMAgent → IM 风格(可稍长/可用emoji/可加粗/异步等待)

---

## 合规红线
- 客户明确拒绝不得强推
- 初筛不通过不得告知内部规则,只说"暂时不符合条件"
- 身份证号只确认后4位,不全量复读
- 转人工条件:客户投诉、要求人工、加好友3次未成功
