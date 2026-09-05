# AGX Cerebellum MTP 快速验证

现场日期：2026-09-04。首轮用户将完整长测缩减为第一阶段快速筛选：
只比较原模型与 MTP2，不测试 MTP4、128K 或 256K 实际长输入，
不修改推理后端，不在本次测试后直接上线 MTP。

## 结论

补头工件成功生成，原主干的 733 个张量逐一保持字节与量化一致。
配置窗口维持 262144，但本次实际输入为约 4K 至 5K。
同一组四轮请求中，后面三轮解码速度的配对中位提升为 9.92%，
总响应耗时的配对中位下降为 6.92%。这只是单组短上下文初测。

MTP2 的四轮正常文本对话及工具调用通过，图像编码阶段触发 CUDA
虚拟地址空间预留错误并退出。因此不能用该配置替换当前多模态服务。
本次没有取得 256K 实际输入的 MTP 结果，也没有进行后端修复。

原模型已经恢复，同一张图及图片追问重新通过。换载维护总耗时为
98.237 秒，包含原服务恢复与验收；不包含事先下载、离线准备和基线采样。

## 工件与边界

- 原模型：`/data/models/Cerebellum-v1-Q3_K_M.gguf`。
- 原模型 SHA256：`136cd335ec534d78b3e5df1d3466328d4f1c4e0543338926d11a434a9c5bbc9b`。
- 预测头来自 `havenoammo/Qwen3.6-35B-A3B-MTP-GGUF`，
  revision `a529a1734ce45a423a27a399d462791201d6995b`，文件 `35BA3B-MTP.gguf`。
- 预测头大小 903319200 字节，SHA256：
  `fb16c34255b1a3bc52e64bd1a9d6f288c67670c0b3c9d8d067fff2c4deeca435`。
- 预测头通过 AI 主机已有 Xray 代理 `127.0.0.1:10808` 下载，再通过 SSH
  传到 AGX；两端 SHA256 均匹配。没有修改全局代理设置。
- 新工件位于
  `/data/agx-runtimes/cerebellum-mtp-20260904/Cerebellum-v1-Q3_K_M-with-MTP.gguf`，
  大小 12852932096 字节，SHA256：
  `8d5e6c152fd8cd04c36b304e286946ccc18dca6b51848c22583a5782e20f1d81`。
- 追加 20 个 `blk.40.*` 张量；只改变主文件的 `block_count=41` 并新增
  `nextn_predict_layers=1`。保留主模型 tokenizer、模板、身份和其他元数据。
  这是基于原版预测头的移植，不是声称其经过 Cerebellum 变体专门训练。

合并使用后端配套的 GGUF Reader/Writer，不执行第三方移植脚本。
逐张量验证描述符和 SHA256，验证原文件前后 SHA256 不变，
成功后才发布独立输出文件；已有输出和报告不能覆盖。

## 性能结果

两组均为单 slot、Q8 K/V、`-c 262144 -b 2048 -ub 512 -ngl 99`。
MTP2 仅改变模型文件并追加 `--spec-type mtp --spec-draft-n-max 2`；
保留同一 projector。测试实例只绑定 AGX 的 `127.0.0.1:18080`，
通过仅绑定 AI 本机的 SSH 隧道访问，没有增加公网入口。

使用原模型生成的固定四轮会话；MTP2 回放相同请求，逐轮检查请求哈希，
实际输入 token 数保持一致。采样 temperature=1、top_p=0.95、top_k=20，
seed=20260904。每轮固定生成 256 tokens，并仅在计时用例中忽略 EOS；
这部分不作为语义或自然结束验收。

| 轮次 | 输入 tokens | 原配置 TPS | MTP2 TPS | 原总耗时 | MTP2 总耗时 | MTP 接受率 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 4073 | 34.81 | 39.52 | 16.451s | 17.420s | 64.86% |
| 2 | 4349 | 34.66 | 36.65 | 8.173s | 7.866s | 55.37% |
| 3 | 4629 | 34.59 | 42.22 | 8.186s | 6.946s | 71.43% |
| 4 | 4905 | 34.52 | 37.94 | 8.238s | 7.668s | 60.43% |

