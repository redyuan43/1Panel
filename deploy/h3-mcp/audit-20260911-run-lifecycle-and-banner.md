# 独立审计：run 生命周期与比较横幅

审计时间：2026-09-11（关键测量 12:26–12:29，复验 18:02）
审计对象：`deploy/h3-mcp` 及 2026-09-11 相关报告（`multimodal-release-20260911.md`、`status-fix-20260911.md`、`residue-fix-20260911.md`）声称的实现与验证。
方法：只读复跑现有测试、与已部署 release 逐字节比对、阅读被叠加/被替换的源文件。**未修改任何生产代码，未切换或重启服务，未提交 GPU 任务。**

## 结论速览

| 结论 | 项数 | 状态 |
|---|---|---|
| 已确认、作者已修复（run 生命周期） | 3 | 12:31 落盘、12:36 随 `releases/20260911-status-fix-r1` 发布；复跑通过 |
| 已确认仍存在、本次修复（比较横幅时钟偏差） | 1 | 本轮修改 `frontend/comparison-status.js` 与对应测试 |
| 已排除（假失败，不是回归） | 7 failed + 3 errors | 未指定 Studio 测试根导致 |

## 一、已确认并已修复：run 生命周期 3 项

测量条件：Studio 测试根 `/tmp/h3-multimodal-studio-candidate-r12`（06:42 构建，**不含** 12:31 的修复），
`python3 -m pytest -q tests/test_router_contract_runs.py` → `3 failed, 438 passed, 1 skipped`（全量口径）。

三项失败与断言原文：

1. `test_running_queued_running_keeps_run_callbacks_and_approval`
   - `At index 1 diff: ('queued', 'run_7d246ab8c1ee458f9832cd69023ab17d') != ('queued', 'run_c29f333cd21d4cd39f410ae35f9a4f7d')`
   - `Right contains 3 more items, first extra item: ('running', 'run_c29f333cd21d4cd39f410ae35f9a4f7d')`
   - 现象：`observed` 只记录 2 次即中断——Fleet 把准备阶段从 running 退回 queued 时 run_id 被换，进行中的回调随即被判 `StaleRun`。
2. `test_reconciliation_of_same_execution_is_not_a_new_attempt`
   - 现象：对账恢复后 run_id 由 `run_9b81…` 变为 `run_36dd…`，同一次 execution 被当作新 attempt 重新提交。
   - 与报告第 39 行「启动失败/结果未知隔离，不无限重启」的承诺相反。
3. `test_cancellation_survives_active_queue_callbacks`
   - 现象：`observed == []`，`h3_cancel_task` 之后的队列回调抛错，`cancelled()` 未被调用，取消无法干净收敛。

根因（同一处）：`Contract.update` 把所有重新进入 queued 的状态变化都当成新 attempt。

修复对照：

```python
# 修复前（已部署 releases/20260911-multimodal-r1/app/router_contract.py:336；12:29 时的仓库源 :391）
if stage["status"] == "queued" and old["status"] != "queued":
    stage["run_id"] = "run_" + uuid4().hex

# 修复后（当前 deploy/ai-router/integrations/h3/router_contract.py:391，与 releases/20260911-status-fix-r1/app/router_contract.py:336 一致）
if stage["status"] == "queued" and old["status"] not in {"queued", "running"} and not (old.get("fleet_pending") and old.get("execution_id") and stage.get("execution_id") == old["execution_id"]):
    stage["run_id"] = "run_" + uuid4().hex
```

验证：修复于 12:31 落盘，12:36 发布；当前复跑 `tests/test_router_contract_runs.py` → `12 passed`。

已排除「测试夹具指向了过期文件」这一可能：该测试会把 `STUDIO_ROOT/app/router_contract.py` 替换为仓库源。
精确 diff 两侧 `update()` 后，唯一差异是候选多出一句 `validate_connector_execution(project)`（位于 `mutator(project)` 之后），
run_id 轮换逻辑逐字相同——因此替换不改变结论，上述失败在当时的部署代码上同样成立。

## 二、已确认仍存在、本次修复：比较横幅时钟偏差

`frontend/comparison-status.js` 用「浏览器时钟 − 服务端时间戳」判断数据是否新鲜：

```javascript
const age = Date.now() / 1000 - data.observed_at;
if (!data.available || !Number.isFinite(age) || age < 0 || age > 30 || !Array.isArray(data.cases)) throw new Error();
```

`observed_at` 由产出侧服务时钟写入（同侧用法见 `deploy/h3-video-studio/app/fleet.py:38` 的 `age = time.time() - float(snapshot["observed_at"])`，
生成侧见 `deploy/h3-video-studio/tests/test_capacity.py:63,96` 的 `"observed_at": time.time()`），而 `age` 用浏览器 `Date.now()` 计算。
两台机器的时钟常态存在毫秒到秒级偏差，客户端只要慢一点 `age` 即为负，`age < 0` 命中后每 5 秒轮询都抛错，横幅**永久**停在
「配方比较实验：状态暂不可核实（不代表工作室无任务）」，且不报错、不留日志。

