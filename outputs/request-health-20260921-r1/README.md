# 请求记录与健康证据部署记录

版本：`request-health-20260921-r1`。最终核验：2026-09-21T09:59:05.156480+08:00。

用户授权后按 API local → API tail → 管理端 local → 管理端 tail 顺序完成替换；API 先停止接收新请求，等待活动请求为零。四个实例均恢复健康，重启计数为 0。

## 发布范围

在各实例原运行镜像上叠加十个文件的限定补丁，未从混合工作树整体构建。包括请求历史倒序、旧页终态刷新、Token 口径与最终部署窗口证据、健康探测分类及异步审计写入。未改变上下文处理、换模型策略或注册表配置。

镜像保留原基线 OCI revision；本次版本由镜像 ID、补丁摘要与逐文件哈希共同标识，不代表新的 Git 提交。

| 实例 | 运行镜像 ID | 基线源码 revision |
| --- | --- | --- |
| api-local | `sha256:8e4ed8023e902bc160254ea7512f96efd196496f04b702409dbca7a91aa35c1c` | `08be434cb5ecf6a3699136f3231785cc16b64c6d` |
| api-tail | `sha256:13290a087164a77a53d5e0f2ae7a781c7f76bab46ebd02e75a0bec77b795df80` | `08be434cb5ecf6a3699136f3231785cc16b64c6d` |
| control-local | `sha256:4bc0a2cc1a38b8a1b3cdfa7c97f2ffbbae20f53684f4503dbc8933834af1060a` | `32196dc72e1a4b1356075a81ce70678fbbd3a396` |
| control-tail | `sha256:4bc0a2cc1a38b8a1b3cdfa7c97f2ffbbae20f53684f4503dbc8933834af1060a` | `32196dc72e1a4b1356075a81ce70678fbbd3a396` |

完整候选标签、基线与补丁哈希见 [candidate-manifest.json](candidate-manifest.json)，运行结果见 [final-verification.json](final-verification.json)，发布过程见 [events.jsonl](events.jsonl)。

## 验证

- API 候选：113 项 Python 测试通过。管理端候选：112 项通过，1 项缓存状态测试失败；该失败在未修改的管理端基线独立复现，属于已有问题。详情见 `tests-control-local.xml` 和 `baseline-control-usage.xml`。
- 11 项 Node 测试及隔离浏览器测试通过，覆盖桌面/移动端排序、旧页终态、分页和滚动。
- 四个构建镜像均以正常运行用户在禁网环境完成模块导入、接口签名及源码哈希验证。
- 上线后核验四个实例的镜像、源码哈希、环境摘要、启动命令、挂载及 host 网络。两个管理端的实际静态资源哈希、按请求 ID 查询、Token 摘要和未认证请求拒绝均通过。
- V100、LMCache、Redis、存档消费者及其他适配服务的容器与镜像未变，重启计数均为 0。

## 边界与剩余问题

没有调用真实模型、注入健康故障或变更路由策略。旧 DeepSeek 故障缺少详细证据，根因仍未确认；新失败事件的生产采集效果需等待实际故障验证。已有管理端缓存状态测试失败未在本次修复。

部署完成时未暂存、提交或推送；后续用户授权单独提交本次源码、测试与部署记录，其他任务改动保留。

## 回滚工件

冻结的 `compose.base.yaml` 和 `compose.rollback.json` 保存逐实例原镜像及配置，原镜像 ID 见清单。后续若需回滚，应先核对当前版本和容器状态，取得部署锁，逐个 API drain 到活动请求为零，再使用冻结回滚覆盖文件仅重建目标服务，随后核验健康、环境和原镜像。不得直接对全套服务执行重建；不得重跑本次 `release.py --apply`（其前置条件是发布前容器基线）。