第二至第四轮两组的缓存命中数均为 4069、4345、4625，
重新预填充数均为 280、284、280，没有整段历史重算。
这里读取的是后端 `timings.cache_n`，不是包含已生成内容的
`tokens_cached` 总序列长度。

首轮纯预填充从 8.386 秒增加到 10.921 秒。不能把首轮变慢、
缓存命中后的首字延迟、解码 TPS 和端到端耗时混为同一个速度指标。
单组 4K 数据不满足原计划的三组近 256K 验收门槛。

## 图片失败与恢复

正常结束的独立语义用例，不忽略 EOS：

| 检查 | 原配置 | MTP2 | 恢复后原配置 |
| --- | --- | --- | --- |
| 四轮记忆与计算 | 4/4 | 4/4 | 4/4 |
| 指定工具及参数 | 通过 | 通过 | 通过 |
| 256x128 合成图片左右颜色识别 | 通过 | 崩溃 | 通过 |
| 图片内容追问 | 通过 | 未执行 | 通过 |

MTP2 图片请求完整 ID：`471b803fe74b4551be8e946ef3bd0de8`。
2026-09-04 20:32:21 CST，task 507，在 `encoding image slice` 后的首个 fatal：

```text
CUDA error: out of memory
current device: 0, in function alloc at ggml/src/ggml-cuda/ggml-cuda.cu:526
cuMemAddressReserve(&pool_addr, CUDA_POOL_VMM_MAX_SIZE, 0, 0, 0)
```

该后端源码的 `CUDA_POOL_VMM_MAX_SIZE` 为 `1ull << 35`，即 32 GiB。
实际失败发生在虚拟地址预留调用，不应仅凭 OOM 字样断言物理内存耗尽。
进一步区分虚拟地址空间、分配器或平台限制尚未进行。
系统记录 `Result=core-dump`、`status=6/ABRT`；未为了通过测试而移除图像能力。

预填充采样期间 GPU 约 51 摄氏度，vmstat 连续样本没有换入换出；
这是有限采样，不代表完整内存性能分析。保留原始采样与启动日志。

恢复状态：

- `agx-cerebellum.service` 恢复原模型和原始 256K drop-in，
  PID=3125768、`active`、`NRestarts=0`。
- 测试进程已停止，测试端口和 SSH 隧道已关闭，恢复定时器已取消。
- 路由在测试开始前已被其他操作停用，revision=5。
  测试前后均为 `enabled=false, auto_candidate=true, revision=5`，
  本次没有调用端点写 API、重启 Router 或重新启用该端点。
- 原服务四轮文本、工具、同图识别、图片追问共 7 项重新通过。
- 没有修改 GPU 功耗设置、后端源码、原模型、生产 unit 或其他节点；
  没有提交或推送 Git 改动。

## 复跑工具与证据

工具在 `scripts/`：

- `prepare-cerebellum-mtp.py`：验证预测头来源、结构和原张量，输出独立 GGUF。
- `benchmark-cerebellum-mtp.py`：`record` 固定会话、`replay` 相同输入、
  `natural` 使用实际输出续轮、`compare` 配对汇总。
  三种推理模式均是固定长度计时，不替代正常 EOS 语义验收。
- `validate-agx-mtp-quick.py`：默认运行 MTP2 快速维护验证，
  `--text-matrix` 则重新采集无 projector 的基线并串行测试 MTP2、MTP4；
  要求端点已停用、单 slot 空闲、独立端口可用，原服务恢复在 finally 中执行；
  模拟启动失败的单测覆盖恢复路径。还设置独立恢复定时器和测试进程时限。

所有在线探针都要求 `--execute`；长测仍需单独确认窗口。
本次基线单请求超时 90 秒，MTP2 回放为 60 秒；
后端错误、缓存丢失、非固定生成长度不会记作通过。
`compare` 对不同请求、自然续轮混入固定回放、缺少三组或非近 256K 数据
不会给出正式性能验收通过；它永远不会自动批准部署。

离线测试：

```bash
cd "/home/ai/github/1Panel/deploy/ai-router"
PYTHONPATH="/home/ai/llama.cpp-github/gguf-py" pytest -q \
  tests/test_cerebellum_mtp.py tests/test_mtp_benchmark.py tests/test_mtp_quick.py
```

