"""Versioned CPU-only recipe contracts; runtime and hardware admission are separate."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re


DEFAULT_PATH = Path(__file__).resolve().parents[1] / "config/recipes.json"
DYNAMIC_INPUTS = {
    "prompt": ("6", "prompt"),
    "seed": ("8", "noise_seed"),
    "prefix": ("13", "filename_prefix"),
}
FORMAL = {"A4", "A4_C0", "A4_C1", "B8"}
RETIRED = {"R0", "C0", "C1", "D4", "A8", "A4_C05"}


def _encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: " + key)
        result[key] = value
    return result


def _structure(graph):
    if not isinstance(graph, dict):
        raise ValueError("recipe graph must be an API graph object")
    result = copy.deepcopy(graph)
    try:
        for node, field in DYNAMIC_INPUTS.values():
            result[node]["inputs"][field]
            result[node]["inputs"][field] = "__RECIPE_DYNAMIC__"
        return _digest(result)
    except (KeyError, TypeError, OverflowError) as error:
        raise ValueError("missing or invalid recipe graph inputs") from error


def _dynamic(prompt, seed, prefix):
    if not isinstance(prompt, str) or not prompt.strip() or "\x00" in prompt:
        raise ValueError("prompt must be nonempty text without NUL")
    if type(seed) is not int or not 0 <= seed < 2**64:
        raise ValueError("seed must be uint64")
    if (not isinstance(prefix, str)
            or re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", prefix) is None
            or any(part in {".", ".."} for part in prefix.split("/"))):
        raise ValueError("prefix must be a safe relative output path")


class RecipeCatalog:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else DEFAULT_PATH
        self._entries = {}
        self._templates = {}
        self.error = None
        try:
            self._load()
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            self._entries.clear()
            self._templates.clear()
            self.error = "recipe catalog unavailable: " + str(error)

    def _load(self):
        document = json.loads(self.path.read_bytes(), object_pairs_hook=_unique)
        if document.get("schema_version") != 1 or document.get("default_recipe_id") != "A4":
            raise ValueError("unsupported recipe catalog schema/default")
        entries = document.get("recipes", [])
        if (not isinstance(entries, list) or len(entries) != len(FORMAL)
                or {entry.get("recipe_id") for entry in entries} != FORMAL
                or set(document.get("retired_recipe_ids", [])) != RETIRED):
            raise ValueError("catalog must contain exactly the four formal recipes")
        self._entries = {}
        self._templates = {}
        for entry in entries:
            recipe_id = entry["recipe_id"]
            if not isinstance(entry.get("version"), str) or not entry["version"]:
                raise ValueError("recipe version is required")
            template_path = (self.path.parent / entry["template"]).resolve()
            if not template_path.is_relative_to(self.path.parent.resolve()):
                raise ValueError("template must remain inside catalog directory")
            raw = template_path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != entry["template_sha256"]:
                raise ValueError("pinned template SHA256 mismatch: " + recipe_id)
            graph = json.loads(raw, object_pairs_hook=_unique)
            if _structure(graph) != entry["structure_sha256"]:
                raise ValueError("pinned structure SHA256 mismatch: " + recipe_id)
            self._templates[recipe_id] = graph
            self._entries[recipe_id] = dict(entry, recipe_digest=_digest(entry))
            self.validate(recipe_id, graph)

    def get(self, recipe_id):
        if not isinstance(recipe_id, str):
            raise ValueError("recipe_id must be a string")
        if recipe_id in RETIRED:
            return {"recipe_id": recipe_id, "status": "retired", "selectable": False,
                    "history_preserved": True}
        if self.error:
            raise ValueError(self.error)
        if recipe_id not in self._entries:
            raise ValueError("unknown recipe: " + recipe_id)
        return copy.deepcopy(self._entries[recipe_id])

    def _active(self, recipe_id):
        entry = self.get(recipe_id)
        if entry.get("status") != "formal":
            raise ValueError("retired recipe cannot execute: " + recipe_id)
        return entry

    def public(self):
        keys = ("recipe_id", "label", "description", "version", "trigger",
                "people_lora_strength", "status", "shape", "sampling", "recipe_digest")
        return {"default_recipe_id": "A4", "recipes": [
            {key: copy.deepcopy(entry[key]) for key in keys}
            for entry in self._entries.values()], "retired_recipe_ids": sorted(RETIRED),
            "catalog_status": "unavailable" if self.error else "ready", "error": self.error}

    def build(self, recipe_id, prompt, seed, prefix):
        entry = self._active(recipe_id)
        _dynamic(prompt, seed, prefix)
        trigger = entry["trigger"]
        if trigger and not prompt.startswith(trigger):
            prompt = trigger + prompt
        graph = copy.deepcopy(self._templates[recipe_id])
        for name, value in (("prompt", prompt), ("seed", seed), ("prefix", prefix)):
            node, field = DYNAMIC_INPUTS[name]
            graph[node]["inputs"][field] = value
        return graph, self.validate(recipe_id, graph)

    def validate(self, recipe_id, graph, version=None):
        entry = self._active(recipe_id)
        if version is not None and version != entry["version"]:
            raise ValueError("recipe version mismatch")
        if _structure(graph) != entry["structure_sha256"]:
            raise ValueError("graph differs from the selected pinned recipe")
        prompt, seed, prefix = (graph[node]["inputs"][field] for node, field in DYNAMIC_INPUTS.values())
        _dynamic(prompt, seed, prefix)
        trigger = entry["trigger"]
        if trigger and (not prompt.startswith(trigger) or not prompt[len(trigger):].strip()
                        or prompt[len(trigger):].lstrip().startswith(trigger.strip())):
            raise ValueError("recipe requires exactly one leading realism trigger and prompt")
        return {"recipe_id": recipe_id, "recipe_version": entry["version"],
                "recipe_digest": entry["recipe_digest"], "graph_sha256": _digest(graph),
                "structure_sha256": entry["structure_sha256"],
                "template_sha256": entry["template_sha256"],
                "hardware_qualification": "not_evaluated"}

    def freeze(self, recipe_id, graph):
        binding = self.validate(recipe_id, graph)
        return copy.deepcopy(graph), binding
