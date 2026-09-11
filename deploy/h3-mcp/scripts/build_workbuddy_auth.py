import argparse
import json
from pathlib import Path
import zipfile


NAME = "siyuan-h3-connect"
SERVER = "siyuan-h3-studio"


def declaration():
    return {"mcpServers": {SERVER: {
        "type": "http", "url": "https://ai-x10drg.taild500c8.ts.net:4001/mcp/h3", "timeout": 30000,
        "headers": {"Authorization": "Bearer ${H3_ACCESS_TOKEN}"},
        "x-workbuddy": {"displayName": {"zh": "H3 工作室", "en": "H3 Studio"},
            "description": {"zh": "四配方15秒竖版视频，先确认提示词，再明确启动。", "en": "Four recipes, explicit prompt approval before generation."},
            "icon": "./avatars/h3.svg",
            "auth": {"type": "token", "tokenSchema": {
                "title": {"zh": "H3 私人访问令牌", "en": "H3 private access token"},
                "description": {"zh": "从 H3 设置页创建专用 Token。只填入本原生表单，不发送到聊天。", "en": "Create a dedicated token in H3 settings. Enter it only in this native form, never in chat."},
                "docUrl": "https://ai-x10drg.taild500c8.ts.net:8445/mcp-settings.html",
                "docLabel": {"zh": "打开 H3 设置创建 Token", "en": "Open H3 settings"},
                "fields": [{"key": "H3_ACCESS_TOKEN", "label": {"zh": "H3 专用 Token", "en": "H3 token"},
                    "type": "password", "required": True,
                    "placeholder": {"zh": "不包含 Bearer 前缀", "en": "Without the Bearer prefix"}}]}}}}}}


def package_files(source):
    prompt = {"zh": "仅检查 H3 连接和能力，不保存任务、不确认提示词、不生成视频。", "en": "Check H3 connectivity and capabilities only. Do not create or approve tasks or generate videos."}
    metadata = {"name": NAME, "version": "0.2.0", "description": "Native token onboarding for the private H3 MCP.",
        "author": {"name": "H3 Studio"}, "agents": ["./agents/" + NAME + ".md"], "expertType": "agent", "agentName": NAME,
        "skills": ["./skills/h3-studio"], "dependencies": {"mcpServers": "./.mcp.json"},
        "displayName": {"zh": "H3 工作室连接向导", "en": "H3 Studio connection guide"},
        "profession": {"zh": "私人视频工作室接入", "en": "Private video studio onboarding"},
        "displayDescription": {"zh": "通过原生私密令牌表单接入个人H3视频工作室，检查四配方能力与队列，并严格保留提示词和视频的人工确认。", "en": "Connect through a native private token form, inspect capabilities, and preserve manual prompt and video approval."},
        "avatar": "avatars/h3.svg", "categoryId": "06-ContentCreative", "defaultInitPrompt": prompt, "plugin": NAME,
        "tags": [{"zh": "视频", "en": "Video"}, {"zh": "人工确认", "en": "Approval"}, {"zh": "MCP", "en": "MCP"}],
        "quickPrompts": [prompt, {"zh": "只查看 H3 当前容量。", "en": "Read current H3 capacity only."},
            {"zh": "只列出我的 H3 任务。", "en": "List my H3 tasks only."}]}
    agent = """---
name: siyuan-h3-connect
description: Connect to the private H3 studio using native credential guidance and inspect its capabilities.
maxTurns: 6
---

# H3 工作室连接向导

凭证必须通过 WorkBuddy 的原生依赖 Token 表单输入。不索取或读取用户 Token，
不通过聊天、Shell、配置文件或第三方脚本处理凭证，不编辑客户端审批记录。
本向导不增加生成后端；它只连接既有 siyuan-h3-studio MCP。
用户只要求连接时，发现工具后最多调用一次 h3_capabilities，报告真实能力后停止。
不创建草稿、不批准提示词、不启动生成、不自动轮询。连接不等于任务批准。
后续明确的视频任务才使用随包的 h3-studio 操作 Skill，优先用户已有创作 Skill。
参数、配方及额外人物设置必须展示，修改后旧确认失效。视频质量由用户评价。
失败时报告错误和设置页入口，不改配方、不绕过保护、不调用云端生成脚本。
"""
    encode = lambda value: (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    return {".codebuddy-plugin/plugin.json": encode(metadata), ".mcp.json": encode(declaration()),
        "agents/" + NAME + ".md": agent.encode(), "avatars/h3.svg": (source / "icon.svg").read_bytes(),
        "skills/h3-studio/SKILL.md": (source / "skills/h3-studio/SKILL.md").read_bytes(),
        "skills/h3-studio/references/multimodal.md": (source / "skills/h3-studio/references/multimodal.md").read_bytes(),
        "README.md": "# H3 原生认证接入\n\n导入此专家包并打开连接向导，填写原生 Token 表单。不要仅把根目录 .mcp.json 复制到全局配置；那样不会安装凭证表单。\n".encode()}


def build(destination, source):
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in package_files(source).items():
            item = zipfile.ZipInfo(name, date_time=(2026, 9, 10, 0, 0, 0))
            item.compress_type = zipfile.ZIP_DEFLATED
            item.external_attr = 0o100644 << 16
            archive.writestr(item, content)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    arguments = parser.parse_args()
    source = Path(__file__).resolve().parents[2] / "ai-router/integrations/workbuddy/h3-studio"
    build(arguments.destination, source)