本次 28 项通过，包含真实小型量化 GGUF 的合并与逐字节验证。

持久证据根目录：
`/home/ai/.local/state/ai-router-acceptance/20260904-agx-mtp/`。

- `prepare-report.json`：所有主干与预测头张量的哈希清单。
- `quick-baseline/`：基线四轮请求、SSE、计时和固定会话。
- `quick-trial/mtp2/`：相同请求的 MTP2 四轮响应与计时。
- `quick-trial/*-smoke/`：基线、MTP2、恢复后的语义和图片结果。
- `quick-trial/report.json`：完整维护结果、启动参数、路由前后快照及测试 journal。
- `quick-trial/telemetry-start.log`、`vmstat-start.log`：硬件采样。
- `quick-comparison.json`：配对提升、样本限制及未通过的正式验收门槛。

## 第二轮纯文本对照：因并发干扰中止

用户随后批准纯文本 off / MTP2 / MTP4 三组短测。新增 `--text-matrix`
模式：三组均不加载 projector，实际输入约 4K，分别使用分析和代码的
固定四轮会话；新基线生成 fixture，候选严格回放相同请求。
只在原服务维护前和恢复后测图片，候选只做文本和工具语义检查。
相关离线测试现为 33 项通过，包括三档参数、跳过图像及两种模式的失败恢复。

本轮未完成，不能作性能对照结论：

- 分析基线后三轮为 34.72、34.49、34.35 TPS。
- 代码基线后三轮为 34.58、34.39、23.15 TPS。
- 代码第四轮 request ID：`c44c997ceeda4da382cfa0bc6e858889`，
  task 1821，运行于 2026-09-04 21:01:15 至 21:01:26 CST。
  缓存复用 4629 tokens，只预填充 284 tokens，解码 256 tokens 耗时
  11.059 秒；不是整段历史重新预填充。
- 同时另一操作在 21:01:14 启动 `agx-hymt-translate.service`，
  PID 3148208，GPU 上加载 `Hy-MT2-1.8B-Q4_K_M`，并实际执行多个请求。
  其中 task 103 在 21:01:20 至 21:01:24 推理，与上述降速轮次明确重叠。
  这证明本轮缺少独占条件，资源竞争是合理解释，但未量化各项开销占比。
- MTP2 尚在模型加载阶段时中止，未取得计时结果；MTP4 未启动。
  加载阶段还观察到 `wait_on_page_bit_common` 和 I/O wait，
  不将加载等待直接归因于 MTP 解码或翻译进程。

操作者向本轮驱动发送 SIGTERM，触发已测试的 finally 恢复路径。
总维护耗时 242.113 秒，报告为 `passed=false, restored=true`。
原服务 PID 3151147，`active/running, NRestarts=0`，恢复后的 7 项检查通过。
本轮 baseline/MTP2 临时实例和恢复定时器均 inactive；
路由继续保持 `enabled=false, auto_candidate=true, revision=5`。
没有停止翻译服务，没有改变生产配置，也没有上线纯文本 MTP。

原始证据位于上述根目录的 `text-only-matrix/`：
`report.json`、`baseline/variant.json`、`baseline/{analysis,code}/`、
`mtp2/variant.json`、`restoration-smoke/` 及 `concurrent-translation.log`。
后续必须先协调 AGX 无其他推理、加载或部署活动的窗口，重新采集三组；
不得把本轮被并发活动拖慢的基线用于计算加速收益。

## 第三轮纯文本筛选：off / MTP2 / MTP4

证据目录 `text-only-matrix-r2/`。三档的固定输入计时、缓存复用及独立
5 项文本/工具语义检查均通过，原服务恢复后 7 项检查通过。
维护耗时 427.356 秒；未部署 MTP。

以下均为缓存命中的第 2、3、4 轮 TPS，实际输入约 4K 至 5K：

