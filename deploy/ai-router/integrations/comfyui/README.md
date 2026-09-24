# 客户 ComfyUI 的视频节点

把 `siyuan_media/` 放到客户 ComfyUI 的 `custom_nodes/`，在该 ComfyUI 的 Python 环境安装 `siyuan_media/requirements.txt`，然后按客户自己的维护流程重启。

ComfyUI 进程环境需要 `SIYUAN_API_BASE`（现有 Router 的 `/v1` 地址）和 `SIYUAN_API_KEY`（现有账号签发的 Key）。凭证不会进入节点参数或工作流；账号需有 `siyuan-video` 授权。显式选择 `minimax-h3` 时，需由内部账号单独授予该模型。

`SIYUAN 单次视频生成` 节点接收提示词、模型、时长、画幅、seed、唯一 `request_id` 和可选首帧。它输出 VIDEO、文件路径与任务 ID，后面可以连接客户自己的保存或后期节点。客户不能指定 GPU、采样步数或服务端配方。

节点从 `/v1/media/options` 读取已验收组合。当前 Ivan 候选仅接受**带首帧、15 秒、16:9**；无首帧、其他时长或竖屏会在新提交前提示，服务端仍做最终校验。能力读取失败时旧工作流可以打开，由服务端决定是否接单。

新作品使用新的 `request_id`；断线后沿用原 ID。回执保存在客户 ComfyUI 输出目录的 `siyuan_media/`，不含 Key。已有回执直接查询原任务，即使设备后来关闭资格，也不会重新生成。停止客户工作流只会停止本地等待；后台取消使用同账号的 `/v1/videos/{id}/cancel`。

首帧与视频画幅不同时，当前预览配方会居中裁剪。方形首帧的上下边缘可能被截掉；输出技术完整性与画面质量分别验收。
