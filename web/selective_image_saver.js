import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const SLOT_RE = /^image_\d+$/;
const GAP = 4;
const MIN_PREVIEW = 100;
const DEFAULT_PREVIEW = 300;
const DEFAULT_FORMAT = "ComfyUI_%counter%";
const FORMAT_HELP = [
    "Nom des fichiers enregistrés, pour toutes les images de ce node (sans .png).",
    "%counter% : numéro qui s'incrémente (00001, 00002…)",
    "%date% ou %date:yyyy-MM-dd hh-mm-ss% : date et heure",
    "%rand8% : 8 lettres ou chiffres aléatoires (%rand4%, %rand12%… pour une autre longueur)",
    "%slot% : nom de l'entrée (image_1…)",
    "%batch% : position de l'image dans le batch",
    "Si le nom existe déjà, un numéro est ajouté : seul « Écraser » remplace un fichier.",
].join("\n");

const STYLE = `
.st-wrap { display: flex; flex-direction: column; gap: 6px; padding: 4px; box-sizing: border-box; overflow: hidden; }
.st-gallery { flex: 1 1 0; min-height: 0; display: grid; gap: ${GAP}px; }
.st-cell { position: relative; min-width: 0; min-height: 0; }
.st-cell img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain; cursor: pointer; }
.st-caption { position: absolute; left: 2px; top: 2px; max-width: calc(100% - 12px); padding: 0 4px; border-radius: 3px; font-size: 11px; background: #000a; color: #fff; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; pointer-events: none; }
.st-controls { flex: none; display: flex; flex-direction: column; gap: 6px; }
.st-row { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; }
.st-label { min-width: 56px; font-size: 12px; font-weight: bold; color: var(--fg-color, #ddd); }
.st-path { flex: 1 1 120px; min-width: 0; font-size: 12px; padding: 3px 6px; border-radius: 4px; border: 1px solid var(--border-color, #444); background: var(--comfy-input-bg, #222); color: var(--input-text, #ddd); }
.st-row button { font-size: 12px; padding: 3px 8px; border-radius: 4px; border: 1px solid var(--border-color, #444); background: var(--comfy-menu-bg, #333); color: var(--input-text, #ddd); cursor: pointer; white-space: nowrap; }
.st-row button:disabled { opacity: 0.4; cursor: default; }
.st-row button.st-folder { flex: 1 1 120px; min-width: 0; overflow: hidden; text-overflow: ellipsis; direction: rtl; text-align: left; background: var(--comfy-input-bg, #222); }
.st-folder.st-empty { opacity: 0.6; }
.st-note { flex-basis: 100%; font-size: 11px; opacity: 0.7; color: var(--fg-color, #ddd); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.st-error { color: var(--error-text, #f66); opacity: 1; }
`;

function el(tag, props = {}, children = []) {
    const node = Object.assign(document.createElement(tag), props);
    node.append(...children);
    return node;
}

function imageInputs(node) {
    return (node.inputs ?? []).filter((input) => SLOT_RE.test(input.name));
}

// Every image input except the trailing empty one.
function slots(node) {
    return imageInputs(node).slice(0, -1).map((input) => input.name);
}

// Keeps one empty input at the end and one output per input before it. Only removes slots that have no links.
function tidy(node) {
    let inputs = imageInputs(node);
    while (inputs.length > 1) {
        const last = inputs[inputs.length - 1];
        const prev = inputs[inputs.length - 2];
        if (last.link != null || prev.link != null || node.outputs[inputs.length - 2]?.links?.length) break;
        node.removeInput(node.inputs.indexOf(last));
        inputs = imageInputs(node);
    }
    if (!inputs.length || inputs[inputs.length - 1].link != null) {
        node.addInput(`image_${inputs.length + 1}`, "IMAGE");
        inputs = imageInputs(node);
    }
    const wanted = inputs.length - 1;
    while (node.outputs.length < wanted) {
        node.addOutput(`image_${node.outputs.length + 1}`, "IMAGE");
    }
    while (node.outputs.length > wanted && !node.outputs[node.outputs.length - 1].links?.length) {
        node.removeOutput(node.outputs.length - 1);
    }
}

function scheduleUpdate(node) {
    const state = node.savingTools;
    if (state.queued) return;
    state.queued = true;
    // Deferred so a link being replaced (disconnect then connect) or a graph being loaded is seen in its final state.
    setTimeout(() => {
        state.queued = false;
        if (!node.graph) return;
        tidy(node);
        renderGallery(node);
        renderControls(node);
    }, 0);
}

// Grows the node when its content needs more room. Never shrinks it, so the size chosen by the user is kept.
function fitHeight(node, extra = 0) {
    const height = node.computeSize()[1] + extra;
    if (node.size[1] < height) node.setSize([node.size[0], height]);
    node.setDirtyCanvas(true, true);
}

