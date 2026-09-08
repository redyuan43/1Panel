# AI 逐请求缓存计数与路由审计

## 页面解释

在路由审计选择请求，点击“模型执行”。时间线和详情顶部使用相同结论：

| 结论 | 证据 |
|---|---|
| 已命中 | 成功完成的同次执行，输入和缓存计数完整且有效，缓存大于零 |
| 未命中 | 同上，但后端明确报告缓存为零 |
| 仅有估算 | 只有全局计数差值，不能确认该请求命中 |
| 数据不足 | 缺失、非法、取消、未完成或旧记录缺少明确来源 |
| 执行中 | 尚未完成，不提前判断命中 |

AI 的“后端缓存复用”是 vLLM 汇总的本地与外部缓存计数，不拆成 GPU / LMCache 命中。未复用输入 = 总输入 − 缓存复用；它不代表包含抢占重算、预热和重试的全部 prefill 工作量。首个输出等待包含入口处理及排队，不能当作 prefill 耗时。非流式缺少首输出计时则显示后端未提供。

NX3/AGX 继续提供固定边界、准备阶段扣除后的净复用和实际 prefill；AI 缺少这些阶段，默认折叠并解释缺失原因。后台统计的后端逐请求复用比例与净复用分开统计，显示有效样本数、中位数、P95 和趋势。旧记录不会追溯改成实测。

## 实现与兼容性

仅 AI 端点 metadata.cache_usage=per_request 启用内部流式 usage。Router 在原有 stream_options 上设置 include_usage=true，私有累计器先采集，再删除客户端未请求的 usage-only 帧；内容、工具调用和 DONE 保留。公共请求方式不变。

支持 Chat prompt_tokens_details 和 Responses input_tokens_details；原始 usage 在协议适配前记录，防止适配器补零污染证据。只使用最后一次执行尝试，不借用失败尝试。布尔值、负数、非整数、非有限值、冲突别名及缓存大于输入都不能证明命中。

流取消及无终态 EOF 不升级成成功。Responses incomplete/failed 状态在 DONE 之后仍保留“不完整”证据；正常截断的公共响应格式不变，缓存结论保持未知。

新增详情字段：cache_status、cache_reason、cache_measurement、backend_input_tokens、backend_cached_tokens、backend_reuse_ratio、backend_usage_source、uncached_input_tokens、estimated_cached_tokens、estimated_reuse_ratio、input_measurement。计数来源和 backend_usage 同时写入现有请求审计，原文不写入这些字段。

## vLLM 补丁

运行参数增加 --enable-prompt-tokens-details。scripts/patch-vllm-cache-usage.py 只把两个响应层 truthiness 条件改为 is not None；零明确输出，未知仍未知。推理和缓存策略不变。

支持版本 1.5.0，原 serving.py SHA256：
bf051be6b517820003c02b81f5c579c4ecda4c9dc7886e3da0a7cae62b240a69

补丁后 SHA256：
80d7bde046ff56a73b96d70ddbba4132cdd26865e5a38611be95a0a4981a0502

工具支持 apply/check/rollback，保留 serving.py.cache-usage-original，原子替换并拒绝未知版本、哈希或独立修改。外层启动脚本会校验补丁，升级 vLLM 后需要重新审查适配，不能静默套用旧补丁。

## 验证记录

隔离目录：/home/ai/github/1Panel/experiments/ai-cache-usage-20260908。

- 77 项原有契约测试通过。
- 20 项新增独立 CPU 契约通过，含流式分片、LF/CRLF、客户端 usage 偏好、重试、取消、EOF、非法计数及原生/适配 Responses 完成状态。
- 3 项响应补丁测试通过；另用当前安装源码的副本验证 apply/check/rollback 与哈希，未进行模型调用。
- 11 项相关 core 流式/usage 测试、9 项路由诊断策略测试通过。
- 原有审核回归通过；EOF 计时测试的桩补齐中断记录接口，继续确认缓冲尾部输出有计时。
- 浏览器缓存审计 10 项、路由诊断 7 项通过；包括五种结论、计数比例、详情折叠、刷新状态、原文按需读取、原有审核与策略功能。
- CPU 和浏览器数据是合成夹具，不是缓存提速的真实推理证据。后续 WorkBuddy 请求需逐条核对最终 usage、审计与页面；不重放旧对话。

