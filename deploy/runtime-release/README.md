# 固定运行版本

`release.py` 将已提交的 1Panel 文件与经过白名单限制的外部运行快照合成一次性发布目录。Git 文件通过对象数据库读取，不读取工作树；外部快照没有伪造 commit，而以逐文件哈希和整体 `release_sha256` 标识。正式 H3 使用已验收的合成快照，Preview 使用同一 commit 中的基础 Studio 组件，避免把仅适用于 Fleet 的运行覆盖带入模拟客户端。

准备当前生产包：

```bash
python3 deploy/runtime-release/release.py prepare \
  --repository /home/ai/github/1Panel \
  --revision <完整提交哈希> \
  --profiles deploy/runtime-release/profiles.json \
  --snapshot-source h3-studio=/path/to/accepted-h3-release \
  --snapshot-source qwen38=/home/ai/github/qwen38-vllm-deploy \
  --external-source h3-venv=/home/ai/.local/state/h3-studio-ivan-runtime/venv \
  --external-source qwen-python=/home/ai/runtimes/python-3.12.14-20260901 \
  --external-source qwen-venv=/home/ai/venvs/1cat-vllm-1.5.0-lmcache \
  --output /home/ai/.local/state/1panel-runtime/releases/<release-name>
```

准备命令拒绝覆盖已有目录、路径逃逸和入选的符号链接。发布目录创建后设为只读。模型、venv、缓存、状态数据库和密钥均不进入发布包。

切换前执行：

```bash
python3 /home/ai/.local/state/1panel-runtime/releases/<release-name>/tools/release.py verify \
  --release /home/ai/.local/state/1panel-runtime/releases/<release-name>
```

验证通过后，以原子符号链接把 `current` 指向新目录，安装本目录中对应的 systemd 模板，并按服务依赖顺序重启。正式 Studio 还必须安装 `systemd/h3-studio-ivan.service.d/zzzz-runtime-release.conf`，它的优先级高于现场已有的 `zz-multinode-*` 覆盖项。切换前保存 `systemctl --user cat` 输出；验收必须读取 systemd 的实际 `WorkingDirectory` 和进程 `/proc/<pid>/cwd`，不能只检查模板。

H3 服务启动前使用 `verify --external-component h3-venv`；Qwen 与 LMCache 使用 `verify --external-component qwen-python --external-component qwen-venv`。Profile 将这些来源绑定到 systemd 实际使用的绝对路径；清单覆盖的解释器、vLLM、LMCache 和关键依赖文件发生变化时启动失败。该白名单指纹不表示模型权重或整个 venv 已冻结。回滚需同时恢复上一 `current`、上一 Router 镜像和上一有效 systemd 配置；不要重放未知请求。

Router 镜像必须从指定 commit 的 Git archive 构建，不能把工作树直接作为构建上下文：

```bash
python3 deploy/runtime-release/build_router_image.py \
  --repository /home/ai/github/1Panel \
  --revision <完整提交哈希> \
  --image 1panel-ai-router:<发布标签>
```

验收记录必须同时包含 Router 容器 image ID 和 OCI revision、`current` 的真实路径、`release_sha256`、systemd 的实际 `WorkingDirectory`/`ExecStart`，以及服务健康结果。
