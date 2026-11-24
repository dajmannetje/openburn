#!/usr/bin/env python3
"""openburn-mcp — MCP server (stdio) that drives a running OpenBurn through its local
automation endpoint (electron/automation.ts, enabled with OPENBURN_AUTOMATION_PORT).

No dependencies (standard library only). Register with Claude Code:
    claude mcp add openburn -s user -- python3 /home/amjad/src/openburn/mcp/openburn_mcp.py

Environment: OPENBURN_AUTOMATION_PORT (default 8768), OPENBURN_TOKEN_FILE
(default ~/.config/openburn/automation-token), OPENBURN_LAUNCH (default ~/.local/bin/openburn).

Laser safety: openburn_start_job fires the laser. It needs user_confirmed=true and must only be
used after the user explicitly said "go" for this job after seeing the red-light frame.
The Gweike G2 bridge additionally refuses jobs that were not framed first.
"""
import base64, json, mimetypes, os, subprocess, sys, time, urllib.error, urllib.request

PORT = int(os.environ.get("OPENBURN_AUTOMATION_PORT", "8768"))
TOKEN_FILE = os.environ.get("OPENBURN_TOKEN_FILE", os.path.expanduser("~/.config/openburn/automation-token"))
LAUNCH = os.environ.get("OPENBURN_LAUNCH", os.path.expanduser("~/.local/bin/openburn"))
PROTOCOL = "2025-06-18"


def rpc(method, params=None, timeout=130):
    try:
        token = open(TOKEN_FILE).read().strip()
    except OSError:
        raise RuntimeError("OpenBurn automation is not running (no token file) — use openburn_launch")
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/rpc", data=json.dumps({"method": method, "params": params or {}}).encode(),
                                 headers={"Content-Type": "application/json", "X-OpenBurn-Token": token}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read())
    except urllib.error.HTTPError as e:
        out = json.loads(e.read() or b"{}") or {"error": str(e)}
    except (urllib.error.URLError, ConnectionError) as e:
        raise RuntimeError(f"OpenBurn is not reachable on 127.0.0.1:{PORT} ({e}) — use openburn_launch")
    if "error" in out:
        raise RuntimeError(out["error"])
    return out.get("result")