## 上线及回滚

本次镜像：1panel-ai-router:ai-cache-usage-20260908-r2。具体旧镜像、进程、步骤和时间见实验目录 deployment.json；备份位于 deployment-backup，含启动环境、响应源码与本次改动前文件。未 commit / push。

上线先 drain 两个 Router API，等待 Router 和 vLLM 的在途计数均为零；保存备份、安装补丁，只 restart 用户服务 qwen38-v100-tp2-vllm.service。LMCache 的进程 ID 与健康状态必须保持；不 restart LMCache，不删除缓存文件。模型健康后更新两个 API 和两个 Control，恢复接单。

回滚同样先 drain 并等待结束。按 deployment.json.old_images 将旧镜像重新标记到各服务镜像名，再用 docker compose up -d --no-deps --no-build --force-recreate 对应服务。不要仅依赖同镜像 up 来解除 drain，必须重建或重启进程。

如果回滚 vLLM 响应补丁，先恢复备份的两个 run-qwen38-v100-tp2 启动脚本，再执行 patch-vllm-cache-usage.py rollback --package-root /home/ai/venvs/1cat-vllm-1.5.0-lmcache/lib/python3.12/site-packages/vllm，最后只 restart qwen38-v100-tp2-vllm.service 并检查 /health 和 LMCache /status。不要直接覆盖后续任务修改过的文件，补丁工具会拒绝源码哈希变化。

GPU 热缓存可能随模型重启失效；LMCache 保持运行不等于保证每个新请求命中。

## 本次线上结果

- 已完成维护更新；AI vLLM 新 PID 2222966，LMCache 前后 PID 均为 237071，健康检查通过。
- 两个 Router API 与两个 Control 均运行镜像 SHA256 c4f8d95d468952ebfc1eb1f74136781f5f6514e51b8cfa278ef13a27b7faf887；两路入口 draining=false。
- 线上 HTTPS 页面检查无浏览器错误；原请求 72081194d34b40beab6cfb46366e79b6 显示“仅有估算 · 0 tokens / 0.0%”，来源为全局计数差值，原生详情默认折叠。未授权缓存查询返回 401。
- 根工作区与部署候选的本次文件逐一相同，git diff --check 通过。其他任务新提交 275f54111 已在隔离工作树快进合入，未覆盖其改动。
- 截至上线后检查，新的 WorkBuddy 请求样本为 0；真实 usage—审计—页面三方计数验收仍待下一条正常请求，不能把 CPU 夹具结果称为线上缓存命中或提速证据。

## 完成开发与提交前 review

审查确认并修复了 Chat 错误对象没有 type 字段时，后续 DONE 可能让已收到的缓存计数被当作完成请求证据的问题。原生流保留错误并将 usage 标为不完整；Responses 适配器遇到上游错误不再生成成功终态。新增两项回归后，22 项专项 CPU 契约全部通过。

review 修复镜像为 1panel-ai-router:ai-cache-usage-20260908-r3，仅更新两个 Router API。Control 保持 r2，页面代码未再次修改。具体上线与回滚镜像见 review-deployment.json；本轮 vLLM / LMCache 均未重启。

真实请求 d766dc2205604bf7a9162c9de81a59f6（2026-09-08 11:21:12，WorkBuddy）被自动路由到 Edge：输入 47,234 tokens，估算复用 24,000（50.8%），首输出 24.11 秒，总耗时 27.81 秒。线上页面与审计一致，明确显示估算而非实测。此记录验证了展示和来源区分，不能替代 AI vLLM 新请求的实际命中验收；后者仍等待自然到达的请求。本轮没有额外模型调用或旧对话重放。
