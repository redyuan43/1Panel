# WorkBuddy 工具续请求前缀一致性修复

2026-09-07；已接回 1Panel 的两个 Router API；本轮不提交代码。

## 原因与行为

入口原先要求最后一条消息必须是 user。首次请求经过重排，后续 assistant/tool 结尾的请求却保留原始 system 和动态工具说明，使同一会话的原生模板在第 12,706 个 token 提前分叉。

现在定位最近一条 user，并对 user、assistant、tool 结尾的请求执行同一规范化。沿用原 WorkBuddy 客户端、模型与结构识别；固定规则和工具 schema 在前，工作区记忆以及 Agent、Skill、ToolSearch 的完整动态说明移入最近 user 的动态区。后续 assistant/tool 消息、调用 ID、参数、结果、图片和顺序保持原样。

已有内存占位符不会再次搬移。重复处理不再插入内容；标记冲突、边界异常或不支持的 user 内容返回完整原请求，避免仅修改了 system/tools 的部分变换。公共 API 不变。

入口审计保留 workbuddy_dynamic_context_moved，并新增适用请求的 workbuddy_dynamic_context_skipped。记录 moved、moved_chars、mode、target_user_index、stable_prefix_sha256、skip_reason，不记录提示原文。哈希覆盖工具和最近 user 之前的消息 JSON，仅用于入口诊断，不能代替原生 token 或 KV 命中证据。

## 两条完整原始请求的离线验证

读取现有加密请求归档，在内存中解密；没有保存原始明文。对候选实现验证内容保全、历史顺序、工具 schema、图片内容和幂等性，再调用现有后端的 /apply-template 与 /tokenize。没有调用推理接口。

| 指标 | 修复前 | 修复后 |
|---|---:|---:|
| 第一条请求 token | 74,509 | 74,509 |
| 工具续请求 token | 74,968 | 74,827 |
| 两条共同前缀 token | 12,706 | 74,508 |
| 最近 user 的位置（从 0 开始） | 69 | 69 |

原始请求 ID：432be7f58d414112b835562dab1eaf67、e9a94d6e5dd747a6bb33231e3bbe2b82。两条重排后的稳定区哈希相同。上述数字是模板/token 结果，尚未作为实际缓存复用率或首字延迟验收。

## CPU 验证

- WorkBuddy 专项回归：33 项通过，覆盖三种结尾、两种动态格式、多模态、最近 user、重复处理、标记冲突与异常内容。
- 扩展 core、tool schema、prefix affinity、client deployment 回归：311 项中 288 通过，23 失败。
- 将 api.py/protocol.py 换回修改前版本，在隔离副本重跑这 23 项，全部同样失败，失败节点一致；网络保护记录 0 次连接尝试。这些是已有路由/配置相关失败，不由本轮两文件变更引入。
- 独立代码审查通过；发布镜像 import/Settings 检查通过；本轮文件 git diff --check 通过。

## 发布与恢复

2026-09-07 18:01:43 / 18:01:57（UTC+8）分别完成 router-api-local / router-api-tail 更新。更新前及 drain 后均无在途请求，更新后 /health 正常，实例 running、draining=false。

基于原运行镜像 sha256:0597ee14c0872ae85450db6d39f27ce88e44831818e8cd3e31da25c4aa9159e2 构建，仅覆盖 /app/ai_router/protocol.py、/app/ai_router/api.py（权限 0644），没有带入工作区的其他模块/配置改动。

新镜像：sha256:dfe5e1357e2a6cce54edad9e7c66f8d9854ae4cdf0189860d1b106fc73703f46
新标签：1panel-ai-router:workbuddy-continuation-20260907。
保留旧标签：1panel-ai-router:workbuddy-normalize-base-20260907。

回滚仍应先确认目标 API 无在途请求，再 drain，将旧标签 tag 回该服务默认镜像，并执行 docker compose up -d --no-deps --no-build <service>。本轮只更新两个 API，未重启模型、网关或其他服务。

更新前后 NX3 模型/网关 PID 为 920/23443，AGX 为 1979258/2011757，均未改变且 active。

## 磁盘缓存与尚待验收

NX3：/home/nx/.local/share/qwen36-prefix-cache，2,034,277,867 bytes（约 1.89 GiB）；预算最多 3 组、2 GiB。
AGX：/data/agx-runtimes/qwen36-prefix-cache，3,963,263,168 bytes（约 3.69 GiB）；预算最多 3 组、12 GiB。
本轮未调整缓存路径/预算或清理快照。文件不全部常驻内存；已有清理策略发生在保存后，写新快照期间仍可能临时高于保留预算。

真实缓存验收使用上线后的自然流量，需关联入口日志与后端实际 cache_n/prompt_n 及首字时间，确认固定前缀复用至少 95%。本轮不重放旧对话推理。若未出现新请求，保留“实际复用率与首字延迟待验收”，不得将 74,508 token 共同前缀描述成已经命中。

已知边界：本修复解决首次/工具续请求处理不一致；真实固定内容发生变化、动态格式无法识别、没有兼容 checkpoint/磁盘状态等情况仍可能重新 prefill。没有增加跨节点状态共享或保证任意模型切换都能复用状态。

证据目录：experiments/nx-prefix-cache-fix-20260907/workbuddy-normalize-fix/。包含原始请求脱敏验证、修改前失败对照、镜像清单、两次部署记录、模型状态核对与真实流量观察结果。


上线后真实流量观察：截至 2026-09-07T10:08:30.540145+00:00，未发现包含本轮新诊断字段的 WorkBuddy 请求；两个 API 均 running、draining=false、在途请求 0。因此实际缓存复用率和首字耗时仍待新真实请求验收。已结束本轮观察，未发起任何推理测试。