| 配置 | 分析任务 | 代码任务 |
| --- | --- | --- |
| off | 34.92 / 34.82 / 34.36 | 34.93 / 34.75 / 34.66 |
| MTP2 | 35.83 / 40.74 / 37.77 | 33.75 / 35.75 / 34.34 |
| MTP4 | 30.39 / 33.07 / 32.15 | 28.07 / 29.27 / 33.01 |

六个暖轮次配对汇总：MTP2 解码中位提升 2.74%，总耗时中位下降 1.25%；
MTP4 解码中位下降 9.70%，总耗时中位增加 10.87%。
MTP2 的分析接受率为 55.4% / 71.4% / 60.4%，代码为
48.1% / 53.3% / 50.0%；MTP4 分别为 36.1% / 41.1% / 39.4% 和
30.9% / 33.5% / 41.4%。更深的预测没有在当前短文本负载中产生收益。

重要环境限制：没有观察到翻译请求重叠，但另一操作者在
2026-09-04 21:16:26 至 21:16:27 停止了翻译服务，释放了其 GPU 内存。
因此此次 `passed=true` 仅表示技术/语义检查通过，不证明严格独占环境、
统计显著提升或最终最优。比较脚本的正式近 256K 性能门槛仍未通过。

下一步增加 MTP1，随后用较长输入复验优胜者。用户明确允许纯文本方案，
不再要求最终候选保留多模态；恢复原服务时仍检查原有图片能力。
`--depths` 支持 0/1/2/4，`--prompt-tokens` 支持 4096/42000/128000/260000，
`--workloads` 可选分析、代码、运维。长测需显式指定维护窗口，
试验进程时限始终早于独立恢复定时器。

首次 MTP1 尝试位于 `text-only-mtp1/`：
翻译服务于 21:21:30 重新加载，21:21:31 至 21:21:32 实际执行请求，
驱动检测后主动失败并恢复，维护耗时 76.014 秒，恢复检查 7/7。
MTP1 尚未启动，不能从该目录得出 MTP1 性能结论。

并发保护随后加强：除推理/加载外，也将翻译服务启动和停止视作无效环境变化，
并在维护前要求最近 60 秒没有上述活动。当前 41 项离线测试通过，
包含 MTP1 参数和比较门槛、并发推理及服务生命周期变化拦截。
此保护针对已确认的干扰服务，不等同于覆盖所有 GPU 进程的全局独占锁。

## MTP1 短文本复测通过

证据目录 `text-only-mtp1-r2/`。本轮翻译服务监控为 `-- No entries --`。
两档分别完成分析、代码固定四轮计时和 5 项正常文本/工具检查，
恢复原服务后 7 项检查通过，维护耗时 251.832 秒。

| 配置 | 分析任务暖轮 TPS | 代码任务暖轮 TPS |
| --- | --- | --- |
| off | 34.90 / 34.75 / 34.55 | 34.88 / 34.76 / 34.67 |
| MTP1 | 38.48 / 40.14 / 40.99 | 39.83 / 37.66 / 37.67 |

六个暖轮次的配对解码中位提升 12.23%，总耗时中位下降 8.56%。
分析接受率为 68.9% / 77.6% / 80.9%，代码为 74.7% / 64.9% / 64.9%。
这支持将 MTP1 作为下一步长输入候选，但不能证明全任务或全上下文最优。
首轮分析总耗时从 15.699 秒增加到 17.679 秒，
代码首轮从 15.933 秒变为 15.812 秒；不能只报告暖轮收益而隐藏冷输入代价。

随后安排约 42K 输入的 off/MTP1/MTP2 对照。
首次 `text-only-42k/` 在本地端口绑定预检失败，未停止原服务；
确认无残留隧道后，预检 socket 增加 `SO_REUSEADDR`，允许已关闭连接的
端口复用但不会占用现有监听器。对应失败恢复测试已重新通过。

## 42K 结果与生命周期验收缺口

证据目录 `text-only-42k-r2/`。off 和 MTP1 阶段未观察到翻译服务活动，
固定输入及正常短文本/工具检查通过；MTP2 阶段翻译服务于
2026-09-04 21:36:57 至 21:36:58 停止，整体矩阵因此判失败。
总维护耗时 680.908 秒，原服务恢复后的 7 项检查通过，路由保持停用。
MTP2 的计时数据不得用于宣称三档严格同环境对照成功。

