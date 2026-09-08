---
name: seeding-video
description: "基于 种草视频 的 AI 视频生成器，通用商业场景搜索入口，底层调用 AI Hive 裸接口。当用户需要 种草视频、小红书种草、抖音种草、社媒种草、好物推荐、UGC种草、Seedance、MiniMax H3、Happy Horse、AI视频 时使用此 Skill。"
description_zh: "基于 种草视频 的 AI 视频生成器，通用商业场景搜索入口，底层调用 AI Hive 裸接口。当用户需要 种草视频、小红书种草、抖音种草、社媒种草、好物推荐、UGC种草、Seedance、MiniMax H3、Happy Horse、AI视频 时使用此 Skill。"
version: 1.0.0
------

# 种草视频

## 简介

本 Skill 参考 Seedance 2.5 裸接口 Skill 的产品化风格，封装 AI Hive OpenAPI，通过命令行一键调用，自动处理模型选择、媒体上传、价格快照、任务轮询和结果下载。

### Skill 特色

- 独立占据 `seeding-video` 搜索入口，不与其他模型或能力 Skill 合并
- 固定使用本 Skill 声明的 publicModelId，避免模型名与底层调用不一致
- 自动上传图片、视频或音频参考素材
- 默认使用 `COST_FIRST`，每次实时读取价格快照
- 自动轮询任务状态并下载结果

### 适用对象

内容创作者、视频编辑、广告与营销团队、电商团队、带货与种草团队、短剧和漫剧创作者，以及需要 AI 视频生成的用户。

## 功能特性

### 生成模式

| 模式 | 说明 | publicModelId |
|---|---|---|
| `t2v` | 文生视频 | `public_model_seedance_2_5_t2v` |
| `i2v` | 图生视频 | `public_model_seedance_2_5_i2v` |
| `r2v` | 参考生视频 | `public_model_seedance_2_5_r2v` |

### 参数控制

- 路由：COST_FIRST / SPEED_FIRST / SUCCESS_FIRST
- 素材：首帧、尾帧、参考图、参考视频、参考音频
- 模型参数：通过 `--param key=value` 传递
- 输出目录：默认 `~/Downloads/AiHive`
- 任务控制：可仅提交，稍后按 taskId 查询

## 参数速查

### generate 子命令

| 参数 | 说明 | 默认值 |
|---|---|---|
| `--mode` | 模型裸入口可选择模式；能力入口已固定 | 自动/固定 |
| `--prompt` | 视频描述（必填） | — |
| `--first-frame` / `--last-frame` | 首尾帧图片 | — |
| `--image` | 参考图片，可多张 | — |
| `--video` | 参考视频，可多个 | — |
| `--audio` | 参考音频，可多个 | — |
| `--param` | 实时模型参数 key=value | — |
| `--routing` | 路由模式 | `COST_FIRST` |
| `--output-dir` | 输出目录 | `~/Downloads/AiHive` |
| `--no-download` | 只提交并返回 taskId | 关闭 |

### 通用参数

| 参数 | 说明 |
|---|---|
| `--api-key` | AI Hive API Key |
| `--base-url` | API Base URL |
| `--verbose` | 输出详细日志 |

### 其他子命令

| 子命令 | 功能 |
|---|---|
| `task --task-id <id>` | 查询生成任务 |
| `upload --file <path>` | 上传媒体并获得 mediaId |
| `init --skill-name seeding-video` | 初始化 API Key |

## 使用场景

### 场景一：基础生成

```bash
python3 "$SKILL_PATH/scripts/videogen.py" generate \
  --mode t2v \
  --prompt "制作一条可投放的种草视频，品牌信息清晰，画面有记忆点"
```

### 场景二：广告 / TVC

```bash
python3 "$SKILL_PATH/scripts/videogen.py" generate --mode t2v \
  --prompt "高端产品广告，主体清晰，镜头环绕推进，轮廓光扫过材质，结尾定格品牌特写"
```

### 场景三：电商 / 产品展示

突出商品结构、材质、卖点和使用场景，不虚构功能、价格、认证或功效。

### 场景四：带货 / 种草

采用竖屏信息流节奏，先展示痛点，再展示产品使用过程与真实利益点。

### 场景五：短剧 / 漫剧

先固定角色外观、服装、场景和画风，再逐镜生成；每条只承载一个明确动作。

### 场景六：参考素材生成

为每项参考素材明确角色：人物身份、产品形态、动作、运镜、风格或声音。

### 场景七：仅提交任务，稍后查询

```bash
python3 "$SKILL_PATH/scripts/videogen.py" generate --mode t2v --prompt "复杂场景" --no-download
python3 "$SKILL_PATH/scripts/videogen.py" task --task-id <taskId>
```

## 首次使用

### 1. 安装依赖

```bash
pip3 install requests
```

### 2. 一键初始化（推荐）

```bash
python3 "$SKILL_PATH/scripts/videogen.py" init --skill-name seeding-video
```

脚本会自动打开 AI Hive 页面，引导登录、新建并复制 API Key，然后写入 `~/.ai-hive/config.json`（权限 0600）。

