#!/usr/bin/env python3
"""pimox-builder — bake the Pimox install into a Raspberry Pi SD card.

Flash Raspberry Pi OS Lite (64-bit) with Raspberry Pi Imager, leave the card
plugged in, point this at the `bootfs` partition, fill in the form, hit Build.

It drops a first-boot script that does every step of the Pimox guide for you:
hostname, static IP, wifi, user, then Proxmox VE on the second boot.

Run with --selftest to check the generator without an SD card.
"""

import ipaddress
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

PVE_REPO = "https://global.mirrors.apqa.cn/proxmox/debian"
CMDLINE_ARGS = (
    "systemd.run=/boot/firmware/firstrun.sh "
    "systemd.run_success_action=reboot "
    "systemd.unit=kernel-command-line.target"
)

FIELDS = [
    ("hostname", "Hostname", "pimox"),
    ("ip", "Static IP / CIDR", "192.168.0.50/24"),
    ("gateway", "Gateway", "192.168.0.1"),
    ("dns", "DNS server", "8.8.8.8"),
    ("username", "Pi username", "pi"),
    ("password", "Pi password", ""),
    ("root_password", "Proxmox root password", ""),
    ("wifi_ssid", "Wi-Fi SSID (blank = ethernet)", ""),
    ("wifi_password", "Wi-Fi password", ""),
    ("wifi_country", "Wi-Fi country code", "US"),
]
SECRET_FIELDS = {"password", "root_password", "wifi_password"}


@dataclass
class Config:
    hostname: str = "pimox"
    ip: str = "192.168.0.50/24"
    gateway: str = "192.168.0.1"
    dns: str = "8.8.8.8"
    username: str = "pi"
    password: str = ""
    root_password: str = ""
    wifi_ssid: str = ""
    wifi_password: str = ""
    wifi_country: str = "US"
    ssh: bool = True
    conn_uuid: str = field(default_factory=lambda: str(uuid.uuid4()))

    @property
    def wifi(self) -> bool:
        return bool(self.wifi_ssid)

    @property
    def addr(self) -> str:
        return str(ipaddress.ip_interface(self.ip).ip)


def validate(cfg: Config) -> list[str]:
    errs = []
    if not cfg.hostname.replace("-", "").isalnum():
        errs.append("Hostname must be letters, digits and dashes only.")
    try:
        if ipaddress.ip_interface(cfg.ip).network.prefixlen == 32:
            errs.append("Static IP needs a prefix, e.g. 192.168.0.50/24")
    except ValueError:
        errs.append("Static IP must look like 192.168.0.50/24")
    for name, val in (("Gateway", cfg.gateway), ("DNS server", cfg.dns)):
        try:
            ipaddress.ip_address(val)
        except ValueError:
            errs.append(f"{name} must be a plain IP address.")
    if not cfg.username.isalnum() or cfg.username == "root":
        errs.append("Username must be alphanumeric and not 'root'.")
    if not cfg.password:
        errs.append("Pi password is required (you need it to SSH in).")
    if not cfg.root_password:
        errs.append("Proxmox root password is required (it's your web UI login).")
    if cfg.wifi:
        if not cfg.wifi_password:
            errs.append("Wi-Fi password is required when an SSID is set.")
        if len(cfg.wifi_country) != 2 or not cfg.wifi_country.isalpha():
            errs.append("Wi-Fi country must be a 2-letter code, e.g. US or GB.")
    for key, label, _ in FIELDS:
        if "\n" in getattr(cfg, key) or "\r" in getattr(cfg, key):
            errs.append(f"{label} cannot contain line breaks.")
    return errs


def _nm_connection(cfg: Config) -> str:
    """NetworkManager keyfile for the uplink. Stage 2 hands ethernet over to
    ifupdown2/vmbr0; on wifi NetworkManager keeps the link for good."""
    head = f"""[connection]
id=pimox-uplink
uuid={cfg.conn_uuid}
interface-name={'wlan0' if cfg.wifi else 'eth0'}
type={'wifi' if cfg.wifi else 'ethernet'}
autoconnect=true
"""
    wifi = f"""
[wifi]
mode=infrastructure
ssid={cfg.wifi_ssid}

[wifi-security]
key-mgmt=wpa-psk
psk={cfg.wifi_password}
""" if cfg.wifi else ""
    return head + wifi + f"""
[ipv4]
method=manual
address1={cfg.ip},{cfg.gateway}
dns={cfg.dns};

[ipv6]
method=disabled
"""


def _interfaces(cfg: Config) -> str:
    if cfg.wifi:
        # ponytail: 802.11 can't be bridged, so vmbr0 is port-less and the Pi's
        # own IP stays on wlan0. VMs get an isolated bridge; add NAT/masquerade
        # yourself if they need to reach the LAN. Wire it up for real bridging.
        return """auto lo
iface lo inet loopback

auto vmbr0
iface vmbr0 inet manual
    bridge-ports none
    bridge-stp off
    bridge-fd 0
"""
    return f"""auto lo
iface lo inet loopback

iface eth0 inet manual

auto vmbr0
iface vmbr0 inet static
    address {cfg.ip}
    gateway {cfg.gateway}
    bridge-ports eth0
    bridge-stp off
    bridge-fd 0
"""


