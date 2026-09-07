# 缓存与流程审计

入口是现有 4001 管理后台的「路由审计」。统计总览与会话详情共用请求记录；会话详情保留时间线，六个阶段按需展开。完整路由图在「路由选择」中，缓存计数在「模型执行」中，加密原文在「内容整理」中按需读取。

## 指标口径

- request_id 是 Router 生成的请求 ID；每次上游执行另有 operation_id 和 attempt。后台预热是独立的 prewarm 操作，不计为一条用户请求。
- 正式输入 P = native cache_n + prompt_n。正式重新计算 R = prompt_n，准备阶段重新计算 Q = prime_tokens。当前尝试实际 prefill = Q + R。
- 净 token 复用率 = max(0, P - Q - R) / P；固定前缀 F 的复用率 = max(0, min(F, cache_n) - Q) / F。仅在原生边界与对应测量均已取得时计算，固定前缀复用至少 95% 才达标。
- 热状态、磁盘恢复和 miss_saved 是缓存准备结果，本身不能证明省掉 prefill。重试明细和所有尝试的计算量分别展示；最终尝试缺少遥测时不使用旧尝试代替。
- Router 首个输出包含排队及入口处理，以流中第一个有效正文、推理或工具输出为准；首段正文另计。网关首个输出从网关接收开始。非流式不伪造 TTFT。
- template_ms 包含运行版本检查和原生模板/token 边界确认；restore_ms 包含完整性校验与恢复；prime_ms/save_ms 是该阶段经过时间。准备总时间包含子阶段，不相加重复统计。
- 总览分布只统计成功请求的非缺失值，显示中位数、P95和有效样本数。缺失不是0；没有冷请求对照时不推算节约秒数。按小时展示趋势；每次最多分析最近30天内10,000条请求，达到上限明确提示缩小范围。

## 内容与数据

阶段快照包含指令清理前的 received、清理后、WorkBuddy重排后、有效上下文和每次真正forwarded请求。正文按内容哈希去重，保存在既有加密压缩归档；元数据保留阶段哈希和规则检查。旧归档不冒充完整入口原文。

默认客户端范围覆盖 workbuddy 开头的所有 WorkBuddy 账号（包括 public 和 qwen36-shared）；可以切换所有客户端或指定单一账号。后端usage报告与全局计数差值估算明确注明来源，不冒充准备阶段已扣除的净复用。

新管理接口：GET /api/cache/summary、GET /api/cache/requests、GET /api/route-traces/{id}/content。全部沿用管理员认证。原文接口按字符分页且no-store，以纯文本渲染；请求列表和遥测新增字段不包含正文。人工评估使用现有审核和备注。

AI 侧指标保存在现有 route-traces.sqlite3 的 cache_operations 表，按请求和时间索引，沿用30天清理。Control以只读方式挂载既有training目录并禁止实例化训练归档写入器；原文查看另记不含正文的访问事件。当只读挂载因SQLite WAL侧文件缺失无法打开数据库时，通过本机API的管理员专用 GET /internal/request-content/{id} 执行同样的 mode=ro 查询；API已有可写目录允许SQLite维护侧文件，Control仍保持只读挂载，不使用会忽略WAL的immutable方式。NX3/AGX仅增加最多1,024条、30分钟、8MiB的短期内存遥测，不新增磁盘日志副本或KV文件，不改变原缓存预算。

网关 GET /cache/telemetry/{operation_id} 使用现有后端认证，读取不占用推理锁。Router仅对声明X-Prefix-Telemetry:1的直接后端异步采集；中断或采集失败保留缺失状态，不阻塞推理。未升级网关的设备与旧请求只有可取得的基础指标。

## 验证及回滚

CPU契约在 tests/test_*_contract.py，浏览器测试在 tests/browser_cache_audit.cjs 和 tests/browser_control.cjs，使用 tests/ui_preview.py 的独立CPU归档，禁止对生产运行预览测试。独立60项、客户端/来源补充2项、归档回退补充14项、管理/权限补充4项、新界面9项和原后台24项通过。全核心组23项旧模型注册断言在未修改基线同样失败；本次未修改注册表。

修复了遥测异常或异常native计数导致网关锁未释放的问题，并验证故障注入后锁仍可获取。内容分页绑定请求/阶段/偏移；切换阶段清空旧片段，拒绝旧响应和重复追加。

部署证据与备份位于 AI 的 experiments/cache-audit-20260907：baseline、baseline-diff.patch、verification、browser、deployment.json（上线生成）。上线前记录旧镜像、网关脚本及原生模型PID；无在途请求时依次更新。回滚重新标记旧镜像并重建四个API/Control容器，恢复两台网关备份脚本后仅重启网关；新增数据库表可保留，旧版本忽略。模型服务和KV缓存文件不属于回滚操作。

真实请求验收必须使用上线后的自然请求：核对同一request/operation的网关原生计数、中央记录与页面。CPU/浏览器测试通过不代表已经完成真实推理验收。

2026-09-08 上线后的首批9条自然 WorkBuddy 请求已记录 Router 首个有效输出，其中5条有正文输出。它们均选择 AI 的 qwen38，首个有效输出中位数约4.35秒；没有可比冷请求，不据此宣称节约了多少秒。最终服务检查时自然请求已累计15条，仍全部走AI。NX3/AGX尚无升级后自然推理样本，原生prefill/恢复/固定前缀达标率的生产核验仍待后续请求。

两套Control均通过生产原文鉴权、分页及no-store检查。真实请求具有5个阶段，received为183,421字符，正文未写入验证日志。HTTPS浏览器只读检查确认六节点、统计卡片、默认WorkBuddy范围及无页面脚本错误。部署期间NX3原生PID 920、AGX原生PID 1979258不变，两侧各13个缓存文件的路径、大小和修改时间与部署前一致。

## 回滚操作

在AI的 `/home/ai/github/1Panel/deploy/ai-router` 操作。先通过现有管理员 `/internal/status` 确认两套Router没有在途请求，再逐个调用 `/internal/drain` 并等其 `active_request_count` 为0；仅停止接单不能替代等待已接收请求完成。以下命令是回滚步骤，未自动执行。

```sh
for service in router-api-local router-api-tail router-control-local router-control-tail; do
  docker tag 1panel-ai-router:before-cache-audit-20260908 "1panel-ai-router-$service"
  docker compose up -d --no-deps --no-build "$service"
done
```

逐个确认新启动API的 `draining=false`、内部状态正常，以及两套Control可访问。旧镜像ID保存在 `experiments/cache-audit-20260907/deployment.json`；R1/R2镜像和后续Control文案镜像同样保留，可选择只回退某次增量。

需要连同遥测网关一起回退时，先确认对应原生 `/slots` 无执行中请求，将上述deployment.json记录的 `qwen36_prefix_gateway.py.before-cache-audit-20260908` 复制覆盖同目录的 `qwen36_prefix_gateway.py`，仅重启 `nx3-qwen36-cache-gateway.service` 或 `agx-qwen36-cache-gateway.service`。重新检查原生PID、slots和网关health。不要重启模型service，也不要删除缓存目录。

代码保留未提交状态。源码回退需按 `source-final.json` 中本次修改列表与 `baseline` 对比，逐文件保留实验开始前的未提交改动；不要对整个仓库执行reset或clean。数据库新增表可保留，旧镜像忽略它。Control只读training挂载可以保留。
