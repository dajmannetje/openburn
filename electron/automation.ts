/**
 * Local automation endpoint (used by openburn-mcp). Opt-in: only starts when
 * OPENBURN_AUTOMATION_PORT is set. Listens on 127.0.0.1 only.
 *
 * Security: every request must carry the secret from <userData>/automation-token
 * (created 0600 on first start) in the X-OpenBurn-Token header, and requests that
 * carry an Origin header (i.e. come from a web page in a browser) are refused — a
 * website must never be able to drive the laser through localhost.
 *
 * Protocol: POST /rpc  {"method": "...", "params": {...}}  →  {"result": …} | {"error": "…"}
 * The request is forwarded to the renderer (src/automation/handler.ts), which owns
 * the document, the canvas and the machine connection.
 */
import http from 'node:http';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { ipcMain, type BrowserWindow } from 'electron';

export function startAutomationServer(getWindow: () => BrowserWindow | null, userDataDir: string): void {
  const port = Number(process.env.OPENBURN_AUTOMATION_PORT || 0);
  if (!port) return;

  const tokenFile = path.join(userDataDir, 'automation-token');
  let token = '';
  try {
    token = fs.readFileSync(tokenFile, 'utf8').trim();
  } catch {
    /* first start */
  }
  if (!token) {
    token = crypto.randomBytes(24).toString('hex');
    fs.mkdirSync(userDataDir, { recursive: true });
    fs.writeFileSync(tokenFile, token, { mode: 0o600 });
  }

  const pending = new Map<number, (payload: unknown) => void>();
  let nextId = 1;
  ipcMain.on('automation:response', (_e, id: number, payload: unknown) => {
    const done = pending.get(id);
    pending.delete(id);
    done?.(payload);
  });

  const server = http.createServer((req, res) => {
    const send = (code: number, body: unknown) => {
      res.writeHead(code, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(body));
    };
    if (req.method !== 'POST' || req.url !== '/rpc') return send(404, { error: 'not found' });
    if (req.headers.origin) return send(403, { error: 'browser requests are not allowed' });
    const given = String(req.headers['x-openburn-token'] ?? '');
    if (given.length !== token.length || !crypto.timingSafeEqual(Buffer.from(given), Buffer.from(token))) {
      return send(403, { error: 'bad token' });
    }
    let body = '';
    req.on('data', (chunk) => {
      body += chunk;
      if (body.length > 25_000_000) req.destroy();
    });
    req.on('end', () => {
      let msg: { method?: string; params?: unknown };
      try {
        msg = JSON.parse(body);
      } catch {
        return send(400, { error: 'invalid JSON' });
      }
      const win = getWindow();
      if (!win || win.isDestroyed()) return send(503, { error: 'OpenBurn window not ready' });
      const id = nextId++;
      const timer = setTimeout(() => {
        pending.delete(id);
        send(504, { error: 'timeout waiting for the OpenBurn window' });
      }, 120_000);
      pending.set(id, (payload) => {
        clearTimeout(timer);
        send(200, payload);
      });
      win.webContents.send('automation:request', id, String(msg.method ?? ''), msg.params ?? {});
    });
  });
  server.on('error', (e) => console.error('[automation] server error:', e));
  server.listen(port, '127.0.0.1', () => console.info(`[automation] listening on 127.0.0.1:${port}`));
}