def stage2_sh(cfg: Config) -> str:
    """Runs on the second boot, once networking is up: the actual Pimox install."""
    handover = "" if cfg.wifi else f"""
rm -f /etc/NetworkManager/system-connections/pimox-uplink.nmconnection
systemctl disable NetworkManager NetworkManager-wait-online || true
rm -f /etc/resolv.conf
echo 'nameserver {cfg.dns}' >/etc/resolv.conf
"""
    unmanaged = """
mkdir -p /etc/NetworkManager/conf.d
printf '[keyfile]\\nunmanaged-devices=interface-name:vmbr0\\n' \\
    >/etc/NetworkManager/conf.d/99-pimox.conf
""" if cfg.wifi else ""

    return f"""#!/bin/bash
# generated by pimox-builder — the Pimox guide, minus the typing
exec >/var/log/pimox-install.log 2>&1
set -x
export DEBIAN_FRONTEND=noninteractive
APT="apt-get -y -o Dpkg::Options::=--force-confold"

# The unit waits for network-online, but DNS is often a beat behind it.
for _ in $(seq 1 60); do getent hosts deb.debian.org && break; sleep 5; done

$APT update
$APT upgrade

echo "deb [arch=arm64] {PVE_REPO}/pve bookworm port" >/etc/apt/sources.list.d/pveport.list
curl -fsSL {PVE_REPO}/pveport.gpg -o /etc/apt/trusted.gpg.d/pveport.gpg || exit 1

$APT update
$APT full-upgrade
$APT dist-upgrade

debconf-set-selections <<'DEBCONF'
postfix postfix/main_mailer_type select Local only
postfix postfix/mailname string {cfg.hostname}.local
DEBCONF

$APT install ifupdown2
$APT install proxmox-ve postfix open-iscsi chrony mmc-utils usbutils

cat >/etc/network/interfaces <<'IFACES'
{_interfaces(cfg)}IFACES
{handover}{unmanaged}
chpasswd <<'PWEOF'
root:{cfg.root_password}
PWEOF

systemctl disable pimox-install.service
rm -f /etc/systemd/system/pimox-install.service "$0"
systemctl reboot
"""


def firstrun_sh(cfg: Config) -> str:
    """Runs on the first boot in a minimal systemd target: local config only."""
    wifi_bits = f"""
rfkill unblock wifi || true
raspi-config nonint do_wifi_country {cfg.wifi_country.upper()} || true
""" if cfg.wifi else ""
    ssh_bits = "systemctl enable ssh\n" if cfg.ssh else ""

    return f"""#!/bin/bash
# generated by pimox-builder — stage 1, deletes itself when done
BOOT=/boot/firmware
[ -d "$BOOT" ] || BOOT=/boot
exec >"$BOOT/pimox-stage1.log" 2>&1
set -x

echo '{cfg.hostname}' >/etc/hostname
cat >/etc/hosts <<'HOSTS'
127.0.0.1 localhost
{cfg.addr} {cfg.hostname}
::1 localhost ip6-localhost ip6-loopback
ff02::1 ip6-allnodes
ff02::2 ip6-allrouters
HOSTS

id -u '{cfg.username}' >/dev/null 2>&1 || \\
    useradd -m -s /bin/bash -G sudo,adm,dialout,video,plugdev,users '{cfg.username}'
chpasswd <<'PWEOF'
{cfg.username}:{cfg.password}
PWEOF
echo '{cfg.username} ALL=(ALL) NOPASSWD: ALL' >/etc/sudoers.d/010-pimox-nopasswd
chmod 440 /etc/sudoers.d/010-pimox-nopasswd
systemctl disable userconfig.service || true
rm -f /etc/ssh/sshd_config.d/rename_user.conf
{ssh_bits}
mkdir -p /etc/NetworkManager/system-connections
cat >/etc/NetworkManager/system-connections/pimox-uplink.nmconnection <<'NMEOF'
{_nm_connection(cfg)}NMEOF
chmod 600 /etc/NetworkManager/system-connections/pimox-uplink.nmconnection
{wifi_bits}
cat >/usr/local/sbin/pimox-install.sh <<'STAGE2EOF'
{stage2_sh(cfg)}STAGE2EOF
chmod 700 /usr/local/sbin/pimox-install.sh

cat >/etc/systemd/system/pimox-install.service <<'UNITEOF'
[Unit]
Description=Install Proxmox VE (pimox-builder)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/pimox-install.sh
TimeoutStartSec=0
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNITEOF
systemctl enable pimox-install.service

sed -i 's| systemd\\.[^ ]*||g' "$BOOT/cmdline.txt"
rm -f "$BOOT/firstrun.sh"
"""


def patch_cmdline(path: Path) -> None:
    """Append the systemd.run hooks, idempotently."""
    line = path.read_text(encoding="utf-8").strip()
    line = " ".join(w for w in line.split() if not w.startswith("systemd."))
    path.write_text(f"{line} {CMDLINE_ARGS}\n", encoding="utf-8")


