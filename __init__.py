import asyncio
import json
import os
import random
import re
import shutil
import string
import subprocess
import sys
import uuid
from datetime import datetime

import numpy as np
from aiohttp import web
from PIL import Image
from PIL.PngImagePlugin import PngInfo

import folder_paths
from comfy.cli_args import args
from comfy_api.latest import io
from comfy_execution.utils import get_executing_context
from server import PromptServer

SLOT_RE = re.compile(r"image_(\d+)")
PREVIEW_RE = re.compile(r"saving_tools_[0-9a-f]{32}\.png")
INVALID_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
DATE_RE = re.compile(r"%date(?::([^%]*))?%")
DATE_PART_RE = re.compile(r"yyyy|yy|MM|dd|hh|mm|ss")
DATE_PARTS = {"yyyy": "%Y", "yy": "%y", "MM": "%m", "dd": "%d", "hh": "%H", "mm": "%M", "ss": "%S"}
# At most 3 digits, so a typo like %rand99999999% stays literal text instead of building a huge string.
RAND_RE = re.compile(r"%rand(\d{1,3})%")
RAND_CHARS = string.ascii_letters + string.digits
FOLDER_DIALOG = os.path.join(os.path.dirname(__file__), "folder_dialog.py")


class ImageTypes(tuple):
    # The frontend adds one output per connected input, so every output index past the declared ones is an IMAGE too.
    def __getitem__(self, index):
        if isinstance(index, int) and index >= len(self):
            return "IMAGE"
        return super().__getitem__(index)


class SelectiveImageSaver:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "optional": {
                "image_1": ("IMAGE",),
            },
            "hidden": {
                "prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"
            },
        }

    RETURN_TYPES = ImageTypes(("IMAGE",))
    RETURN_NAMES = ("image_1",)
    FUNCTION = "preview"
    OUTPUT_NODE = True
    CATEGORY = "image"
    DESCRIPTION = "Previews every connected image and lets you save each one to its own folder with a button."

    def preview(self, prompt=None, extra_pnginfo=None, **inputs):
        images = {int(m.group(1)): v for k, v in inputs.items() if (m := SLOT_RE.fullmatch(k))}

        metadata = None
        if not args.disable_metadata:
            metadata = PngInfo()
            if prompt is not None:
                metadata.add_text("prompt", json.dumps(prompt))
            if extra_pnginfo is not None:
                for x in extra_pnginfo:
                    metadata.add_text(x, json.dumps(extra_pnginfo[x]))

        workflow = ((extra_pnginfo or {}).get("workflow") or {}).get("id")
        temp_dir = folder_paths.get_temp_directory()
        os.makedirs(temp_dir, exist_ok=True)
        previews = []
        for slot in sorted(images):
            if images[slot] is None:
                continue
            if f"preview_image_{slot}" in inputs:
                previews.extend(inputs[f"preview_image_{slot}"])
                continue
            for image in images[slot]:
                i = 255. * image.cpu().numpy()
                img = Image.fromarray(np.clip(i, 0, 255).astype(np.uint8))
                filename = f"saving_tools_{uuid.uuid4().hex}.png"
                img.save(os.path.join(temp_dir, filename), pnginfo=metadata, compress_level=4)
                previews.append({"slot": f"image_{slot}", "filename": filename, "subfolder": "", "type": "temp", "workflow": workflow})

        result = tuple(images.get(i) for i in range(1, max(images, default=0) + 1))
        return {"ui": {"previews": previews, "preview_run": [get_executing_context().prompt_id]}, "result": result}


class SavingToolsPreview(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="SavingToolsPreview",
            inputs=[io.Image.Input("image"), io.String.Input("slot"), io.String.Input("node_id")],
            outputs=[io.Image.Output(is_output_list=True), io.AnyType.Output(is_output_list=True)],
            hidden=[io.Hidden.prompt, io.Hidden.extra_pnginfo],
            is_input_list=True,
            is_output_node=True,
            is_dev_only=True,
        )

    @classmethod
    def execute(cls, image, slot, node_id):
        slot, node_id = slot[0], node_id[0]
        saver = SelectiveImageSaver()
        batches = [saver.preview(prompt=cls.hidden.prompt, extra_pnginfo=cls.hidden.extra_pnginfo,
                                 **{slot: batch})["ui"]["previews"] for batch in image]
        output = {"previews": [p for batch in batches for p in batch], "preview_slot": [slot], "preview_node": [node_id],
                  "preview_run": [get_executing_context().prompt_id]}
        if PromptServer.instance.client_id is not None:
            PromptServer.instance.send_sync("executed", {
                "node": node_id, "display_node": node_id, "output": output,
                "prompt_id": get_executing_context().prompt_id,
            }, PromptServer.instance.client_id)
        return io.NodeOutput(image, batches, ui=output)