| 配置 | 暖轮 TPS | 暖轮总耗时 |
| --- | --- | --- |
| off | 22.88 / 23.00 / 22.91 | 12.20 / 12.14 / 12.23s |
| MTP1 | 30.57 / 31.12 / 30.90 | 9.51 / 9.34 / 9.43s |

实际首轮输入 41998 tokens。两档后三轮缓存复用均为
41994 / 42270 / 42550 tokens，只预填充 280 / 284 / 280 tokens。
基线暖轮预填充约 0.94 秒，仍只有约 23 TPS，说明这种长历史解码变慢
不是整段历史重算。MTP1 配对解码中位提升 34.87%，总耗时中位下降 22.88%；
单组比较文件为根目录 `42k-mtp1-comparison.json`，未通过正式近 256K 门槛。
冷首轮总耗时为基线 105.946 秒、MTP1 116.179 秒。

**后续生命周期审计发现：此前的推理通过不等于候选完整验收通过。**
多个纯文本 MTP 实例在收到停止命令后的清理阶段发生
`double free or corruption (!prev)`，systemd 记录
`Result=core-dump, ExecMainCode=3, ExecMainStatus=6`。
例如 `agx-cerebellum-mtp-quick-c2c21138-mtp1.service` 在
21:35:32 完成工具请求并开始退出，21:35:34 报上述错误。
因此保留旧报告作为原始证据，但撤回其 `passed=true` 能代表完整候选成功的解释；
只有推理/语义和原服务恢复检查通过，候选正常停止存在缺陷。

现场源码存在明确的释放顺序风险：

- `tools/server/server-context.cpp` 的 `destroy()` 先执行 `llama_init.reset()`，
  然后才销毁 `slot.spec`。
- `common/speculative.cpp` 的 MTP 析构函数仍调用
  `llama_set_mtp(ctx_tgt, nullptr)`，需要尚未释放的主上下文。
- `src/llama-context.cpp` 的 `set_mtp()` 会访问上下文字段并释放 hook batch。

准备的最小补丁 `patches/agx-mtp-destroy-order.patch` 仅把主上下文释放
移动到 MTP 对象销毁之后。已对 AGX 当前源码执行 `patch --dry-run` 成功，
**未应用、未编译、未替换现有二进制，尚不能宣称实机修复成功。**
源码基线 SHA256：
`tools/server/server-context.cpp` =
`275e6515ef2394d5fcca2fa560db12509b1af0b3438be3a4ec73ee63a204a9d1`；
`common/speculative.cpp` =
`49a2eb7c1ad4bdeecc43a81c626d9fddc0e40401952b9db96fd2d07904c92ed0`。

验证驱动已补充 shutdown 状态检查：`Result=success, MainPID=0,
ActiveState=inactive` 才通过，候选退出异常时停止继续测试并恢复原服务。
同时新增 `--long-semantic-check`：从长输入开头、中间和末尾抽取真实记录，
要求精确字段、正常 EOS；错误值或 `finish_reason=length` 均失败。
目前 48 项离线测试通过，但尚未运行近 256K 的新检索检查。

翻译启停来源也已确认：AGX 的
`/usr/local/lib/check-boards/lazy-service-gateway.mjs`
按请求唤醒 `agx-hymt-translate.service`，默认空闲 900 秒后停止后端。
网关同时承载 TTS，不能为了独占翻译 GPU 而直接停整个网关。
下一阶段需要单独授权：在独立目录修复/构建后端，并协调仅翻译服务的维护隔离；
先验证正常退出，再跑接近 256K 的最终性能与检索测试。

## 授权后的独立构建与存储超时

用户确认独立修复构建和翻译隔离后，使用
`scripts/build-agx-mtp-exit-fix.py` 复制完整源码与构建产物，只重编译
`server-context.cpp`、更新静态库并重新链接；没有重编 CUDA 内核。
独立目录为 `/data/agx-runtimes/cerebellum-mtp-20260904/exit-fix-v1/source/`，
二进制为其中 `build-agx-cuda/bin/llama-server`，SHA256：
`830987adaaa4de9e52389386121206fb013a11a1c0c09280c859ee55c16cb5e4`。
原二进制 SHA256 仍为
`875cc69684a1b5da59bdbaad1c7f5c3e9938a849463f70993a5135171b2c1d74`，
原源码校验也保持不变。构建记录为证据根目录 `exit-fix-build-report.json`。
测试启动必须将 `LD_LIBRARY_PATH` 指向独立目录的 `build-agx-cuda/bin`，
已通过相同环境下的 `ldd` 确认 llama/ggml 库均从独立目录加载。

