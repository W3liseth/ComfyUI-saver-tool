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

        temp_dir = folder_paths.get_temp_directory()
        os.makedirs(temp_dir, exist_ok=True)
        previews = []
        for slot in sorted(images):
            if images[slot] is None:
                continue
            for image in images[slot]:
                i = 255. * image.cpu().numpy()
                img = Image.fromarray(np.clip(i, 0, 255).astype(np.uint8))
                filename = f"saving_tools_{uuid.uuid4().hex}.png"
                img.save(os.path.join(temp_dir, filename), pnginfo=metadata, compress_level=4)
                previews.append({"slot": f"image_{slot}", "filename": filename, "subfolder": "", "type": "temp"})

        result = tuple(images.get(i) for i in range(1, max(images, default=0) + 1))
        return {"ui": {"previews": previews}, "result": result}


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
    names = [format_name(data["format"], data["slot"], i + 1, now) for i in range(len(sources))]
    try:
        os.makedirs(folder, exist_ok=True)
        targets += free_files(folder, names[len(targets):])
        for src, dst in zip(sources, targets):
            shutil.copyfile(src, dst)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response({"files": targets})


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
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "SelectiveImageSaver": "Selective Image Saver",
}

WEB_DIRECTORY = "./web"