def format_name(fmt, slot, batch, now):
    name = DATE_RE.sub(lambda m: DATE_PART_RE.sub(lambda p: now.strftime(DATE_PARTS[p.group()]), m.group(1) or "yyyy-MM-dd"), fmt)
    name = RAND_RE.sub(lambda m: "".join(random.choices(RAND_CHARS, k=int(m.group(1)))), name)
    name = name.replace("%slot%", slot).replace("%batch%", str(batch))
    return INVALID_CHARS_RE.sub("_", name).strip(" .") or "ComfyUI"


def free_files(folder, names):
    # %counter% takes the next number after the files already matching the name. A name without it that is already
    # taken gets one appended, so only the "overwrite" button ever replaces a file.
    taken = {f.lower() for f in os.listdir(folder)}
    files = []
    for name in names:
        if "%counter%" not in name and f"{name}.png".lower() not in taken:
            file = f"{name}.png"
        else:
            parts = (name if "%counter%" in name else f"{name}_%counter%").split("%counter%")
            pattern = re.compile(r"(\d+)".join(re.escape(p) for p in parts) + r"\.png", re.IGNORECASE)
            counter = max((int(m.group(1)) for f in taken if (m := pattern.fullmatch(f))), default=0) + 1
            file = f"{counter:05}".join(parts) + ".png"
        taken.add(file.lower())
        files.append(os.path.join(folder, file))
    return files


@PromptServer.instance.routes.post("/saving_tools/save")
async def save_previews(request):
    data = await request.json()
    temp_dir = folder_paths.get_temp_directory()
    sources = []
    for name in data["files"]:
        path = os.path.join(temp_dir, name)
        if not PREVIEW_RE.fullmatch(name) or not os.path.isfile(path):
            return web.json_response({"error": "Aperçu introuvable, relancez le workflow."}, status=400)
        sources.append(path)

    targets = data.get("overwrite") or []
    if targets:
        if not all(os.path.isabs(p) and p.lower().endswith(".png") for p in targets):
            return web.json_response({"error": "Seuls des fichiers .png peuvent être écrasés."}, status=400)
        targets = targets[:len(sources)]
        folder = os.path.dirname(targets[0])
    else:
        folder = data.get("folder", "")
        if not os.path.isabs(folder):
            return web.json_response({"error": "Choisissez un dossier de destination."}, status=400)

    now = datetime.now()
    rng = random.Random(data.get("random_seed"))
    random_values = {}
    for match in RAND_RE.finditer(data["format"]):
        if match.group() not in random_values:
            random_values[match.group()] = "".join(rng.choices(RAND_CHARS, k=int(match.group(1))))
    fmt = RAND_RE.sub(lambda m: random_values[m.group()], data["format"])
    names = [format_name(fmt, data["slot"], i + 1, now) for i in range(len(sources))]
    try:
        os.makedirs(folder, exist_ok=True)
        targets += free_files(folder, names[len(targets):])
        for src, dst in zip(sources, targets):
            shutil.copyfile(src, dst)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response({"files": targets})


@PromptServer.instance.routes.get("/saving_tools/previews")
async def last_previews(request):
    # The last finished run of this node in this workflow, so the frontend can show it again after a tab switch.
    workflow, node = request.query["workflow"], request.query["node"]
    queue = PromptServer.instance.prompt_queue
    if (run := request.query.get("run")):
        running, _ = queue.get_current_queue_volatile()
        if any(item[1] == run for item in running):
            # A queue update can arrive after a live preview but before that run reaches history.
            return web.json_response([])
    for entry in reversed(queue.get_history().values()):
        extra_pnginfo = entry["prompt"][3].get("extra_pnginfo") or {}
        if (extra_pnginfo.get("workflow") or {}).get("id") != workflow:
            continue
        output = entry["outputs"].get(node)
        if output and "previews" in output:
            return web.json_response(output["previews"], headers={"X-Saving-Tools-Run": entry["prompt"][1]})
        previews = [p for output in entry["outputs"].values()
                    if output.get("preview_node") == [node] for p in output.get("previews", [])]
        if previews:
            return web.json_response(previews, headers={"X-Saving-Tools-Run": entry["prompt"][1]})
    return web.json_response([])


@PromptServer.instance.routes.post("/saving_tools/pick_folder")
async def pick_folder(request):
    if sys.platform != "win32":
        return web.json_response({"error": "Le choix du dossier n'est disponible que sous Windows."}, status=400)
    data = await request.json()
    start = data["folder"] if os.path.isdir(data["folder"]) else folder_paths.get_output_directory()
    proc = await asyncio.to_thread(subprocess.run, [sys.executable, "-I", FOLDER_DIALOG, data["title"], start],
                                   capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    if proc.returncode:
        error = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        return web.json_response({"error": error[-1] if error else "La fenêtre de choix du dossier a échoué."}, status=400)
    return web.json_response({"folder": proc.stdout.decode("utf-8")})


NODE_CLASS_MAPPINGS = {
    "SelectiveImageSaver": SelectiveImageSaver,
    "SavingToolsPreview": SavingToolsPreview,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "SelectiveImageSaver": "Selective Image Saver",
}

WEB_DIRECTORY = "./web"
