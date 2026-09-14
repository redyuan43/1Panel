# 四配方版本化目录

## 服务端接口

`app.recipes.RecipeCatalog(path=None)` 默认读取 delivery 内 `config/recipes.json`。
import 无文件读取、副作用、网络或 GPU 操作；构造只读取本地目录和四份模板。
缺失、损坏、重复 JSON key、模板 SHA 错误会使目录 unavailable，保留诊断 `error`，
`public()` 返回空配方列表，`build/validate` 拒绝执行；不回退旧模板，不阻断 main import。
自定义 path 是服务端可信配置，不得接收客户端提供的路径或目录内容。

```python
catalog = RecipeCatalog()
graph, binding = catalog.build("A4", prompt, seed, "video/router/execution_id")
binding = catalog.validate("A4", graph, version="20260910.1")
```

- `public()` 返回 default_recipe_id、四个公开 recipe、retired_recipe_ids、catalog_status/error。
- `get(id)` 返回隔离副本，含模板 pin、来源、权重、采样和运行时源码要求；退休 ID 返回历史状态。
- `build(id, prompt, seed, prefix)` 返回 `(graph, binding)`，无自动生成 seed，无 prompt 改写或 strip。
- `validate(id, graph, version=None)` 校验完整所选图并返回 binding，不进行转换或硬件准入。
- `freeze(id, graph)` 仅校验并深拷贝；不是旧 Studio T8 图的转换入口。
- `/prompt` 必须验证收到的原 graph，不能先 build 再把非法输入当作通过。
- Studio 可先请求只 CPU 的 recipe-workflow API，再原图携 recipe_id/version 提交。

唯一可变输入定位为 `6.inputs.prompt`、`8.inputs.noise_seed`、`13.inputs.filename_prefix`。
seed 必须为 uint64（不接受 bool/float）；prefix 必须是安全相对路径，禁止绝对路径、反斜杠、
空 segment、`.`、`..`。写实配方自动添加 `r34l1sm\n`，已存在一次时保留，重复或缺正文拒绝。
validate 要求写实配方确有一次前缀；动态 prompt 正文不作质量或“是否有人物”判定。

全图除上述三个值外逐 JSON 内容固定：节点 ID/数量、类、所有 inputs、连线、采样器 role、
权重名、LoRA 强度、VAE、编码器、native audio、尺寸、fps、输出 codec 和 `_meta` 均不可改变。
不信 client shape、title 或 recipe label；不按同 shape 推导采样语义等价。
JSON 对象键顺序不影响摘要；不宽松归一化数值（固定字段 4 与 4.0 不视为相同）。

## 正式与退休配方

| ID | LoRA / 分支 | steps / shifts | trigger / People |
|---|---|---|---|
| A4（默认） | LightX2V v1.2 原始 BF16 LoRA，强度1 | dual_clock_euler 4，6/3 | 无固定 trigger / 0 |
| A4_C0 | 同 A4 | 同 A4 | r34l1sm / 0；noPeople 指无 People LoRA |
| A4_C1 | a4v12_people10_fp32，单 Bypass 强度1 | 同 A4 | r34l1sm / 1 |
| B8 | OpenVDN stage_dmd_8nfe 原生执行计划 | euler 8，12/3，native_flow | 无固定 trigger / 0 |

统一原生 T2VA：480×864、362帧、24fps；产品标称15秒，真实时长362/24≈15.083333秒，不裁切。
R0/C0/C1/D4/A8/A4_C05 仅保留历史，不支持新 build/validate，不自动重命名映射。
目录不会删除旧报告或 gallery。A4 与 A4_C0 的静态图可相同，其区别为固定 trigger 规则；
允许 A4 自由 prompt 自带该文字不构成不同模型权重身份。

## 来源及未知边界

- A4 取自持久证据 `optimization-20260909/A4-r3/workflow.json`。
- A4_C0 取自 `A4-working-set-v4/A4_C0/workflow.json`。
- A4_C1 取自 `A4-working-set-v3/A4_C1/workflow.json`；该 batch 最终失败，C1 task collected，
  不将其宣称为成功并发 batch 或硬件资格。
- B8 只读 SSH 取得 `B8-r3/workflow.json`，原始字节 SHA 与本地 report.workflow_sha256 一致。
- 四份 delivery 模板只将 prompt、seed、prefix 替换为固定占位值，其他内容保留。
- A4_C1 组合工件 SHA：`5f7d9c69972d65c96438614af645b6d9e60f9a91daf1699179e2843e2de892fc`；
  来源为只读现场组合 receipt，其 SHA 为 `0678473f515652c2d45a7d1beddea2edef6e2f9d19c722a53391749b60a15a59`。
  本轮未重新读取4GB权重做全文件验证；receipt 声明与现场实际文件一致性仍由 runtime gate 校验。
- 基础 DiT 为 full INT8 convrot，不是 NVFP4 DiT；NVFP4 AWQ 是单独的文本编码器。
- 编码器、视频VAE、音频VAE的全文件 SHA 在本轮已查证据中缺失，JSON 显式 `null`，不编造。
- core/T8 完整 commit、A runtime source hashes、B8 最小 plugin hashes、VDN 分支/adapter/meta
  文件 pin 保存在 catalog。它们是所需源码来源，不等于现场 worker 已符合。
- provenance 中历史绝对路径仅说明证据来源；加载、构图和验证绝不访问这些路径。

## 摘要与独立硬件资格

recipe_digest 是 recipe entry 的排序、紧凑 UTF-8 JSON SHA256，覆盖版本、模板摘要、权重和来源要求。
graph_sha256 是完整构图的同格式 SHA；structure_sha256 将三个动态值替换为同一哨兵后计算。
template_sha256 是 delivery 模板原始文件字节 SHA。修改契约需新 version 并重算 pins。
以上为部署信任边界内的内容 pin，不是签名；客户端不能覆盖服务端 catalog。

binding 仅含 recipe_id/recipe_version/recipe_digest/graph_sha256/structure_sha256/template_sha256
及 `hardware_qualification=not_evaluated`。它不包含 GPU 选择或 mixedqualified。
主代理在独立 recipe-scheduling.json 中以 recipe_digest + runtime/config/权重全SHA + 硬件 profile
匹配资格和内存预算；默认 enabled=false/backends=[]。未知共享 SHA 必须补齐，不能当作通配符。
recipe catalog ready 只说明契约可构建，不代表3060、4060Ti或混合并行可执行。

## 离线验证

`PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_recipes.py`

无需新增第三方依赖；实现仅使用 Python 标准库，测试复用 pytest。
