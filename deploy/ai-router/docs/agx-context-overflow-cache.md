# WorkBuddy 长上下文转移与 AGX 前缀缓存

本说明记录 2026-09-07 本轮实现的路由和缓存行为。代码及 CPU 检查已经完成；AGX 与 Router 的生产部署状态由主代理确认。本文不代表 AGX 已通过真实缓存命中验收。

## NX3 容量边界

WorkBuddy 使用共享模型 `siyuan/qwen36-shared` 时，NX3 的安全上下文容量为 **57,344 tokens**。Router 判断容量时计入输入与输出预留，不能把 57,344 当作仅输入的额度。

- 所需上下文在 NX3 容量内时，继续使用 NX3；NX3 忙时排队，不因忙碌改去 AGX。
- 所需上下文超过 NX3 容量时，才选择具备相应容量的 AGX。

上下文转移不会让不同后端自动共享内存中的 KV 状态。AGX 必须在自己的模型实例上建立可复用的状态。

## 提前准备 AGX 前缀

当真实请求的 `prompt_tokens >= 45000`，且 NX3 已成功接收该请求时，Router 尝试在后台准备 AGX 缓存：

1. 尝试取得 AGX 的空闲容量 lease。AGX 忙时直接跳过，不等待，也不阻塞当前 NX3 请求。
2. 向 AGX gateway 的 `/cache/prepare` 发送该请求原有的 routed body，不追加测试问题，不生成回答。
3. gateway 解析真实固定前缀，在 AGX 本地完成前缀计算和快照保存。此准备过程本身仍有计算成本，只是有机会在后续转移前完成。

后台准备保持单任务执行，并按客户端与 worker 的组合限频 **120 秒**。达到 45,000 tokens 是尝试预热的条件，不是立即转移到 AGX 的条件，也不是缓存已就绪的保证。

## 复用范围与冷启动

精确磁盘快照要求前缀 token 一致；新增的原生复用修复允许快照键变化时仍由 llama.cpp 按真实共同前缀与可恢复 checkpoint 复用内存状态。后续变化的用户内容、动态尾部和新增上下文仍需要正常计算；预热不会消除这些成本。

以下情况仍可能走冷计算：

- 请求长度突然增加，在后台准备完成前就超过 NX3 容量。
- AGX 忙碌、限频或已有后台任务，导致本次准备被跳过。
- 固定前缀发生变化，或者所需的有效快照尚不存在。

AGX 的磁盘快照属于其本机运行时。NX3 的二进制、内存状态和快照不能直接视为 AGX 可复用的缓存。

## 本轮证据与验收边界

- Router 与 `/cache/prepare` 相关的 **68 项 CPU 测试通过**。
- AGX 已完成隔离构建审计，仅重新编译 server-context 对象并重链独立的 server library；保留 Q8 target KV、f16 draft KV、MTP 参数及 8 个 checkpoint 的预算。
- AGX 阶段库路径：`/data/agx-runtimes/qwen36-prefix-cache-stage-20260907/lib/libllama-server-impl.so`。
- 阶段库 SHA256：`4619e0827ae6abddd8345cedb9bed7bff0a72997a29849f7eb557a7233247661`。
- 构建、基线、CPU checkpoint 检查及静态 ABI 证据位于该阶段目录的 `reports/` 下。

本轮没有重放旧请求，没有执行推理测试，也没有提交代码。CPU 测试和构建审计证明相应代码检查与构建结果，不证明 AGX 在真实迁移请求中的命中率、首输出延迟或恢复效果。生产上线状态及后续自然请求的实际结果，等待主代理确认。

## 2026-09-07 上线结果

AGX 原服务 `agx-cerebellum.service` 已串行重启，原参数不变，后端监听 `127.0.0.1:18080`，认证缓存网关保持外部 `8080`。网关已设置开机启动。NX3 未重启。

两个 Router API 实例均使用镜像 `sha256:cd65a51ee066284d41561b1e31015252857577200358a875d70f54778f39e969`，状态 running，draining=false。构建基于上一生产镜像，只叠加本项更改；首次内部实例因混入并行 LMCache 配置校验依赖未启动，已回退、剔除依赖并重新上线，外部实例在该失败期间保持原版。其他任务的工作树改动保留。

使用线上 Redis 配置和实际健康状态做不触发推理的路由检查：53248 prompt + 4096 output → NX3；55619 + 4096 → AGX，原错误容量请求不再被 NX3 硬绑定拒绝。检查程序在已打印两个成功决策后，调用不存在的 HealthMonitor.close 产生清理异常；该独立诊断进程退出，没有用户推理请求。

AGX 尚未通过本轮真实请求证明缓存命中或测出首字延迟改善；本轮按用户要求没有旧请求重放。阈值以上的后续实际 NX3 请求会在符合条件时触发准备。模型、模板或固定前缀改变、准备未完成、AGX 忙等情况可能冷计算，不保证所有切换免 prefill。

回滚 Router：恢复上一镜像 `sha256:e68d5000811b02b6667344f6cdea7ba8af25d2b8d4cfa306c0d4d59b9184cf04`，逐个排空两个 API 实例再更新。AGX 回滚：维护窗口内停用 gateway，移除本次新增的 `/etc/systemd/system/agx-cerebellum.service.d/zzz-prefix-cache.conf`，daemon-reload 后重启原服务；其他 drop-in 和原二进制均保留。

后续真实流量结果与网关清空状态问题的修复见 [native-prefix-reuse-fix.md](native-prefix-reuse-fix.md)。该文档也记录当前部署和仍待验证的边界变化分支；上文初次上线时的未验证状态属于当时记录。
