# H3 四配方首发：4060 Ti 单路

## 已部署范围

- OnePanel：`https://ai-x10drg.taild500c8.ts.net:4001/h3-studio/`。
- 独立页面：`https://ai-x10drg.taild500c8.ts.net:8445/`。
- 默认 A4，另保留 A4_C0、A4_C1、B8；只为 15 秒、480×864、24fps、原生音频文生视频配置新配方。
- Fleet 全局 `max_active_jobs=1`；不授予 3060 新配方资格或混合并发资格，不自动更换配方。
- A4 使用原 reserve-vram=1 配置；A4_C0/A4_C1 使用原 reserve-vram=3 配置；B8 使用原 reserve-vram=6 配置。三个本地后端都绑定同一 4060 Ti UUID，只有串行占用权。
- 保留 72GiB 准入与运行期原始内存保护、Swap/PSI/显存/磁盘门禁。资源未稳定时自动等待，不为了上线放宽保护。

## 验证边界

用户后续明确取消重复验证，因此本次不提交任何新 GPU 生成任务，也不重解码历史整片。A4/B8 启动短测同样取消。

导入原 A4-r3、B8-r3、A4-working-set-v4/A4_C0、A4-working-set-v3/A4_C1 报告、原始工作流、成功历史和媒体探测文件，保留文件摘要与配方版本绑定。C1 的原批次失败信息仍保留，只复用该任务本身的成功结果。不补造采样事件或历史权重摘要；当前运行源码和权重独立固定。资格标为 `historical_single_completed` / `historical_report_reused`，不等于本次完整成片或并发验证。

发布前软件回归包括 Studio 122 项、前端 Node 15 项、代理 23 项。Fleet 首轮 1190 项通过，打包清单一项失败后修正，相关打包与进程身份检查 18 项通过。用户取消新增验证后不重复跑全套测试。

上线只读检查确认两个入口返回 200、配方接口返回四项、未认证项目接口返回 401、跨站写入返回 403、10 个旧样片分段读取返回 206，前端资源与发布清单相符。未发送新生成请求，不能把这些检查称为新的推理成功证据。

## 发布位置

- Studio 前后端：`/home/ai/.local/state/h3-studio-ivan-production/releases/20260910-r1`，不再从开发工作树加载程序。
- Studio 项目数据库与文件仍位于 `/home/ai/.local/state/h3-studio-ivan-production`。
- 历史比较产物保持原持久路径，由只读资源路由提供；不复制覆盖旧样片，不中断原只读发布器。
- Ivan：`/mnt/ivan-ext4-offload/h3-single-release-20260910-r1`；生效配方模板位于 `binding-r2/recipe-policy-template.json`。
- `h3-fleet.service` 通过 `90-single-release.conf` 切换代码与策略；原 Fleet 数据库不搬移。
- `h3-single-a4.service` / `h3-single-realism.service` / `h3-single-b8.service` 分别使用 18488/18489/18490，仅监听 Ivan 回环地址。
- 进程启动前核对服务身份、实际命令、GPU UUID、目录、监听端口及保护 cgroup，再刷新 PID 绑定。审计写入 `identity-audit.jsonl`；旧任务的冻结后端身份不改写，不重提身份未知任务。
- OnePanel 镜像基于发布时最新 `routing-modes-control-20260910-r2`，只叠加代理及导航；保留全部既有 Compose 覆盖配置，仅重建两个 Control。Router API 与 3060 worker 未由本次发布重启。

## 运维与回滚

AI 发布材料和备份：`/home/ai/.local/state/h3-studio-ivan-production/deployment/single-release-20260910-r1`。Ivan 原服务、配置和 SQLite 备份在对应发布根目录的 `backup` 内。私密文件不入 Git。

1. 回滚先通过 Fleet 的认证 `POST /api/router/release-validation-gate` 设置 `enabled=true, allow_execution_prefixes=[]`，停止新增提交；原健康任务继续由新版本对账。
2. 有已提交任务或未知提交结果时，不替换 Fleet 为不认识新后端的旧版本，不重提，不删除数据库。
3. 所有任务终结且真实队列空后，恢复备份的 Studio unit；Control 回退到本次切换前的 r2 镜像和完整覆盖链。不要回退到初始盘点时已被其他发布替代的 r1。
4. 移除本次 Fleet drop-in 前核对旧服务代码/配置，再做受控切换。新后端只有确认无任务、空队列、模型已卸载后才可停止。
5. 不自动恢复旧数据库备份覆盖新任务历史；备份仅用于人工恢复审计。

本次候选打包曾误排除嵌套的 `comfy/ldm/models`，导致新后端在导入阶段退出，未提交推理、未发生推理 OOM。随后修正为只排除根目录的权重目录，恢复与原运行时一致的代码，记录为 `runtime-stage-r2.json`；不将该打包修复记为模型验证。