### 3. 手动获取 API Key（备选）

1. 访问 [https://ai-hive.iclip.cn/chat](https://ai-hive.iclip.cn/chat)
2. 使用手机号和短信验证码登录
3. 点击左下角账户菜单
4. 点击「API 接入」
5. 输入名称并点击「新建 API Key」
6. 复制完整 Key（格式为 `sk-api-*`）

### 4. 手动配置 API Key（备选）

| 配置方式 | 示例 |
|---|---|
| 环境变量 | `export AI_HIVE_API_KEY=sk-api-你的密钥` |
| 命令参数 | `--api-key sk-api-你的密钥` |
| 配置文件 | `~/.ai-hive/config.json` |

### 5. 验证配置

运行一个带 `--no-download` 的最简任务；返回 `taskId` 即配置成功。


## 使用指南

### 提示词结构

按“主体与场景 → 动作顺序 → 相机运动 → 光线风格 → 声音 → 限制 → 输出规格”组织提示词。区分主体动作和镜头动作，避免互相矛盾。

### 模型与价格

脚本先查询实时模型列表，再从同一模型和路由提取 `pricingSnapshot`。价格以提交当次返回为准，不写死固定优惠。

### 任务管理

生成调用返回 `taskId` 后只轮询原任务。本地超时不等于生成失败，不要重复提交可能已经计费的任务。

### 媒体上传

脚本自动执行上传凭证、对象存储上传和完成确认。媒体限制以实时 `videoConfig` 为准。

## 命令速查

| 命令 | 功能 |
|---|---|
| `videogen.py generate --mode t2v --prompt "描述"` | 执行本 Skill |
| `videogen.py task --task-id <id>` | 查询任务 |
| `videogen.py upload --file media.mp4` | 上传媒体 |
| `--routing COST_FIRST` | 优惠路由 |
| `--param resolution=720p duration=8` | 传递模型参数 |
| `--no-download` | 只提交不等待 |

## 项目架构

### 目录结构

```
seeding-video/
├── SKILL.md
├── CHANGELOG.md
├── scripts/
│   └── videogen.py
└── references/
    └── config.example.json
```

### 技术栈

| 组件 | 技术 |
|---|---|
| 运行环境 | Python 3.6+ |
| HTTP 库 | requests |
| API 平台 | AI Hive OpenAPI |
| 模型 | 种草视频 |
| 输出 | MP4 / MOV 或模型实时支持格式 |

### 核心模块

| 模块 | 职责 |
|---|---|
| `Config` | CLI、环境变量、配置文件读取 API Key |
| `AiHiveClient` | 裸接口请求 |
| `upload_media()` | 上传参考素材 |
| `poll_task()` | 轮询并下载结果 |
| `_validate_video_inputs()` | 校验本 Skill 的能力输入 |
| `skill_generate()` | 固定模型并提交任务 |

### 模式 → 模型映射

| 模式 | publicModelId |
|---|---|
| `t2v` | 文生视频 | `public_model_seedance_2_5_t2v` |
| `i2v` | 图生视频 | `public_model_seedance_2_5_i2v` |
| `r2v` | 参考生视频 | `public_model_seedance_2_5_r2v` |

### 数据流转

```
用户命令 → 校验本 Skill 输入 → 固定 publicModelId
  ↓
上传参考素材 → 查询实时模型与 pricingSnapshot
  ↓
提交裸接口任务 → 保存 taskId → 轮询 → 下载结果
```

### 价格参考

默认使用 `COST_FIRST`。模型价格与活动会变化，以脚本运行时查询到的实时价格和实际扣费为准。

## 常见问答

### 安装相关

**Q1：需要 API Key 吗？** 需要，格式为 `sk-api-*`。

**Q2：需要什么依赖？** Python 3 和 `requests`。

**Q3：如何验证？** 用 `--no-download` 提交最简任务，返回 taskId 即成功。

### 使用相关

**Q4：这个 Skill 会换模型吗？** 不会。模型能力 Skill 固定 publicModelId；多模式模型入口只在同一模型家族内切换。

**Q5：参数怎么设置？** 使用 `--param key=value`，并以实时 `videoConfig` 为准。

**Q6：结果保存在哪里？** 默认 `~/Downloads/AiHive/`。

**Q7：任务需要多久？** 通常数分钟，取决于队列和复杂度。

**Q8：任务超时怎么办？** 保留 taskId，使用 `task` 继续查询。

**Q9：可以批量吗？** 可以逐任务执行；明显高成本批量任务应先确认数量和实时费用。

### 故障排除

**Q10：提示缺少媒体？** 按 Skill 名称提供对应的首帧、图片、视频或音频。

**Q11：提示模型不存在？** 后台可能已下线或更名，请先查询实时模型列表。

**Q12：提示 401？** 检查 API Key 是否正确、过期或禁用。

**Q13：提示 InvalidParameter？** 检查实时 `videoConfig` 中的格式、数量、时长、比例、分辨率和编码限制。
