# g2bridge — Gweike G2 fibre galvo laser as a GRBL machine

Made by Claude (Anthropic) together with the repository owner.

`g2bridge.py` speaks GRBL over a local WebSocket (`ws://127.0.0.1:8767`) and drives a
Gweike G2 (BSL/SeaCAD board, USB `04b4:1004`) over USB with the protocol in
[`docs/GWEIKE-G2.md`](../../docs/GWEIKE-G2.md). In OpenBurn choose
**Port → Gweike G2 bridge (localhost)**.

- Program with only S0 (OpenBurn *Frame*) → red-light outline of the shapes.
- Program with power → mark job(s); a job is refused unless the same area was framed first.
- Points outside the 110 mm field are refused; `!` / Ctrl-X → laser off + disarm.

```
pip install pyusb          # or: pacman -S python-pyusb
python3 tools/g2bridge/g2bridge.py [--dry-run] [--freq 30] [--max-power 100]
```
Linux needs a udev rule giving your user access to `04b4:1004`.
