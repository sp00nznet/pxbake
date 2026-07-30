#!/usr/bin/env python3
"""Renders docs/demo.gif — the half of pxbake you can't screen-record.

The GUI gets a real screenshot (docs/gui.png). This part doesn't: it's a 30-minute
unattended install on hardware that reboots twice and reconfigures its own network
partway through. So it's rendered with termshot, from the exact commands
pxbake.stage2_sh() emits — check them against the source if you like.

    pip install pillow
    curl -O https://raw.githubusercontent.com/sp00nznet/termshot/main/termshot.py
    python docs/demo.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from termshot import Term, GREEN, YELLOW, CYAN, DIM, FG  # noqa: E402

# Windows has no DejaVu; Consolas is the local monospace pair.
FONTS = {}
if sys.platform == "win32":
    FONTS = {"reg": r"C:\Windows\Fonts\consola.ttf",
             "bold": r"C:\Windows\Fonts\consolab.ttf"}


def seg(*parts):
    """(text, color, bold) triples, defaulting to plain foreground."""
    return [(t, c, b) for t, c, b in parts]


def plain(text, color=FG, bold=False):
    return [(text, color, bold)]


# rows must cover every line the demo ever commits (23) — termshot's screen is
# additive and doesn't scroll, so anything past `rows` is silently clipped.
t = Term(title="pxvirt — first boot", font_size=20, rows=23,
         user="you@desk", cwd="~", **FONTS)

t.type("ssh pi@192.168.0.50")
t.reveal([
    plain("  Linux pxvirt 6.12.47-v8+ aarch64", DIM),
    None,
], 260)

t.type("sudo journalctl -fu pxbake-install")
t.reveal([
    seg(("  Starting ", FG, False), ("Install PXVIRT (pxbake)", CYAN, True),
        ("...", FG, False)),
    plain("  + curl -fsSL .../pxcloud/pxvirt/pveport.gpg -o /usr/share/keyrings/", DIM),
    plain("  + echo deb [arch=arm64 signed-by=...] ... trixie main", DIM),
    plain("  + apt-get update", DIM),
    seg(("  Get:5 ", DIM, False),
        ("https://mirrors.lierfang.com/pxcloud/pxvirt trixie InRelease", FG, False)),
    plain("  + apt-get -y install ifupdown2", DIM),
    plain("  + apt-get -y install proxmox-ve pve-edk2-firmware-aarch64 ...", DIM),
], 420)

t.reveal([
    seg(("  Setting up ", FG, False), ("pve-cluster", YELLOW, False),
        (" (9.0.1) ...", FG, False)),
    seg(("  Setting up ", FG, False), ("pve-manager", YELLOW, False),
        (" (9.0.3) ...", FG, False)),
    seg(("  Setting up ", FG, False), ("proxmox-ve", YELLOW, True),
        (" (9.0.0) ...", FG, False)),
    plain("  + cat >/etc/network/interfaces   # vmbr0, bridging eth0", DIM),
    plain("  + systemctl disable NetworkManager", DIM),
    plain("  + systemctl disable pxbake-install.service", DIM),
    seg(("  ", FG, False), ("Rebooting", GREEN, True), (".", FG, False)),
    None,
], 430)

t.reveal([
    seg(("  PXVIRT 9.0", GREEN, True), ("  —  node ", FG, False),
        ("pxvirt", CYAN, True)),
    seg(("  ", FG, False), ("https://192.168.0.50:8006", CYAN, True),
        ("   root / Linux PAM", DIM, False)),
    None,
], 700)

t.blink()

out = Path(__file__).resolve().parent / "demo.gif"
t.save_gif(out)
print(f"wrote {out} ({out.stat().st_size >> 10} KB)")