`scripts/guard-agx-translation.py` 只为 Hy-MT2 添加临时启动条件，
不重启共享 TTS 网关，并设置 150 分钟兜底恢复定时器。
共享网关 PID 始终为 2106714；恢复时删除本次 drop-in 并恢复原运行状态。
已验证恢复后实际翻译返回“你好，服务运行正常。”。

`exit-fix-short/` 的修复版基线完成四轮计时和三位置精确检索，
暖轮为 34.18 / 34.33 / 34.13 TPS。MTP1 启动超过 180 秒而中止，
退出状态为 success，但尚未完成一次 MTP 推理后的退出，故不能宣称修复验收成功。
原服务恢复也超出原等待预算，原始报告保留 `restored=false`；
随后等待同一个 PID 3278666，没有重复重启，最终完成 7 项复验。
后续真实恢复证据为 `exit-fix-short/late-restoration-report.json`，
翻译恢复证据为 `exit-fix-short/translation-restoration.json`。

内核日志证实模型所在 `/data` 的 NVMe 出现反复 I/O 超时：
`nvme nvme0: I/O ... timeout, completion polled`。
本次检索的 20:00 之后日志最早在 21:02:07 出现，早于独立修复版构建；
23:30 至 23:36 再次发生并伴随模型加载进程等待磁盘页。
`nvme-kernel.log` 和 `nvme-smart.json` 保存现场证据。
SMART 当次 critical_warning=0、media_errors=0、num_err_log_entries=0，
不能据此排除主机、控制器或驱动路径问题，也不能直接断言 SSD 介质损坏。

未重置 NVMe、未改内核/电源设置、未重启设备、未执行磁盘压测或删除 core。
后续先测试仅作用于独立实例的 `--no-mmap` 加载方式，
保持相同模型、上下文与采样；它是待验证的加载替代方案，不是已证明的存储修复。

`exit-fix-no-mmap-short/` 记录第二次尝试。2026-09-04 23:41:49，
非 mmap 的基线加载也出现新的 NVMe I/O 超时，因此主动中止而非继续压测。
维护耗时 203.769 秒，`passed=false, restored=true`。
最终原服务 PID=3288508、active、NRestarts=0，7 项恢复检查通过；
翻译实际请求返回“翻译服务已恢复。”。
两次翻译隔离均已撤销，对应恢复定时器已取消，测试端口与 SSH 隧道已关闭。
共享 TTS 网关仍为原 PID=2106714、NRestarts=0；没有停止或重启它。

当前系统为 L4T R36.5.0、内核 `5.15.185-tegra`，
当次 `/proc/interrupts` 显示 NVMe 所有队列中断计数集中在 CPU0。
NVIDIA 开发者论坛 topics 296712、297495 存在类似 I/O 超时或 PCIe
中断分布的讨论，但仅作排查线索，不认定本机根因或直接套用旧版本补丁。
本轮没有修改 IRQ 亲和性、PCIe/APST 参数、内核或设备固件。

**尚未完成：修复版 MTP1 的完整推理后正常退出验收，以及近 256K
性能和真实检索检查。** 最小修复已编译但未上线，原生产源码与二进制
再次校验未变。当前已有数据支持 MTP1 作为优先候选，不支持宣称最终最优
或已具备 256K 生产安全性。应先定位 NVMe/PCIe 路径异常，再继续长测。

当前聚焦测试为 52 项通过：

```bash
PYTHONPATH="/home/ai/llama.cpp-github/gguf-py" pytest -q \
  tests/test_cerebellum_mtp.py tests/test_mtp_benchmark.py \
  tests/test_mtp_quick.py tests/test_mtp_maintenance.py
```