- 上一版 `releases/20260911-multimodal-r1` 同一处只有 `Date.now() / 1000 - data.observed_at > 30`，没有 `age < 0`，所以这是本轮新引入的收紧。
- 它躲过了 12:36 的上线验收：已部署 release 的 `frontend/` 里没有 `comparison-results/` 目录（源码里该目录只有 `.gitignore`，`live.json` 由比较监控在运行时写入），
  在 `live.json` 缺失时落入同一段 fallback 本来就正确，因此这条分支在线上从未被真正走到。
- 同源小问题：耗时分钟数 `Math.floor((Date.now()/1000 - record.started_at)/60)`，`started_at` 略在未来时会渲染 `-1分钟`。

本轮修复：

```javascript
// 容忍 5 秒以内的跨机时钟偏差；明显未来/过期/缺失仍判为不可核实
if (!data.available || !Number.isFinite(age) || age < -5 || age > 30 || !Array.isArray(data.cases)) throw new Error();
// 未来 started_at 不得渲染负数分钟
Math.max(0, Math.floor((Date.now() / 1000 - record.started_at) / 60))
```

测试从 3 项补到 6 项：新增「小幅未来（+2 秒）必须正常渲染」「明显未来（+60 秒）必须判不可核实」「未来 started_at 不得出现负数分钟」。
`node --test tests/comparison-status.test.cjs` → `6 pass / 0 fail`。

## 三、已排除：未指定 Studio 测试根造成的假失败

不设 `H3_CONNECTOR_TEST_STUDIO_ROOT` 直接 `pytest tests/` 会得到 `7 failed / 3 errors`：

- `tests/test_browser_tasks.py` 3 项：`assert 405 == 403/409`——默认根是 studio 源码目录，缺 overlay 安装的 `browser_tasks.py` / `input_view.py`，`/api/h3-browser/*` 未注册；
- `tests/test_input_assets.py` 1 项断言失败（同上缺少叠加模块）；
- `tests/test_multimodal_client_chain.py` 3 项 errors：`ModuleNotFoundError: No module named '...multimodal_client'`。

指向任一已叠加目录后这些用例全部通过。**这不是发布回归**，但必须在有人复跑前说明，否则会被误判。

## 四、仍待处理（不在本轮范围）

1. 同一前端资源存在三份且已漂移：`deploy/h3-mcp/frontend/comparison-status.js`（overlay 源，本轮已改）、
   各 release 内的副本、studio 源码树 `deploy/h3-video-studio/frontend/comparison-status.js` 及其自带测试 `tests/comparison_status.test.cjs`（仍读旧文件）。
   本轮只改 overlay 源；同步旧副本需按发布流程另行处理。
2. 候选与证据仍停在 `/tmp`（`/tmp/h3-multimodal-studio-candidate-r12`、`/tmp/h3-multimodal-full-r12.xml`），
   其中 `20260911-status-fix-r1` 已不是线上根：17:37 起线上 Studio 运行 `releases/20260911-firstframe-four-recipes-r1`。
   复跑数字必须连「测试根 + 时间」一起记录，否则不可复现。
3. run 生命周期修复覆盖「重新进入 queued」与「同 execution 对账恢复」两条路径；终态重试换号由
   `test_new_attempt_rotates_run_and_rejects_old_callback` 覆盖。后续若新增 other 状态回流路径，应同样补一条反向断言。
4. 真实浏览器复核：本轮不部署，线上 release 内的 `frontend/comparison-status.js` 仍是含 `age < 0` 的版本，
   因此本修复的浏览器实机复核（`#comparisonLive` 实际文案、控制台无 JavaScript 错误）留待发布后执行。
   不得以 `node --test` 的 6 项结果替代浏览器留证。

## 附：本次测量时间线（同一份代码在不同时刻结论不同）

| 时间 | 事件 |
|---|---|
| 06:42:10 | 候选 `/tmp/h3-multimodal-studio-candidate-r12` 构建（不含 run 生命周期修复） |
| 06:44 | `multimodal-release-20260911.md` 记录的 654 项全绿（h3-mcp 侧 438 + ai-router 侧 `tests.test_h3_*` 216） |
| 12:21:01 | `frontend/comparison-status.js`、`tests/comparison-status.test.cjs` 落盘（引入 `age < 0`） |
| 12:26–12:29 | 本次审计复跑：`3 failed, 438 passed, 1 skipped`（根 r12） |
| 12:31:00 | 作者修复 `deploy/ai-router/integrations/h3/router_contract.py`，同分钟加入 `tests/test_router_contract_runs.py` |
| 12:36:38 | 发布 `releases/20260911-status-fix-r1` |
| 17:37 | 线上 Studio 切到 `releases/20260911-firstframe-four-recipes-r1`（drop-in `90-firstframe-four-recipes.conf`） |
| 18:02:45 | 本轮改动后复跑：根 `20260911-status-fix-r1` → `499 passed, 1 skipped` |

复跑命令（务必显式指定 Studio 测试根）：

```bash
cd /home/ai/github/1Panel/deploy/h3-mcp
R=/home/ai/.local/state/h3-studio-ivan-production/releases/<当前线上或已叠加候选目录>
H3_CONNECTOR_TEST_STUDIO_ROOT=$R python3 -m pytest -q tests/
node --test tests/comparison-status.test.cjs
```
