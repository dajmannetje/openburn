/**
 * Renderer side of the local automation endpoint (electron/automation.ts → openburn-mcp).
 * Every method uses the same store / workspace / machine calls as the UI does.
 *
 * Laser safety: `start_job` refuses without `confirm: true`; the Gweike G2 bridge
 * additionally refuses any job that was not framed (red light) first.
 */
import { ActiveSelection, type FabricObject, type Canvas } from 'fabric';
import { getWorkspaceApi, store, type AlignMode, type DroppedFile } from '@/state/store';
import { machine } from '@/serial/machine';
import type { CutSettings } from '@/types/document';

type Params = Record<string, unknown>;

function ws() {
  const api = getWorkspaceApi();
  if (!api) throw new Error('workspace not ready');
  return api;
}

function canvas(): Canvas {
  const c = (ws() as unknown as { canvas?: Canvas }).canvas;
  if (!c) throw new Error('canvas not ready');
  return c;
}

/** User shapes only (helpers such as the grid or guides carry excludeFromExport). */
function shapes(): FabricObject[] {
  return canvas()
    .getObjects()
    .filter((o) => !(o as FabricObject & { excludeFromExport?: boolean }).excludeFromExport);
}

const round = (v: number) => Math.round(v * 1000) / 1000;

function describe(o: FabricObject, index: number) {
  const b = o.getBoundingRect();
  const layerId = (o as FabricObject & { layerId?: string }).layerId ?? null;
  const layer = store.getState().doc.layers.find((l) => l.id === layerId);
  const text = (o as FabricObject & { text?: string }).text;
  return {
    index,
    type: o.type,
    layer: layer ? `${layer.name} (${layer.id})` : layerId,
    x: round(b.left),
    y: round(b.top),
    width: round(b.width),
    height: round(b.height),
    ...(text ? { text } : {}),
  };
}

function pick(indices: unknown): FabricObject[] {
  const all = shapes();
  if (!Array.isArray(indices) || indices.length === 0) throw new Error('indices: non-empty array required');
  return indices.map((i) => {
    const o = all[Number(i)];
    if (!o) throw new Error(`no object with index ${i} (have ${all.length})`);
    return o;
  });
}

function select(objs: FabricObject[]) {
  const c = canvas();
  c.discardActiveObject();
  if (objs.length === 1) c.setActiveObject(objs[0]);
  else if (objs.length > 1) c.setActiveObject(new ActiveSelection(objs, { canvas: c }));
  c.requestRenderAll();
}

function findLayer(ref: unknown) {
  const layers = store.getState().doc.layers;
  const r = String(ref ?? '');
  const layer =
    layers.find((l) => l.id === r) ??
    layers.find((l) => l.name.toLowerCase() === r.toLowerCase()) ??
    (r !== '' && Number.isInteger(Number(r)) ? layers[Number(r)] : undefined);
  if (!layer) throw new Error(`unknown layer ${r}; layers: ${layers.map((l) => l.name).join(', ')}`);
  return layer;
}

function state() {
  const s = store.getState();
  const m = machine.getState();
  const active = canvas().getActiveObjects();
  return {
    title: document.title,
    units: s.doc.units,
    workArea: { width: s.doc.bed.width, height: s.doc.bed.height },
    device: { name: s.device.name, origin: s.device.origin, maxS: s.device.maxS, laserMode: s.device.laserMode },
    activeLayer: s.activeLayerId,
    layers: s.doc.layers.map((l) => ({ id: l.id, name: l.name, color: l.color, settings: l.settings })),
    objects: shapes().map(describe),
    selected: active.length,
    machine: {
      connected: m.connected,
      transport: m.transportName,
      status: m.status,
      jobActive: m.jobActive,
      jobKind: m.jobKind,
      progress: round(m.progress),
      lastError: m.lastError,
      log: m.logs.slice(-12).map((l) => `${l.dir}: ${l.text}`),
    },
  };
}

async function frame(selectionOnly: boolean) {
  const api = ws();
  const m = machine.getState();
  if (!m.connected) throw new Error('not connected (use connect_g2 first)');
  if (m.transportName.startsWith('G2 bridge')) {
    const job = await api.generateJob(selectionOnly);
    const raster = job.segments.some((sg) => sg.powers && sg.powers.length > 0);
    if (job.segments.length > 0 && !raster) {
      const gcode = job.gcode.map((l) => l.replace(/^(M[34]) S[\d.]+/, '$1 S0').replace(/ S[\d.]+$/, ''));
      machine.startPlan({ ...job, gcode }, 'frame');
      return 'framing the shapes with the red light';
    }
  }
  const plan = api.generateFrameJob();
  if (!plan) throw new Error('nothing to frame');
  machine.startPlan(plan, 'frame');
  return 'framing the bounding box with the red light';
}