function previewUrl(preview) {
    const params = new URLSearchParams({ filename: preview.filename, subfolder: preview.subfolder, type: preview.type });
    return api.apiURL(`/view?${params}`);
}

// Picks the column count that shows the images the largest in the available area, like the core image preview.
function layoutGallery(gallery) {
    const imgs = [...gallery.querySelectorAll("img")];
    const width = gallery.clientWidth;
    const height = gallery.clientHeight;
    if (!imgs.length || !width || !height) return;
    let bestArea = -1;
    let bestCols = 1;
    for (let cols = 1; cols <= imgs.length; cols++) {
        const rows = Math.ceil(imgs.length / cols);
        const cellWidth = Math.max(0, (width - GAP * (cols - 1)) / cols);
        const cellHeight = Math.max(0, (height - GAP * (rows - 1)) / rows);
        let area = 0;
        for (const img of imgs) {
            const ratio = img.naturalWidth / img.naturalHeight || 1;
            const w = Math.min(cellWidth, cellHeight * ratio);
            area += (w * w) / ratio;
        }
        if (area > bestArea) {
            bestArea = area;
            bestCols = cols;
        }
    }
    gallery.style.gridTemplateColumns = `repeat(${bestCols}, minmax(0, 1fr))`;
    gallery.style.gridTemplateRows = `repeat(${Math.ceil(imgs.length / bestCols)}, minmax(0, 1fr))`;
}

function renderGallery(node) {
    const { gallery, previews } = node.savingTools;
    const cells = [];
    for (const slot of slots(node)) {
        const list = previews[slot] ?? [];
        list.forEach((p, i) => {
            const src = previewUrl(p);
            const img = el("img", {
                src,
                title: "Ouvrir en taille réelle",
                onclick: () => window.open(src, "_blank"),
                onload: () => layoutGallery(gallery),
            });
            const caption = list.length > 1 ? `${slot} (${i + 1}/${list.length})` : slot;
            cells.push(el("div", { className: "st-cell" }, [img, el("div", { className: "st-caption", textContent: caption })]));
        });
    }
    gallery.replaceChildren(...cells);
    gallery.style.display = cells.length ? "" : "none";
    layoutGallery(gallery);
    node.setDirtyCanvas(true, true);
}

// The workflow change tracker only checks on user input, so properties set after a server reply must be reported.
function markChanged() {
    app.extensionManager.workflow.activeWorkflow?.changeTracker?.checkState();
}

function fileNames(paths) {
    return paths.map((path) => path.split(/[\\/]/).pop()).join(", ");
}

async function post(route, body) {
    const res = await api.fetchApi(route, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);
    return data;
}

// Opens the Windows folder dialog on the ComfyUI machine. Returns the chosen folder, or "" when cancelled.
async function pickFolder(node, slot) {
    const state = node.savingTools;
    const paths = (node.properties.savingPaths ??= {});
    state.busy[slot] = true;
    state.status[slot] = { text: "Choisissez le dossier dans la fenêtre Windows…" };
    renderControls(node);
    let folder = "";
    try {
        folder = (await post("/saving_tools/pick_folder", { title: `Dossier de destination pour ${slot}`, folder: paths[slot] ?? "" })).folder;
        if (folder) {
            paths[slot] = folder;
            markChanged();
        }
        delete state.status[slot];
    } catch (error) {
        state.status[slot] = { text: `Erreur : ${error.message}`, error: true };
    }
    state.busy[slot] = false;
    renderControls(node);
    return folder;
}

async function save(node, slot, overwrite) {
    const state = node.savingTools;
    const lastSaved = (node.properties.lastSaved ??= {});
    const body = { files: state.previews[slot].map((p) => p.filename), format: node.properties.filenameFormat, slot };
    if (overwrite) {
        body.overwrite = lastSaved[slot];
    } else {
        body.folder = node.properties.savingPaths?.[slot] || (await pickFolder(node, slot));
        if (!body.folder) return;
    }

    state.busy[slot] = true;
    state.status[slot] = { text: "Enregistrement…" };
    renderControls(node);
    try {
        lastSaved[slot] = (await post("/saving_tools/save", body)).files;
        markChanged();
        state.status[slot] = { text: `Enregistré : ${fileNames(lastSaved[slot])}`, title: lastSaved[slot].join("\n") };
    } catch (error) {
        state.status[slot] = { text: `Erreur : ${error.message}`, error: true };
    }
    state.busy[slot] = false;
    renderControls(node);
}

