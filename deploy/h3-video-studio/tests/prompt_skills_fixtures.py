import asyncio
import json

import httpx


def complex_draft():
    descriptions = [
        (0, 2, "仓库门口，女主灰夹克在左，男主黑大衣在右", "中景，冷色侧光", "男主：带来了吗？", "通风机低鸣"),
        (2, 4, "女主取出唯一的黑色圆柱形音箱，右腕红绳可见", "近景，轻微推进", "女主：就差它了。", "帆布摩擦声弱于对白"),
        (4, 6, "音箱放在木箱中心，女主右手退出", "产品特写，位置与方向锁定", "", "接触木箱的轻响"),
        (6, 9, "男主推开身后的门，露出暖光排练场地", "沿原轴线拉远，音箱留在前景", "男主：开场。", "门轴声，对白后留白"),
        (9, 12, "女主按下音箱按钮，两人相视一笑", "中景，人物不换装不换位", "", "按钮声后响起原创鼓点"),
        (12, 15, "音箱原地静立，人物虚化，无字幕、价格或新标识", "产品特写，最后3秒锁定机位", "", "同一鼓点自然收束"),
    ]
    return {"title": "《交接》复杂测试夹具", "summary": "看似神秘交接，原来是两人准备排练。此文本为测试预设，不是真实模型输出。",
            "shots": [dict(zip(("start", "end", "visual", "camera", "dialogue", "sound"), row)) for row in descriptions],
            "music": "9秒后鼓点来自场内音箱，无额外旁白", "continuity": ["同一个音箱，不改变方向和尺寸", "人物服装与左右位置固定"],
            "questions": [], "assumptions": ["人物与外观均为虚构测试设定，未读取图片"]}


async def respond(request):
    body = json.loads(request.content)
    context = json.loads(body["messages"][1]["content"])["TaskEnvelope"]
    instruction = context["revision_instruction"]
    await asyncio.sleep(4 if "慢请求测试" in context["objective"] else .15)
    if request.headers["x-request-id"].endswith("_skills"):
        value = {"skill_ids": ["short-drama-video", "ai-ad-video", "ecommerce-video"], "reason": "【测试预设，非真实模型选择】短剧负责反转与人物，创意广告负责揭示，电商规则约束产品展示。"}
    else:
        value = complex_draft()
        if "六镜都保留" in instruction:
            value["questions"] = ["六镜每镜至少2秒、末镜6秒，至少需要16秒；是否允许减少镜头？"]
        elif "五镜" in instruction:
            value["shots"].pop(4)
            value["shots"][-1].update(start=9, camera="固定产品特写，最后6秒无运镜")
    return httpx.Response(200, json={"model": "TEST-FIXTURE-NOT-REAL-MODEL", "choices": [{"message": {"content": json.dumps(value, ensure_ascii=False)}}]},
                          headers={"x-1panel-route-request-id": "fixture-" + request.headers["x-request-id"]})
