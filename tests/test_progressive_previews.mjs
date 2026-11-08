import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../web/selective_image_saver.js", import.meta.url), "utf8")
    .replace(/^import .*;\r?\n/gm, "");
const context = vm.createContext({ app: { registerExtension() {} } });
vm.runInContext(`${source}
    renderGallery = () => {};
    renderControls = () => {};
    markChanged = () => {};
    globalThis.sync = syncPreviews;
    globalThis.load = loadPreviews;
    globalThis.addPreviewNodes = addPreviewNodes;
    globalThis.savePreview = save;
    globalThis.overwriteFiles = overwriteFiles;
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
// Cached files may stay unchanged while a new workflow run changes the naming seed.
context.sync(node, { previews: [first], preview_run: ["run1"] });
assert.equal(node.savingTools.namingRun, "run1");
context.sync(node, { preview_slot: ["image_2"], previews: [second], preview_run: ["run1"] });
assert.equal(node.savingTools.namingRun, "run1");
context.sync(node, { preview_slot: ["image_2"], previews: [second], preview_run: ["run2"] });
assert.equal(node.savingTools.namingRun, "run2");

// Queue status can refresh history while the gallery is already showing a live run.
context.URLSearchParams = URLSearchParams;
context.api = { fetchApi: async (url) => {
    const params = new URL(url, "http://localhost").searchParams;
    assert.equal(params.get("run"), "run2");
    return { json: async () => [] };
} };
node.graph = { id: "workflow" };
node.id = "saver";
node.savingTools.version = 1;
await context.load(node);
assert.equal(node.savingTools.previews.image_2[0], second);
assert.equal(node.savingTools.namingRun, "run2");

// Overwrite follows the last successful save across slots and generation changes.
const requests = [];
context.post = async (route, body) => {
    requests.push(body);
    return { files: body.overwrite ?? [`/saved/${requests.length}.png`] };
};
const saveNode = {
    id: 1, graph: { id: "workflow" },
    properties: { filenameFormat: "%rand6%", savingPaths: { image_1: "/saved", image_2: "/other" } },
    savingTools: { previews: { image_1: [first], image_2: [second] }, namingRun: "run1", status: {}, busy: {} },
};
await context.savePreview(saveNode, "image_1", false);
assert.equal(saveNode.properties.lastSavedFiles[0], "/saved/1.png");
saveNode.savingTools.namingRun = "run2";
// Workflow serialization and restoration keeps the overwrite target.
saveNode.properties = JSON.parse(JSON.stringify(saveNode.properties));
await context.savePreview(saveNode, "image_2", true);
assert.equal(requests[1].overwrite[0], "/saved/1.png");
assert.notEqual(requests[1].random_seed, requests[0].random_seed);
assert.equal(requests[1].folder, undefined);
await context.savePreview(saveNode, "image_2", false);
await context.savePreview(saveNode, "image_1", true);
assert.equal(requests[3].overwrite[0], "/saved/3.png");
context.post = async () => { throw new Error("Save failed"); };
await context.savePreview(saveNode, "image_1", false);
assert.equal(saveNode.properties.lastSavedFiles[0], "/saved/3.png");
assert.equal(context.overwriteFiles({ properties: { lastSaved: { image_1: ["/saved/legacy.png"] } } })[0], "/saved/legacy.png");
console.log("Progressive gallery checks passed.");
