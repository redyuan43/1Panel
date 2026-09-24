"""ComfyUI nodes: client-side composition around one remote generation."""
from __future__ import annotations

import io

from .client import MediaClient


def reference_png(image):
    if image is None:
        return None
    from PIL import Image
    import numpy as np
    if len(image) != 1:
        raise ValueError("首版仅支持一张参考图，请先选择一帧。")
    buffer = io.BytesIO()
    Image.fromarray(np.clip(image[0].cpu().numpy() * 255, 0, 255).astype(np.uint8)).save(buffer, format="PNG")
    return buffer.getvalue()


def generate(kind, body, request_id, image=None):
    import folder_paths
    import comfy.model_management
    client = MediaClient(folder_paths.get_output_directory())
    try:
        return client.generate(kind, body, request_id, reference=reference_png(image),
                               check_interrupt=comfy.model_management.throw_exception_if_processing_interrupted)
    finally:
        client.close()


class SiyuanImage:
    CATEGORY = "SIYUAN/API"
    FUNCTION = "run"
    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("image", "job_id")

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "prompt": ("STRING", {"multiline": True}),
            "model": (["siyuan-image", "qwen-image-2.1"],),
            "aspect_ratio": (["auto", "square", "landscape", "portrait"],),
            "request_id": ("STRING", {"default": "", "tooltip": "新作品填唯一值；重试保持原值。"}),
        }, "optional": {"reference_image": ("IMAGE",)}}

    def run(self, prompt, model, aspect_ratio, request_id, reference_image=None):
        import torch
        import numpy as np
        from PIL import Image
        path, identifier = generate("image", {"model": model, "prompt": prompt, "aspect_ratio": aspect_ratio},
                                    request_id, reference_image)
        with Image.open(path) as image:
            pixels = np.array(image.convert("RGB"), dtype=np.float32) / 255.0
        return torch.from_numpy(pixels)[None, ...], identifier


class SiyuanVideo:
    CATEGORY = "SIYUAN/API"
    FUNCTION = "run"
    RETURN_TYPES = ("VIDEO", "STRING", "STRING")
    RETURN_NAMES = ("video", "file_path", "job_id")

    @classmethod
    def INPUT_TYPES(cls):
        # ComfyUI asks for widgets before execution. A failed options read must
        # not prevent loading a saved graph; the server remains authoritative.
        durations, ratios = list(range(4, 16)), ["16:9", "9:16"]
        models = ["siyuan-video", "minimax-h3"]
        try:
            import folder_paths
            client = MediaClient(folder_paths.get_output_directory())
            try:
                options = client.video_options()
                capabilities = client.video_capabilities(options)
            finally:
                client.close()
            if capabilities:
                durations = sorted({item["duration"] for item in capabilities})
                ratios = sorted({item["aspect_ratio"] for item in capabilities})
            allowed = [model for model in models if model in options.get("models", [])]
            if allowed:
                models = allowed
        except Exception:
            pass
        return {"required": {
            "prompt": ("STRING", {"multiline": True}),
            "model": (models,),
            "duration": (durations,),
            "aspect_ratio": (ratios, {"tooltip": "首帧比例与视频不同时，当前配方会居中裁剪；边缘内容可能被截掉。"}),
            "seed": ("INT", {"default": -1, "min": -1, "max": 2**63 - 1}),
            "request_id": ("STRING", {"default": "", "tooltip": "新作品填唯一值；重试保持原值。"}),
        }, "optional": {"first_frame": ("IMAGE",)}}

    def run(self, prompt, model, duration, aspect_ratio, seed, request_id, first_frame=None):
        from comfy_api.input_impl import VideoFromFile
        mode = "i2v" if first_frame is not None else "t2v"
        path, identifier = generate("video", {
            "model": model, "prompt": prompt, "duration": duration, "aspect_ratio": aspect_ratio,
            "seed": seed, "mode": mode, "workflow_mode": "single_generation",
        }, request_id, first_frame)
        return VideoFromFile(str(path)), str(path), identifier


NODE_CLASS_MAPPINGS = {"SiyuanImage": SiyuanImage, "SiyuanVideo": SiyuanVideo}
NODE_DISPLAY_NAME_MAPPINGS = {"SiyuanImage": "SIYUAN 图片生成 / 编辑", "SiyuanVideo": "SIYUAN 单次视频生成"}
