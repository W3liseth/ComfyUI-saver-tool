import asyncio
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1]))

from aiohttp import web
import torch

from comfy.cli_args import args

args.cpu = True

import folder_paths
import nodes
from execution import CacheType, PromptExecutor
from server import PromptServer


class GeneratedImage:
    calls = []

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"value": ("FLOAT",)}, "optional": {"previous": ("IMAGE",)}}

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "generate"

    def generate(self, value, previous=None):
        self.calls.append(value)
        PromptServer.instance.events.append(("generated", value))
        return (torch.full((2, 8, 8, 3), value),)


class CaptureImages:
    values = None

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"first": ("IMAGE",), "second": ("IMAGE",), "fourth": ("IMAGE",)}}

    RETURN_TYPES = ()
    FUNCTION = "capture"
    OUTPUT_NODE = True
    INPUT_IS_LIST = True

    def capture(self, **inputs):
        type(self).values = inputs
        return ()


class GeneratedImageList(GeneratedImage):
    OUTPUT_IS_LIST = (True,)

    def generate(self, value, previous=None):
        image = super().generate(value, previous)[0]
        return ([image, image * 0.5],)


class ProgressivePreviewsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.server = SimpleNamespace(client_id=None, routes=web.RouteTableDef(), events=[])
        self.server.send_sync = lambda event, data, *args: self.server.events.append((event, data))
        self.start_patch(patch.object(PromptServer, "instance", self.server, create=True))
        self.start_patch(patch.object(folder_paths, "get_temp_directory", return_value=self.temp.name))
        spec = importlib.util.spec_from_file_location("saving_tools_test", ROOT / "__init__.py")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.start_patch(patch.dict(nodes.NODE_CLASS_MAPPINGS, {
            **self.module.NODE_CLASS_MAPPINGS,
            "TestGeneratedImage": GeneratedImage,
            "TestGeneratedImageList": GeneratedImageList,
            "TestCaptureImages": CaptureImages,
        }))
        GeneratedImage.calls = []
        CaptureImages.values = None
        self.executor = PromptExecutor(self.server, cache_type=CacheType.CLASSIC,
                                       cache_args={"ram": 0, "ram_inactive": 0})
        self.prompt = {
            "first": {"class_type": "TestGeneratedImage", "inputs": {"value": 0.25}},
            "second": {"class_type": "TestGeneratedImage", "inputs": {"value": 0.75, "previous": ["first", 0]}},
            "saver": {"class_type": "SelectiveImageSaver", "inputs": {
                "image_1": ["first", 0], "image_2": ["second", 0], "image_4": ["first", 0],
            }},
            "capture": {"class_type": "TestCaptureImages", "inputs": {
                "first": ["saver", 0], "second": ["saver", 1], "fourth": ["saver", 3],
            }},
        }

    def start_patch(self, mock_patch):
        mock_patch.start()
        self.addCleanup(mock_patch.stop)

    def run_prompt(self, prompt=None):
        transformed = subprocess.run(["node", str(ROOT / "tests" / "test_progressive_previews.mjs"), "--transform"],
                                     input=json.dumps(prompt or self.prompt), capture_output=True, text=True, check=True)
        self.executor.execute(json.loads(transformed.stdout), "test", extra_data={
            "client_id": "test", "extra_pnginfo": {"workflow": {"id": "workflow"}},
        }, execute_outputs=["saver", "capture"])
        self.assertTrue(self.executor.success, self.executor.status_messages)

    def preview_events(self):
        return [(index, data["output"]) for index, (event, data) in enumerate(self.server.events)
                if event == "executed" and data["node"] == "saver" and data["output"].get("preview_slot")]

    def test_first_preview_precedes_next_generation_and_outputs_keep_batches(self):
        self.run_prompt()
        events = self.preview_events()
        first_preview = next(index for index, output in events if output["preview_slot"] == ["image_1"])
        second_generation = self.server.events.index(("generated", 0.75))
        self.assertLess(first_preview, second_generation)
        self.assertEqual({output["preview_slot"][0] for _, output in events}, {"image_1", "image_2", "image_4"})
        self.assertEqual(len(list(Path(self.temp.name).glob("*.png"))), 6)
        for _, output in events:
            self.assertEqual(len(output["previews"]), 2)
            for preview in output["previews"]:
                with self.module.Image.open(Path(self.temp.name) / preview["filename"]) as image:
                    self.assertEqual(image.size, (8, 8))
                    self.assertEqual(json.loads(image.info["workflow"]), {"id": "workflow"})
        values = CaptureImages.values
        self.assertEqual(len(values["first"]), 1)
        self.assertEqual(tuple(values["first"][0].shape), (2, 8, 8, 3))
        self.assertIs(values["first"][0], values["fourth"][0])
        self.assertTrue(torch.all(values["second"][0] == 0.75))

    def test_changed_branch_reuses_other_preview_and_history_restores_all_slots(self):
        self.run_prompt()
        original = {output["preview_slot"][0]: output["previews"] for _, output in self.preview_events()}
        self.server.events.clear()
        changed = copy.deepcopy(self.prompt)
        changed["second"]["inputs"]["value"] = 0.5
        self.run_prompt(changed)
        current = {}
        for preview in self.executor.history_result["outputs"]["saver"]["previews"]:
            current.setdefault(preview["slot"], []).append(preview)
        self.assertEqual(GeneratedImage.calls, [0.25, 0.75, 0.5])
        self.assertEqual([p["filename"] for p in current["image_1"]], [p["filename"] for p in original["image_1"]])
        self.assertNotEqual([p["filename"] for p in current["image_2"]], [p["filename"] for p in original["image_2"]])
        entry = {"prompt": [None, None, None, {"extra_pnginfo": {"workflow": {"id": "workflow"}}}],
                 "outputs": self.executor.history_result["outputs"]}
        self.server.prompt_queue = SimpleNamespace(get_history=lambda: {"test": entry})
        response = asyncio.run(self.module.last_previews(SimpleNamespace(query={"workflow": "workflow", "node": "saver"})))
        self.assertEqual({p["slot"] for p in json.loads(response.text)}, {"image_1", "image_2", "image_4"})

    def test_image_lists_keep_their_output_positions(self):
        self.prompt["first"]["class_type"] = "TestGeneratedImageList"
        self.prompt["second"]["inputs"]["previous"] = ["saver", 0]
        self.run_prompt()
        values = CaptureImages.values
        self.assertEqual(len(values["first"]), 2)
        self.assertEqual(len(values["second"]), 2)
        self.assertEqual(len(values["fourth"]), 2)
        for index in range(2):
            self.assertIs(values["first"][index], values["fourth"][index])
        first_output = next(output for _, output in self.preview_events() if output["preview_slot"] == ["image_1"])
        self.assertEqual(len(first_output["previews"]), 4)

    def test_output_can_generate_a_later_input_of_the_same_saver(self):
        self.prompt["second"]["inputs"]["previous"] = ["saver", 0]
        self.run_prompt()
        first_preview = next(index for index, output in self.preview_events() if output["preview_slot"] == ["image_1"])
        self.assertLess(first_preview, self.server.events.index(("generated", 0.75)))
        self.assertTrue(torch.all(CaptureImages.values["second"][0] == 0.75))


if __name__ == "__main__":
    unittest.main()