def build(cfg: Config, boot: Path) -> list[str]:
    cmdline = boot / "cmdline.txt"
    if not cmdline.exists():
        raise FileNotFoundError(
            f"No cmdline.txt in {boot} — that's not a Raspberry Pi boot partition.\n"
            "Pick the small FAT32 one (usually labelled 'bootfs')."
        )
    (boot / "firstrun.sh").write_text(firstrun_sh(cfg), encoding="utf-8", newline="\n")
    patch_cmdline(cmdline)
    written = ["firstrun.sh", "cmdline.txt"]
    if cfg.ssh:
        (boot / "ssh").write_text("", encoding="utf-8")
        written.append("ssh")
    return written


# --------------------------------------------------------------------------- GUI

def gui() -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title("pimox-builder")
    root.resizable(False, False)
    frm = ttk.Frame(root, padding=12)
    frm.grid()

    ttk.Label(frm, text="Proxmox on a Raspberry Pi, without the 13 steps.",
              font=("", 10, "bold")).grid(row=0, column=0, columnspan=3,
                                          sticky="w", pady=(0, 10))

    entries = {}
    for i, (key, label, default) in enumerate(FIELDS, start=1):
        ttk.Label(frm, text=label).grid(row=i, column=0, sticky="e", padx=(0, 8), pady=2)
        e = ttk.Entry(frm, width=32, show="*" if key in SECRET_FIELDS else "")
        e.insert(0, default)
        e.grid(row=i, column=1, columnspan=2, sticky="we", pady=2)
        entries[key] = e

    row = len(FIELDS) + 1
    ssh_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(frm, text="Enable SSH", variable=ssh_var).grid(
        row=row, column=1, sticky="w", pady=(6, 2))

    row += 1
    ttk.Label(frm, text="Boot partition").grid(row=row, column=0, sticky="e", padx=(0, 8))
    boot_var = tk.StringVar()
    ttk.Entry(frm, textvariable=boot_var, width=24).grid(row=row, column=1, sticky="we")
    ttk.Button(frm, text="Browse…",
               command=lambda: boot_var.set(filedialog.askdirectory() or boot_var.get())
               ).grid(row=row, column=2, sticky="we", padx=(6, 0))

    row += 1
    status = ttk.Label(frm, text="Flash Raspberry Pi OS Lite 64-bit first, then point me at bootfs.",
                       wraplength=380, foreground="grey30")
    status.grid(row=row, column=0, columnspan=3, sticky="w", pady=(10, 6))

    def on_build():
        cfg = Config(ssh=ssh_var.get(),
                     **{k: entries[k].get().strip() for k, _, _ in FIELDS})
        errs = validate(cfg)
        if not boot_var.get():
            errs.append("Pick the boot partition.")
        if errs:
            messagebox.showerror("Fix these first", "\n".join(f"• {e}" for e in errs))
            return
        try:
            written = build(cfg, Path(boot_var.get()))
        except OSError as exc:
            messagebox.showerror("Build failed", str(exc))
            return
        status.config(text=f"Wrote {', '.join(written)}. Eject the card, boot the Pi, "
                           f"then wait ~30 min. Proxmox lands at https://{cfg.addr}:8006",
                      foreground="green4")

    row += 1
    ttk.Button(frm, text="Build", command=on_build).grid(
        row=row, column=0, columnspan=3, sticky="we", pady=(4, 0))
    root.mainloop()


def selftest() -> None:
    cfg = Config(password="p'w$1", root_password="r\"t", wifi_ssid="my net",
                 wifi_password="hunter2")
    assert validate(cfg) == [], validate(cfg)
    assert validate(Config(ip="192.168.0.50", password="a", root_password="b"))
    assert validate(Config(password="", root_password="b"))
    assert validate(Config(hostname="pi mox", password="a", root_password="b"))
    assert validate(Config(wifi_ssid="x", wifi_password="y", wifi_country="USA",
                           password="a", root_password="b"))

    s = firstrun_sh(cfg)
    assert "p'w$1" in s and "bridge-ports none" in s  # secrets literal, wifi bridge
    assert s.count("STAGE2EOF") == 2 and "pimox-install.service" in s
    assert 'psk=hunter2' in s and "do_wifi_country US" in s
    # stage 1 must strip systemd.unit= too, or the Pi reboots into a bare target
    assert r"'s| systemd\.[^ ]*||g'" in s

    wired = Config(password="a", root_password="b")
    assert "bridge-ports eth0" in firstrun_sh(wired)
    assert "systemctl disable NetworkManager" in stage2_sh(wired)
    assert "systemctl disable NetworkManager" not in stage2_sh(cfg)

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        boot = Path(d)
        (boot / "cmdline.txt").write_text("console=serial0 rootwait systemd.run=/old.sh\n")
        build(wired, boot)
        line = (boot / "cmdline.txt").read_text()
        assert "/old.sh" not in line and line.count("systemd.run=") == 1
        build(wired, boot)  # idempotent
        assert (boot / "cmdline.txt").read_text() == line
        assert (boot / "ssh").exists()
    print("selftest ok")


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else gui()