def launch(_):
    try:
        rpc("get_state", timeout=3)
        return "OpenBurn is already running with automation enabled."
    except RuntimeError:
        pass
    env = dict(os.environ, OPENBURN_AUTOMATION_PORT=str(PORT))
    subprocess.Popen(["setsid", "-f", LAUNCH], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        time.sleep(0.5)
        try:
            rpc("get_state", timeout=3)
            return "OpenBurn started (automation enabled)."
        except RuntimeError:
            continue
    raise RuntimeError("OpenBurn did not come up within 30 s")


def import_file(a):
    path = os.path.expanduser(a["path"])
    name = os.path.basename(path)
    ext = name.lower().rsplit(".", 1)[-1]
    if ext in ("svg", "dxf"):
        return rpc("import", {"name": name, "text": open(path, encoding="utf-8", errors="replace").read()})
    mime = mimetypes.guess_type(name)[0] or "image/png"
    data = base64.b64encode(open(path, "rb").read()).decode()
    return rpc("import", {"name": name, "dataUrl": f"data:{mime};base64,{data}"})


def save_project(a):
    doc = rpc("export_project")
    path = os.path.expanduser(a["path"])
    if not path.endswith(".obrn"):
        path += ".obrn"
    with open(path, "w") as f:
        json.dump(doc, f)
    return f"saved {path}"


def open_project(a):
    return rpc("open_project", {"document": json.load(open(os.path.expanduser(a["path"])))})


def set_layer(a):
    settings = {k: a[k] for k in ("mode", "speed", "power", "minPower", "passes", "interval", "angle", "overscan") if k in a}
    return rpc("set_layer", {"layer": a.get("layer"), "settings": settings})


def start_job(a):
    if a.get("user_confirmed") is not True:
        raise RuntimeError("refused: set user_confirmed=true only after the user explicitly said go for this job")
    return rpc("start_job", {"selectionOnly": bool(a.get("selection_only")), "confirm": True})


def preview(a):
    url = rpc("preview_png", {"scale": a.get("scale", 1)})["dataUrl"]
    return {"__image__": url.split(",", 1)[1]}


IDX = {"type": "array", "items": {"type": "integer"}, "description": "object indices from openburn_state"}
SEL = {"selection_only": {"type": "boolean", "description": "only the selected objects (default: everything that is output)"}}
TOOLS = [
    ("openburn_launch", "Start OpenBurn (with automation enabled) if it is not running.", {}, launch),
    ("openburn_state", "Current document: work area, device, layers + cut settings, objects (index, type, layer, x/y/width/height in mm), selection, machine/laser connection status and recent console log.",
     {}, lambda a: rpc("get_state")),
    ("openburn_set_work_area", "Set the work area (bed) size in mm. Gweike G2: 110 x 110.",
     {"width": {"type": "number"}, "height": {"type": "number"}}, lambda a: rpc("set_work_area", a)),
    ("openburn_new_document", "Start a new empty document (unsaved work is discarded — ask first).", {}, lambda a: rpc("new_document")),
    ("openburn_import_file", "Import artwork from a file on disk (SVG, DXF, PNG, JPG, …) into the active layer.",
     {"path": {"type": "string"}}, import_file),
    ("openburn_import_svg", "Import SVG given as text (sizes in mm/px as the SVG declares).",
     {"svg": {"type": "string"}}, lambda a: rpc("import", {"name": "import.svg", "text": a["svg"]})),
    ("openburn_select", "Select objects by index.", {"indices": IDX}, lambda a: rpc("select", a)),
    ("openburn_select_all", "Select all objects.", {}, lambda a: rpc("select_all")),
    ("openburn_clear_selection", "Deselect everything.", {}, lambda a: rpc("clear_selection")),
    ("openburn_delete", "Delete objects by index (ask the user before deleting their work).", {"indices": IDX}, lambda a: rpc("delete", a)),
    ("openburn_transform", "Move/resize/rotate the current selection: x/y = left/top in mm, width/height in mm, angle in degrees.",
     {"x": {"type": "number"}, "y": {"type": "number"}, "width": {"type": "number"}, "height": {"type": "number"}, "angle": {"type": "number"}},
     lambda a: rpc("transform", a)),
    ("openburn_align", "Align the selection. centerPage = centre on the work area.",
     {"mode": {"type": "string", "enum": ["left", "hcenter", "right", "top", "vcenter", "bottom", "centerPage", "centerHPage", "centerVPage"]}},
     lambda a: rpc("align", a)),
    ("openburn_set_layer", "Change a layer's cut settings (layer = id, name like C00, or index; default active layer). speed in mm/s, power/minPower in %, mode line|fill|offsetFill|image, interval in mm.",
     {"layer": {"type": "string"}, "mode": {"type": "string"}, "speed": {"type": "number"}, "power": {"type": "number"}, "minPower": {"type": "number"},
      "passes": {"type": "integer"}, "interval": {"type": "number"}, "angle": {"type": "number"}, "overscan": {"type": "number"}}, set_layer),
    ("openburn_assign_layer", "Move the selected objects to a layer.", {"layer": {"type": "string"}}, lambda a: rpc("assign_layer", a)),
    ("openburn_undo", "Undo.", {}, lambda a: rpc("undo")),
    ("openburn_redo", "Redo.", {}, lambda a: rpc("redo")),
    ("openburn_preview", "Picture of the current canvas (PNG).", {"scale": {"type": "number"}}, preview),
    ("openburn_gcode", "Generate the job G-code (first 60 lines, line count, estimated time) without running it.", SEL,
     lambda a: rpc("gcode", {"selectionOnly": bool(a.get("selection_only"))})),
    ("openburn_save_project", "Save the document as an .obrn project file.", {"path": {"type": "string"}}, save_project),
    ("openburn_open_project", "Open an .obrn project file (replaces the current document — ask first).", {"path": {"type": "string"}}, open_project),
    ("openburn_connect_g2", "Connect OpenBurn to the Gweike G2 bridge (g2bridge, ws://127.0.0.1:8767).", {}, lambda a: rpc("connect_g2")),
    ("openburn_disconnect", "Disconnect from the laser.", {}, lambda a: rpc("disconnect")),
    ("openburn_frame", "Red-light frame: the G2 traces the job's shapes (images: bounding box) for ~10 s. Low-power aiming light only.", SEL,
     lambda a: rpc("frame", {"selectionOnly": bool(a.get("selection_only"))})),
    ("openburn_start_job", "FIRES THE LASER. Only call after the user explicitly said go for this job, after seeing the frame. Requires user_confirmed=true.",
     {**SEL, "user_confirmed": {"type": "boolean"}}, start_job),
    ("openburn_stop", "Stop the running job/frame immediately (laser off + disarm).", {}, lambda a: rpc("stop")),
]
HANDLERS = {name: fn for name, _, _, fn in TOOLS}


def tool_list():
    return [{"name": n, "description": d, "inputSchema": {"type": "object", "properties": p}} for n, d, p, _ in TOOLS]


def respond(id_, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": id_}
    if error:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:
            continue                      # notifications (initialized, cancelled, …)
        if method == "initialize":
            respond(mid, {"protocolVersion": params.get("protocolVersion", PROTOCOL), "capabilities": {"tools": {}},
                          "serverInfo": {"name": "openburn", "version": "0.1.0"}})
        elif method == "ping":
            respond(mid, {})
        elif method == "tools/list":
            respond(mid, {"tools": tool_list()})
        elif method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            fn = HANDLERS.get(name)
            if not fn:
                respond(mid, error={"code": -32602, "message": f"unknown tool {name}"}); continue
            try:
                out = fn(args)
                if isinstance(out, dict) and "__image__" in out:
                    content = [{"type": "image", "data": out["__image__"], "mimeType": "image/png"}]
                else:
                    content = [{"type": "text", "text": out if isinstance(out, str) else json.dumps(out, indent=1)}]
                respond(mid, {"content": content, "isError": False})
            except Exception as e:
                respond(mid, {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True})
        else:
            respond(mid, error={"code": -32601, "message": f"method not found: {method}"})


if __name__ == "__main__":
    main()
