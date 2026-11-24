/**
 * Machine profile applied at startup (OPENBURN_PROFILE env, set by the `openburn` launcher).
 *
 * g2 — Gweike G2 fibre galvo through g2bridge:
 *   device "Gweike G2", origin bottom-left, max S 1000 (persisted by OpenBurn itself),
 *   work area 110 x 110 mm when the open document is still empty (projects keep theirs;
 *   new documents inherit the current work area), and connect to the G2 bridge.
 */
import { getWorkspaceApi, store } from '@/state/store';
import { machine } from '@/serial/machine';

export function activeProfile(): string {
  return window.openburn?.profile ?? '';
}

export function applyProfile(): void {
  if (activeProfile() !== 'g2') return;
  const start = Date.now();
  const tick = window.setInterval(() => {
    const ws = getWorkspaceApi() as unknown as { canvas?: { getObjects(): { excludeFromExport?: boolean }[] } } | null;
    if (!ws?.canvas) {
      if (Date.now() - start > 15000) window.clearInterval(tick);
      return;
    }
    window.clearInterval(tick);
    store.updateDevice({ name: 'Gweike G2', origin: 'bottom-left', maxS: 1000, laserMode: 'M4' });
    const hasShapes = ws.canvas.getObjects().some((o) => !o.excludeFromExport);
    const bed = store.getState().doc.bed;
    if (!hasShapes && (bed.width !== 110 || bed.height !== 110)) store.setBed(110, 110);
    void connectWithRetry();
  }, 200);
}

async function connectWithRetry(): Promise<void> {
  for (let attempt = 0; attempt < 10; attempt += 1) {
    if (machine.getState().connected) return;
    try {
      await machine.connectBridge();
      return;
    } catch {
      await new Promise((r) => window.setTimeout(r, 1500)); // bridge may still be starting
    }
  }
}
