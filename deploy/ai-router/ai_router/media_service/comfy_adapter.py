"""Private, durable ComfyUI image executor; one deployment per ComfyUI runtime.

Run with uvicorn ai_router.media_service.comfy_adapter:create_app --factory.
The operator owns recipe JSON. API callers can supply only model parameters.
"""
from __future__ import annotations

import asyncio
import copy
import fcntl
import hmac
import io
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image

from .contracts import ID_PATTERN, MediaError, TERMINAL, UnknownOutcome, decode_asset, fingerprint, image_info, image_request
from .image_direct import private_url
from .storage import MediaStore


class ComfyExecutor:
    def __init__(self, root, config, *, client=None):
        self.store = MediaStore(root)
        self.client = client or httpx.AsyncClient(trust_env=False, follow_redirects=False)
        self.base = private_url(config["comfy_url"])
        self.recipes = {}
        if config.get("version") != 1:
            raise ValueError("Unsupported ComfyUI adapter configuration")
        for recipe in config["recipes"]:
            identifier = recipe["id"]
            if not ID_PATTERN.fullmatch(identifier) or identifier in self.recipes:
                raise ValueError("Invalid or duplicate recipe identifier")
            if recipe.get("model") != "qwen-image-2.1" or recipe.get("mode") not in {"t2i", "i2i"}:
                raise ValueError("Unsupported image recipe")
            graph = json.loads(Path(recipe["graph_file"]).read_text())
            if not isinstance(graph, dict) or not graph:
                raise ValueError("Recipe requires a ComfyUI API-format graph")
            for name, bindings in recipe["bindings"].items():
                if name not in {"prompt", "seed", "width", "height", "images"}:
                    raise ValueError("Unsupported recipe binding")
                for node, field in bindings:
                    if node not in graph or field not in graph[node].get("inputs", {}):
                        raise ValueError("Recipe binding does not exist in graph")
            if not recipe["bindings"].get("prompt") or not recipe["bindings"].get("seed"):
                raise ValueError("Recipes must bind prompt and seed")
            if recipe["output_node"] not in graph:
                raise ValueError("Recipe output node does not exist")
            self.recipes[identifier] = {**recipe, "graph": graph, "graph_digest": fingerprint(graph)}
        self.lock = asyncio.Lock()
        self.runner = None
        self.worker_lock = None

    async def request(self, method, path, **kwargs):
        response = await self.client.request(method, self.base + path, timeout=30, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else {}

    def get(self, operation_id):
        if not isinstance(operation_id, str) or not ID_PATTERN.fullmatch(operation_id):
            raise MediaError("invalid_operation_id", "Invalid execution identifier.")
        with self.store.connect() as db:
            row = db.execute("SELECT value FROM jobs WHERE owner='adapter' AND idem=?", (operation_id,)).fetchone()
        if not row:
            raise MediaError("execution_not_found", "Execution does not exist.", 404)
        return json.loads(row[0])

    def accept(self, value):
        if not isinstance(value, dict) or set(value) != {"operation_id", "recipe", "request"}:
            raise MediaError("invalid_execution", "An operation, recipe and model request are required.")
        if not isinstance(value["operation_id"], str) or not ID_PATTERN.fullmatch(value["operation_id"]):
            raise MediaError("invalid_operation_id", "Invalid execution identifier.")
        raw = value["request"]
        if not isinstance(raw, dict):
            raise MediaError("invalid_execution", "Image request must be an object.")
        body = image_request(raw, edit=bool(raw.get("images")))
        request = {**value, "request": body, "model": body["model"]}
        if (existing := self.store.replay("adapter", "image", request, value["operation_id"])) is not None:
            return self.public(existing)
        recipe = self.recipes.get(value["recipe"])
        if not recipe or body["model"] not in {recipe["model"], "siyuan-image"}:
            raise MediaError("recipe_unavailable", "Recipe is unavailable.", 422)
        if (recipe["mode"] != ("i2i" if body["images"] else "t2i")
                or body["aspect_ratio"] not in recipe["aspect_ratios"]
                or body["background"] not in recipe["backgrounds"]
                or len(body["images"]) != len(recipe["bindings"].get("images", []))):
            raise MediaError("recipe_incompatible", "Recipe does not support these parameters.", 422)
        job, created = self.store.create("adapter", "image", request, value["operation_id"], value["operation_id"], 1,
                                         policy={"image_max_active": 1}, recipe_snapshot=recipe)
        # Admission and the immutable graph snapshot commit atomically.
        return self.public(job)

    async def capabilities(self):
        try:
            info = await self.request("GET", "/object_info")
            queue = await self.request("GET", "/queue")
            recipes = [key for key, recipe in self.recipes.items()
                       if all(node["class_type"] in info for node in recipe["graph"].values())]
            busy = bool(self.store.active() or queue.get("queue_running") or queue.get("queue_pending"))
            return {"version": 1, "recipes": recipes, "busy": busy, "ready": bool(recipes) and not busy}
        except (httpx.HTTPError, ValueError, KeyError):
            return {"version": 1, "recipes": [], "ready": False}

    @staticmethod
    def public(job):
        return {"execution_id": job["request"]["operation_id"], "status": job["status"],
                "error": job.get("error"), "progress": 100 if job["status"] == "completed" else 0}

    async def graph(self, job):
        recipe = job["recipe_snapshot"]
        graph = copy.deepcopy(recipe["graph"])
        body = job["request"]["request"]
        state = job["provider_state"]
        dimensions = recipe["aspect_ratios"][body["aspect_ratio"]]
        values = {"prompt": body["prompt"] + "\nImage use case: " + body["use_case"],
                  "seed": state["seed"], "width": dimensions[0], "height": dimensions[1]}
        for key, value in values.items():
            for node, field in recipe["bindings"].get(key, []):
                graph[node]["inputs"][field] = value
        for index, asset in enumerate(body["images"]):
            # Input names are deterministic and scoped to this execution.
            data = decode_asset(asset, image=True)
            extension = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[asset["content_type"]]
            name = job["id"] + "_" + str(index) + extension
            uploaded = await self.request("POST", "/upload/image", data={"type": "input", "overwrite": "true"},
                                          files={"image": (name, data, asset["content_type"])})
            subfolder = uploaded.get("subfolder", "")
            node, field = recipe["bindings"]["images"][index]
            graph[node]["inputs"][field] = (subfolder + "/" if subfolder else "") + uploaded["name"]
        return graph

    async def recover_prompt(self, job):
        state = job["provider_state"]
        if state.get("prompt_id"):
            return state["prompt_id"]
        # Older ComfyUI versions allocate prompt_id themselves. The operation
        # marker in extra_data survives queue/history and recovers a lost reply.
        queue = await self.request("GET", "/queue")
        rows = queue.get("queue_running", []) + queue.get("queue_pending", [])
        history = await self.request("GET", "/history")
        rows += [entry.get("prompt", []) for entry in history.values()]
        for row in rows:
            if len(row) > 3 and row[3].get("siyuan_operation_id") == job["request"]["operation_id"]:
                return row[1]
        raise UnknownOutcome()

    async def step(self, job):
        async with self.lock:
            job = self.store.get(job["id"])
            if job["status"] in TERMINAL:
                return
            state = dict(job["provider_state"])
            finishing = False
            try:
                if not state.get("submitted"):
                    if job.get("cancel_requested"):
                        self.store.update(job["id"], status="cancelled")
                        return
                    queue = await self.request("GET", "/queue")
                    if queue.get("queue_running") or queue.get("queue_pending"):
                        return
                    if not job.get("recipe_snapshot"):
                        # Crash before snapshot commit: there cannot be a prompt.
                        self.store.update(job["id"], status="failed", error="recipe_snapshot_missing")
                        return
                    state.setdefault("seed", secrets.randbelow(2**63))
                    job = self.store.update(job["id"], provider_state=state)
                    graph = await self.graph(job)
                    if self.store.get(job["id"]).get("cancel_requested"):
                        self.store.update(job["id"], status="cancelled")
                        return
                    state["submitted"] = True
                    self.store.update(job["id"], status="in_progress", provider_state=state)
                    response = await self.client.post(self.base + "/prompt", timeout=30, json={
                        "prompt": graph, "client_id": "siyuan-media-adapter",
                        "extra_data": {"siyuan_operation_id": job["request"]["operation_id"]},
                    })
                    if response.status_code == 400:
                        self.store.update(job["id"], status="failed", error="comfy_prompt_rejected")
                        return
                    response.raise_for_status()
                    state["prompt_id"] = response.json()["prompt_id"]
                    self.store.update(job["id"], provider_state=state)
                prompt_id = await self.recover_prompt(self.store.get(job["id"]))
                state["prompt_id"] = prompt_id
                self.store.update(job["id"], provider_state=state)
                history = await self.request("GET", "/history/" + prompt_id)
                result = history.get(prompt_id)
                if result:
                    status = result.get("status", {})
                    if status.get("status_str") == "error":
                        self.store.update(job["id"], status="failed", error="comfy_execution_failed")
                    elif status.get("completed"):
                        finishing = True
                        await self.archive(job, result)
                    return
                queue = await self.request("GET", "/queue")
                if self.store.get(job["id"]).get("cancel_requested"):
                    if state.get("cancel_delete_sent") or any(row[1] == prompt_id for row in queue.get("queue_pending", [])):
                        state["cancel_delete_sent"] = True
                        self.store.update(job["id"], provider_state=state)
                        # Deleting this exact queued ID is idempotent. Retry a
                        # lost deletion acknowledgement even when it is absent.
                        await self.request("POST", "/queue", json={"delete": [prompt_id]})
                        remaining = await self.request("GET", "/queue")
                        if not any(row[1] == prompt_id for row in remaining.get("queue_pending", []) + remaining.get("queue_running", [])):
                            finished = (await self.request("GET", "/history/" + prompt_id)).get(prompt_id)
                            if finished and finished.get("status", {}).get("completed"):
                                finishing = True
                                await self.archive(job, finished)
                            elif finished and finished.get("status", {}).get("status_str") == "error":
                                self.store.update(job["id"], status="failed", error="comfy_execution_failed")
                            else:
                                self.store.update(job["id"], status="cancelled")
                    # Never call the global /interrupt: it can kill a manual job.
                    elif not any(row[1] == prompt_id for row in queue.get("queue_running", [])):
                        self.uncertain(job["id"])
                elif any(row[1] == prompt_id for row in queue.get("queue_pending", []) + queue.get("queue_running", [])):
                    self.store.update(job["id"], status="in_progress", error=None, reconcile_since=None)
                else:
                    self.uncertain(job["id"])
            except asyncio.CancelledError:
                raise
            except MediaError as exc:
                if finishing and exc.code in {"invalid_image", "media_too_large"}:
                    self.store.update(job["id"], status="failed", error="invalid_image_output")
                else:
                    self.uncertain(job["id"])
            except Exception:
                self.uncertain(job["id"])

    def uncertain(self, identifier):
        since = self.store.get(identifier).get("reconcile_since") or time.time()
        self.store.update(identifier, status="reconciling", reconcile_since=since,
                          error="operator_verification_required" if time.time() - since >= 600 else "checking_original_prompt")

    async def archive(self, job, result):
        outputs = result.get("outputs", {}).get(job["recipe_snapshot"]["output_node"], {}).get("images", [])
        if len(outputs) != 1:
            self.store.update(job["id"], status="failed", error="invalid_image_output")
            return
        output = outputs[0]
        if output.get("type") != "output":
            self.store.update(job["id"], status="failed", error="invalid_image_output")
            return
        data = bytearray()
        async with self.client.stream("GET", self.base + "/view", params={
            key: output.get(key, "") for key in ("filename", "subfolder", "type")
        }, timeout=120) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > 32 * 1024 * 1024:
                    raise MediaError("media_too_large", "Generated image exceeds its limit.")
        info = image_info(bytes(data))
        # ComfyUI PNGs may carry the entire graph. Return pixels without metadata.
        clean = io.BytesIO()
        with Image.open(io.BytesIO(data)) as image:
            if job["request"]["request"]["background"] == "opaque" and info["transparent"]:
                image = Image.alpha_composite(Image.new("RGBA", image.size, "white"), image.convert("RGBA"))
                image.convert("RGB").save(clean, format="PNG")
            else:
                image.convert("RGBA" if info["transparent"] else "RGB").save(clean, format="PNG")
        path = self.store.root / (job["id"] + ".png")
        pending = path.with_suffix(".part")
        with pending.open("wb") as handle:
            handle.write(clean.getvalue())
            handle.flush()
            os.fsync(handle.fileno())
        pending.chmod(0o600)
        pending.replace(path)
        self.store.update(job["id"], status="completed", output_path=str(path), error=None)

    async def cancel(self, identifier):
        job = self.get(identifier)
        if job["status"] not in TERMINAL:
            job = self.store.update(job["id"], cancel_requested=True,
                                    status="cancelling" if job["provider_state"].get("submitted") else "cancelled")
        return self.public(job)

    async def run(self):
        while True:
            for job in self.store.active():
                await self.step(job)
            await asyncio.sleep(3)

    async def start(self):
        self.worker_lock = (self.store.root / "adapter.lock").open("a")
        fcntl.flock(self.worker_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.runner = asyncio.create_task(self.run())

    async def close(self):
        if self.runner:
            self.runner.cancel()
            await asyncio.gather(self.runner, return_exceptions=True)
        await self.client.aclose()
        if self.worker_lock:
            self.worker_lock.close()


def create_app(executor=None, *, run_worker=True):
    @asynccontextmanager
    async def lifespan(app):
        if not executor:
            config = json.loads(Path(os.environ["COMFY_ADAPTER_CONFIG"]).read_text())
            app.state.executor = ComfyExecutor(os.environ["COMFY_ADAPTER_ROOT"], config)
        if run_worker:
            await app.state.executor.start()
        yield
        await app.state.executor.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
    if executor:
        app.state.executor = executor

    @app.middleware("http")
    async def protect(request, call_next):
        key = os.environ.get("COMFY_ADAPTER_KEY", "")
        if not key or not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + key):
            return JSONResponse({"error": {"code": "invalid_api_key"}}, status_code=401)
        received, receive = 0, request._receive
        async def bounded_receive():
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > 72 * 1024 * 1024:
                raise MediaError("media_too_large", "Request exceeds the image upload limit.", 413)
            return message
        request._receive = bounded_receive
        return await call_next(request)

    @app.exception_handler(MediaError)
    async def error(request, exc):
        return JSONResponse(exc.payload(), status_code=exc.status)

    @app.get("/capabilities")
    async def capabilities(request: Request):
        return await request.app.state.executor.capabilities()

    @app.post("/executions")
    async def submit(request: Request):
        return JSONResponse(request.app.state.executor.accept(await request.json()), status_code=202)

    @app.get("/executions/{operation_id}")
    async def get(operation_id: str, request: Request):
        current = request.app.state.executor
        return current.public(current.get(operation_id))

    @app.post("/executions/{operation_id}/cancel")
    async def cancel(operation_id: str, request: Request):
        return await request.app.state.executor.cancel(operation_id)

    @app.get("/executions/{operation_id}/output")
    async def output(operation_id: str, request: Request):
        job = request.app.state.executor.get(operation_id)
        if job["status"] != "completed":
            raise MediaError("output_not_ready", "Execution has no completed output.", 409)
        return FileResponse(job["output_path"], media_type="image/png", headers={"Cache-Control": "no-store"})

    return app
