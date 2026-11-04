import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../web/selective_image_saver.js", import.meta.url), "utf8")
    .replace(/^import .*;\r?\n/gm, "");
const context = vm.createContext({ app: { registerExtension() {} } });
vm.runInContext(`${source}
    renderGallery = () => {};
    renderControls = () => {};
    globalThis.sync = syncPreviews;
    globalThis.addPreviewNodes = addPreviewNodes;
`, context);

if (process.argv.includes("--transform")) {
    const prompt = JSON.parse(readFileSync(0, "utf8"));
    context.addPreviewNodes(prompt);
    process.stdout.write(JSON.stringify(prompt));
    process.exit(0);
}

const prompt = {
    source: { class_type: "Source", inputs: {} },
    saver: { class_type: "SelectiveImageSaver", inputs: { image_1: ["source", 0], image_4: ["source", 0] } },
    next: { class_type: "Generate", inputs: { image: ["saver", 0] } },
    fourth: { class_type: "Consumer", inputs: { image: ["saver", 3] } },
};
context.addPreviewNodes(prompt);
assert.equal(prompt["saver:saving_tools:image_1"].inputs.image[0], "source");
assert.equal(prompt.saver.inputs.image_1[0], "saver:saving_tools:image_1");
assert.equal(prompt.saver.inputs.preview_image_1[1], 1);
assert.equal(prompt.saver.inputs.image_4[0], "saver:saving_tools:image_4");
assert.equal(prompt.next.inputs.image[0], "saver:saving_tools:image_1");
assert.equal(prompt.fourth.inputs.image[0], "saver:saving_tools:image_4");
const once = JSON.stringify(prompt);
context.addPreviewNodes(prompt);
assert.equal(JSON.stringify(prompt), once);

const chain = {
    first: { class_type: "Source", inputs: {} },
    saver: { class_type: "SelectiveImageSaver", inputs: { image_1: ["first", 0], image_2: ["second", 0] } },
    second: { class_type: "Generate", inputs: { image: ["saver", 0] } },
    saver2: { class_type: "SelectiveImageSaver", inputs: { image_1: ["saver", 1] } },
    consumer: { class_type: "Consumer", inputs: { image: ["saver2", 0] } },
};
context.addPreviewNodes(chain);
assert.equal(chain.second.inputs.image[0], "saver:saving_tools:image_1");
assert.equal(chain["saver2:saving_tools:image_1"].inputs.image[0], "saver:saving_tools:image_2");
assert.equal(chain.consumer.inputs.image[0], "saver2:saving_tools:image_1");
const chainedOnce = JSON.stringify(chain);
context.addPreviewNodes(chain);
assert.equal(JSON.stringify(chain), chainedOnce);

const node = { savingTools: { previews: {}, previewKey: "", status: {} } };
const preview = (slot, filename) => ({ slot, filename });
const first = preview("image_1", "first.png");
const second = preview("image_2", "second.png");

context.sync(node, { preview_slot: ["image_2"], previews: [second] });
assert.equal(node.savingTools.previews.image_2[0], second);
node.savingTools.status.image_2 = { text: "Enregistré" };

context.sync(node, { preview_slot: ["image_1"], previews: [first] });
assert.equal(node.savingTools.previews.image_1[0], first);
assert.equal(node.savingTools.previews.image_2[0], second);
assert.equal(node.savingTools.status.image_2.text, "Enregistré");

// A cached execution or a history reply keeps the save status for unchanged files.
context.sync(node, { preview_slot: ["image_2"], previews: [second] });
context.sync(node, { previews: [second, first] });
assert.equal(node.savingTools.status.image_2.text, "Enregistré");

const next = preview("image_2", "next.png");
context.sync(node, { preview_slot: ["image_2"], previews: [next] });
assert.equal(node.savingTools.previews.image_1[0], first);
assert.equal(node.savingTools.previews.image_2[0], next);
assert.equal(node.savingTools.status.image_2, undefined);

// Full history snapshots still replace the gallery when restoring a workflow.
context.sync(node, { previews: [first] });
assert.equal(node.savingTools.previews.image_1[0], first);
assert.equal(node.savingTools.previews.image_2, undefined);
console.log("Progressive gallery checks passed.");