function controlRow(node, slot) {
    const state = node.savingTools;
    const hasPreview = !!state.previews[slot]?.length;
    const folder = node.properties.savingPaths?.[slot];
    const last = node.properties.lastSaved?.[slot];
    const connected = imageInputs(node).find((input) => input.name === slot)?.link != null;

    let note = state.status[slot];
    if (!note && !hasPreview) note = { text: connected ? "Pas encore d'aperçu : lancez le workflow." : "Entrée déconnectée." };
    if (!note && last?.length) note = { text: `Dernière : ${fileNames(last)}`, title: last.join("\n") };

    const children = [
        el("span", { className: "st-label", textContent: slot }),
        el("button", {
            className: `st-folder${folder ? "" : " st-empty"}`,
            title: folder ? `${folder}\nCliquer pour changer de dossier` : "Choisir le dossier de destination",
            disabled: state.busy[slot],
            onclick: () => pickFolder(node, slot),
        }, [el("bdi", { textContent: folder || "Choisir un dossier…" })]),
        el("button", {
            textContent: "Enregistrer",
            disabled: !hasPreview || state.busy[slot],
            onclick: () => save(node, slot, false),
        }),
        el("button", {
            textContent: "Écraser",
            title: last?.length ? `Écraser la dernière image enregistrée : ${last.join(", ")}` : "Aucune image enregistrée pour cette entrée",
            disabled: !hasPreview || !last?.length || state.busy[slot],
            onclick: () => save(node, slot, true),
        }),
    ];
    if (note) children.push(el("div", { className: `st-note${note.error ? " st-error" : ""}`, textContent: note.text, title: note.title ?? note.text }));
    return el("div", { className: "st-row" }, children);
}

function renderControls(node) {
    const { controls } = node.savingTools;
    const names = slots(node);
    const format = el("div", { className: "st-row", title: FORMAT_HELP }, [
        el("span", { className: "st-label", textContent: "Nom" }),
        el("input", {
            className: "st-path",
            type: "text",
            value: node.properties.filenameFormat,
            placeholder: DEFAULT_FORMAT,
            oninput: (e) => { node.properties.filenameFormat = e.target.value; },
        }),
    ]);
    controls.replaceChildren(format, ...(names.length
        ? names.map((slot) => controlRow(node, slot))
        : [el("div", { className: "st-note", textContent: "Connectez une image pour commencer." })]));
    node.setDirtyCanvas(true, true);
}

app.registerExtension({
    name: "saving-tools.SelectiveImageSaver",
    setup() {
        document.head.append(el("style", { textContent: STYLE }));
    },
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "SelectiveImageSaver") return;

        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = onNodeCreated?.apply(this, arguments);
            const gallery = el("div", { className: "st-gallery", style: "display: none" });
            const controls = el("div", { className: "st-controls" });
            const state = { gallery, controls, controlsHeight: 30, previews: {}, status: {}, busy: {}, queued: false };
            this.savingTools = state;
            this.properties.filenameFormat ??= DEFAULT_FORMAT;
            this.addDOMWidget("saving_tools", "saving_tools", el("div", { className: "st-wrap" }, [gallery, controls]), {
                serialize: false,
                getMinHeight: () => state.controlsHeight + (gallery.childElementCount ? MIN_PREVIEW + 6 : 0) + 8,
            });
            new ResizeObserver(() => layoutGallery(gallery)).observe(gallery);
            // A hidden widget (off screen, collapsed) measures 0 and keeps its last height.
            new ResizeObserver(() => {
                if (!controls.offsetHeight || controls.offsetHeight === state.controlsHeight) return;
                state.controlsHeight = controls.offsetHeight;
                fitHeight(this);
            }).observe(controls);
            this.setSize([Math.max(this.size[0], 400), this.size[1]]);
            scheduleUpdate(this);
            return r;
        };

        const onConnectionsChange = nodeType.prototype.onConnectionsChange;
        nodeType.prototype.onConnectionsChange = function () {
            const r = onConnectionsChange?.apply(this, arguments);
            scheduleUpdate(this);
            return r;
        };

        const onConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function () {
            const r = onConfigure?.apply(this, arguments);
            scheduleUpdate(this);
            return r;
        };

        const onExecuted = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function (output) {
            const r = onExecuted?.apply(this, arguments);
            const state = this.savingTools;
            const hadPreviews = state.gallery.childElementCount > 0;
            state.previews = {};
            for (const p of output?.previews ?? []) (state.previews[p.slot] ??= []).push(p);
            state.status = {};
            renderGallery(this);
            renderControls(this);
            // First previews: give them a comfortable default size, like the core image preview.
            fitHeight(this, hadPreviews ? 0 : DEFAULT_PREVIEW - MIN_PREVIEW);
            return r;
        };
    },
});
