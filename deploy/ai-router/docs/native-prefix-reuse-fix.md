# 修复网关清空原生前缀状态

2026-09-07，真实 WorkBuddy 请求在 AGX 超限续接后第二条仍冷算：

| Router request ID | 固定边界 | 网关准备 | 网关首字等待 |
|---|---:|---:|---:|
| d78c001103e24450a786e190e5cc643c | 40647 | 69.448秒，新计算40647 | 89.823秒 |
| a1997669425945d9903d158dd1d1c418 | 51248 | 94.149秒，新计算51248 | 96.100秒 |

第二条 native task4919 的日志为 lcp=0、cached_sequence=0、checkpoints=0。原因是网关按末条 user 边界生成的 snapshot key 变化，prepare 未命中精确磁盘文件时先调用 `/slots/0?action=erase`，将 llama.cpp 原本可用于比较和恢复的状态清空。其后同一边界的真实第三、第四条可热恢复 checkpoint，首字等待7.620/1.606秒，说明不能把 `miss_saved` 或响应中的 cached_tokens 当作跨请求免预填充证据。

## 修改

新 snapshot key 未命中时，保留 slot，将完整的新前缀 token 交给原生 `/completion`，保持 n_predict=0、cache_prompt=true。llama.cpp 比较实际 token LCP，按可用 checkpoint 回退，只有需要的部分重新计算。缩短、分叉或无关输入由后端处理；没有可恢复 checkpoint 时仍可能冷算。快照数量与 RAM checkpoint 预算不增加。

只有实际尝试磁盘 restore 后失败时才清空，以避免 target/draft 部分恢复后的不完整状态。校验和或元数据失败还没触碰运行状态，保留原生缓存。新日志字段 `reused_tokens` 来自 native timings.cache_n，`prime_tokens` 来自 timings.prompt_n；`miss_saved` 只表示产生新磁盘快照，并不表示全冷或完全命中。请求原文、采样参数和响应字节保持透传。

提前准备删除对 Router prefix_affinity_key 的依赖。Router 的本地模板签名失败会给空 key，但 gateway 自身仍会使用原生模板、文本类型、唯一边界和 token 前缀一致性检查决定是否准备。仍保留 NX3 pin、45000阈值、AGX空闲、容量租约、单任务及120秒限频。此次AGX首条本身已经超限，因此不能把其首次冷算归因于这项签名门槛。

## 验证及范围

32项 CPU 回归通过，覆盖增长、缩短、分叉、无关输入、恢复失败、校验失败、持久化及签名缺失时的后台准备。未重放旧对话，未发送合成模型推理请求。CPU协议测试不能替代真实模型性能验收。

独立源码审查确认实际AGX实现按LCP选择checkpoint并同步裁剪target/draft。既存MTP pending_h没有随checkpoint序列化，可能影响draft接受率；主模型仍逐token采样验证draft。本轮不修改该原生实现，不宣称已完成MTP全部运行状态的恢复验收。

部署仅替换NX3/AGX Python网关及两个Router API的prefix_prewarm模块，模型进程不重启。部署状态以实验目录 native-reuse-fix/gateway-activation.json 和 router-activation.json 为准。原网关脚本、原Router镜像保留可回滚，不提交代码。

## 上线与自然流量观察

NX3与AGX网关均已更新到 SHA256 `2f11fcf3b8416ba6466eb0d36b3eda667f0dfdaf84982cf893b510dd6b273d07`；模型PID保持920/1979258，网关PID变为23443/2011757，健康正常。新镜像 `sha256:45ab4aaaa64714aeab8a7a4d01d8579f414352053fc08938eef71546817a956e` 仅在上一生产镜像上覆盖 prefix_prewarm.py，两个Router API均已上线，running且draining=false。外部Router排空曾被Hermes无限输出请求占用，经用户明确批准后结束该请求并完成更新。

子代理完成180秒只读观察。新AGX网关日志有1次disk、3次hot，网关首字等待26.140/8.123/1.919/1.935秒；原生有磁盘和内存checkpoint恢复证据。没有出现新snapshot key，因此本次修复的新key增量准备分支尚待真实请求验证；这些既有hot/disk不能冒充该分支验收。没有额外发送请求，详细证据见 real-traffic-observation.json/.md。

Hermes请求 `28aee7d2b9d341e3a27be36cad544da4` 的旧API进程停止后，非流式gateway连接仍占用native task5684。ss -K返回0但实际连接仍ESTAB，没有据此误判已取消；随后使用pidfd_getfd复制并校验gateway23443/fd5的准确socket地址(127.0.0.1:48204→127.0.0.1:18081)，shutdown该socket后原生slot已空闲。NX3模型PID920保持，AGX模型也未重启。没有终止其他模型任务。