const methods: Record<string, (p: Params) => unknown> = {
  get_state: () => state(),

  set_work_area: (p) => {
    const w = Number(p.width), h = Number(p.height ?? p.width);
    if (!(w > 0 && h > 0)) throw new Error('width/height must be > 0');
    store.setBed(w, h);
    return state().workArea;
  },

  set_device: (p) => {
    const patch: Record<string, unknown> = {};
    if (p.origin) patch.origin = p.origin;
    if (p.maxS) patch.maxS = Number(p.maxS);
    if (p.name) patch.name = String(p.name);
    store.updateDevice(patch);
    return state().device;
  },

  new_document: () => {
    ws().newDocument();
    return state();
  },

  import: async (p) => {
    const file: DroppedFile = { name: String(p.name ?? 'import.svg') };
    if (typeof p.text === 'string') file.text = p.text;
    else if (typeof p.dataUrl === 'string') file.dataUrl = p.dataUrl;
    else throw new Error('text (SVG/DXF) or dataUrl (image) required');
    const before = shapes().length;
    await ws().dropFiles([file]);
    const all = shapes();
    return { added: all.slice(before).map((o, i) => describe(o, before + i)), total: all.length };
  },

  select: (p) => {
    select(pick(p.indices));
    return { selected: canvas().getActiveObjects().length };
  },

  select_all: () => {
    ws().selectAll();
    return { selected: canvas().getActiveObjects().length };
  },

  clear_selection: () => {
    canvas().discardActiveObject();
    canvas().requestRenderAll();
    return { selected: 0 };
  },

  delete: (p) => {
    select(pick(p.indices));
    ws().deleteSelection();
    return { objects: shapes().map(describe) };
  },

  /** Position/size of the current selection in mm (x/y = left/top on the canvas). */
  transform: (p) => {
    if (canvas().getActiveObjects().length === 0) throw new Error('nothing selected');
    const patch: Record<string, number> = {};
    for (const k of ['x', 'y', 'width', 'height', 'angle']) if (p[k] !== undefined) patch[k] = Number(p[k]);
    ws().applyNumeric(patch);
    return canvas().getActiveObjects().map((o) => describe(o, shapes().indexOf(o)));
  },

  align: (p) => {
    if (canvas().getActiveObjects().length === 0) throw new Error('nothing selected');
    ws().align(String(p.mode) as AlignMode);
    return canvas().getActiveObjects().map((o) => describe(o, shapes().indexOf(o)));
  },

  set_layer: (p) => {
    const layer = findLayer(p.layer ?? store.getState().activeLayerId);
    const allowed: (keyof CutSettings)[] = [
      'mode', 'speed', 'power', 'minPower', 'passes', 'interval', 'angle', 'overscan', 'offsetDistance',
    ];
    const patch: Partial<CutSettings> = {};
    const src = (p.settings ?? {}) as Record<string, unknown>;
    for (const k of allowed) if (src[k] !== undefined) (patch as Record<string, unknown>)[k] = k === 'mode' ? src[k] : Number(src[k]);
    store.updateCutSettings(layer.id, patch);
    return store.getState().doc.layers.find((l) => l.id === layer.id);
  },

  assign_layer: (p) => {
    const layer = findLayer(p.layer);
    if (canvas().getActiveObjects().length === 0) throw new Error('nothing selected');
    ws().setLayerOfSelection(layer.id);
    return { layer: layer.name };
  },

  undo: () => (ws().undo(), state().objects),
  redo: () => (ws().redo(), state().objects),

  export_project: () => ws().snapshotDocument(),
  open_project: async (p) => ({ ok: await ws().applyRecoveredDocument(p.document, null) }),

  gcode: async (p) => {
    const plan = await ws().generateJob(Boolean(p.selectionOnly));
    return { lines: plan.gcode.length, seconds: plan.timeSeconds, head: plan.gcode.slice(0, 60), warnings: plan.warnings };
  },

  preview_png: (p) => {
    const c = canvas();
    return { dataUrl: c.toDataURL({ format: 'png', multiplier: Number(p.scale ?? 1) }) };
  },

  connect_g2: async (p) => {
    await machine.connectBridge(typeof p.url === 'string' ? p.url : undefined);
    return state().machine;
  },

  disconnect: async () => {
    await machine.disconnect();
    return state().machine;
  },

  frame: async (p) => frame(Boolean(p.selectionOnly)),

  start_job: async (p) => {
    if (p.confirm !== true) throw new Error('start_job needs confirm: true — only after the user explicitly said go');
    const m = machine.getState();
    if (!m.connected) throw new Error('not connected');
    if (m.jobActive) throw new Error('a job is already running');
    const plan = await ws().generateJob(Boolean(p.selectionOnly));
    if (plan.segments.length === 0) throw new Error('nothing to mark (check layer output)');
    machine.startPlan(plan, 'job');
    return { started: true, lines: plan.gcode.length, estimatedSeconds: plan.timeSeconds };
  },

  stop: () => {
    machine.stopJob();
    return state().machine;
  },
};

export function installAutomation(): void {
  const bridge = window.openburn?.automation;
  if (!bridge) return;
  bridge.onRequest(async (id, method, params) => {
    const fn = methods[method];
    try {
      if (!fn) throw new Error(`unknown method ${method}; available: ${Object.keys(methods).join(', ')}`);
      const result = await fn((params ?? {}) as Params);
      bridge.respond(id, { result: result ?? null });
    } catch (e) {
      bridge.respond(id, { error: (e as Error).message ?? String(e) });
    }
  });
}
