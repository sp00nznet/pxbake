#!/usr/bin/env python3
"""pimox-builder — bake PXVIRT (Proxmox VE for ARM) into a Raspberry Pi SD card.

Flash Raspberry Pi OS Lite (64-bit) with Raspberry Pi Imager, leave the card
plugged in, point this at the boot partition, fill in the form, hit Build.

It drops a first-boot script that does every step for you: hostname, static IP,
wifi, user, the 4K-page kernel and cgroup fixes the Pi needs, then PXVIRT on
the second boot.

Run with --selftest to check the generator without an SD card.
"""

import ipaddress
import string
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

# The old pimox/pveport repo (global.mirrors.apqa.cn) is dead — jiangcuo stopped
# distributing pveport debs and the project moved to PXVIRT. This is the live one.
PXVIRT_REPO = "https://mirrors.lierfang.com/pxcloud/pxvirt"
PXVIRT_KEY = f"{PXVIRT_REPO}/pveport.gpg"
KEYRING = "/usr/share/keyrings/pxvirt.gpg"

# LXC needs these or container memory/cpu accounting reads zero. They stay in
# cmdline.txt forever, unlike the systemd.run hooks stage 1 cleans up.
CGROUP_ARGS = "cgroup_enable=cpuset cgroup_enable=memory cgroup_memory=1"
STAGE1_ARGS = (
    "systemd.run=/boot/firmware/firstrun.sh "
    "systemd.run_success_action=reboot "
    "systemd.unit=kernel-command-line.target"
)
# Pi 5 boots kernel_2712.img with 16K pages; PXVIRT needs a 4K-page kernel.
# kernel8.img is the 4K arm64 kernel and is correct on Pi 4 too.
CONFIG_TXT_FIX = "\n[all]\nkernel=kernel8.img\n"

PACKAGES = (
    "proxmox-ve pve-edk2-firmware-aarch64 postfix "
    "open-iscsi chrony mmc-utils usbutils"
)

FIELDS = [
    ("hostname", "Hostname", "pimox"),
    ("ip", "Static IP / CIDR", "192.168.0.50/24"),
    ("gateway", "Gateway", "192.168.0.1"),
    ("dns", "DNS server", "8.8.8.8"),
    ("username", "Pi username", "pi"),
    ("password", "Pi password", ""),
    ("root_password", "PXVIRT root password", ""),
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
    if not cfg.hostname or not cfg.hostname.replace("-", "").isalnum():
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
        errs.append("PXVIRT root password is required (it's your web UI login).")
    if cfg.wifi:
        if not cfg.wifi_password:
            errs.append("Wi-Fi password is required when an SSID is set.")
        if len(cfg.wifi_country) != 2 or not cfg.wifi_country.isalpha():
            errs.append("Wi-Fi country must be a 2-letter code, e.g. US or GB.")
    for key, label, _ in FIELDS:
        if any(c in getattr(cfg, key) for c in "\r\n"):
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
        # yourself if they need the LAN, or use Ethernet for real bridging.
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
    """Runs on the second boot, once networking is up: the actual PXVIRT install."""
    # Ethernet: ifupdown2 takes vmbr0, so NetworkManager has to go entirely.
    # Wifi: NM keeps wlan0, so it stays — just tell it to leave vmbr0 alone.
    handover = f"""
rm -f /etc/NetworkManager/system-connections/pimox-uplink.nmconnection
systemctl disable NetworkManager NetworkManager-wait-online || true
systemctl stop NetworkManager || true
rm -f /etc/resolv.conf
echo 'nameserver {cfg.dns}' >/etc/resolv.conf
""" if not cfg.wifi else """
mkdir -p /etc/NetworkManager/conf.d
printf '[keyfile]\\nunmanaged-devices=interface-name:vmbr0\\n' \\
    >/etc/NetworkManager/conf.d/99-pimox.conf
"""

    return f"""#!/bin/bash
# generated by pimox-builder — docs.pxvirt.lierfang.com, minus the typing
exec >/var/log/pimox-install.log 2>&1
set -x
export DEBIAN_FRONTEND=noninteractive
# ponytail: plain `upgrade` only. full-upgrade/dist-upgrade let the PXVIRT repo
# pull a generic kernel and drop the Pi's own — `upgrade` never removes packages.
APT="apt-get -y -o Dpkg::Options::=--force-confold"

# The unit waits for network-online, but DNS is often a beat behind it.
for _ in $(seq 1 60); do getent hosts mirrors.lierfang.com && break; sleep 5; done

$APT update
$APT install curl ca-certificates
$APT upgrade

curl -fsSL {PXVIRT_KEY} -o {KEYRING} || exit 1
. /etc/os-release
echo "deb [arch=arm64 signed-by={KEYRING}] {PXVIRT_REPO} $VERSION_CODENAME main" \\
    >/etc/apt/sources.list.d/pxvirt.list
$APT update

debconf-set-selections <<'DEBCONF'
postfix postfix/main_mailer_type select Local only
postfix postfix/mailname string {cfg.hostname}.local
DEBCONF

# ifupdown2 first and on its own — it conflicts with ifupdown, and untangling
# that mid-way through a proxmox-ve install is not a good afternoon.
$APT install ifupdown2
rm -f /etc/network/interfaces.new
$APT install {PACKAGES}

cat >/etc/network/interfaces <<'IFACES'
{_interfaces(cfg)}IFACES
{handover}
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
{cfg.addr} {cfg.hostname}.local {cfg.hostname}
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
Description=Install PXVIRT (pimox-builder)
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

# Drop our own boot hooks, keep everything else (cgroup args included).
sed -i 's| systemd\\.[^ ]*||g' "$BOOT/cmdline.txt"
rm -f "$BOOT/firstrun.sh"
"""


def patch_cmdline(path: Path) -> None:
    """Add the cgroup args and the stage-1 hooks, idempotently."""
    words = [w for w in path.read_text(encoding="utf-8").split()
             if not w.startswith("systemd.") and not w.startswith("cgroup_")]
    path.write_text(" ".join(words) + f" {CGROUP_ARGS} {STAGE1_ARGS}\n",
                    encoding="utf-8", newline="\n")


def patch_config_txt(path: Path) -> bool:
    """Force the 4K-page kernel. Returns True if the file was changed."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if "kernel=kernel8.img" in text:
        return False
    path.write_text(text.rstrip("\n") + CONFIG_TXT_FIX, encoding="utf-8", newline="\n")
    return True


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
    if patch_config_txt(boot / "config.txt"):
        written.append("config.txt")
    if cfg.ssh:
        (boot / "ssh").write_text("", encoding="utf-8")
        written.append("ssh")
    return written


def find_boot_partitions() -> list[Path]:
    """Anything mounted that looks like a Pi boot partition."""
    if sys.platform == "win32":
        roots = [Path(f"{c}:/") for c in string.ascii_uppercase[3:]]  # skip A/B/C
    else:
        roots = [p for base in ("/Volumes", "/media", "/run/media")
                 for pat in ("*", "*/*")
                 for p in Path(base).glob(pat)]
    found = []
    for r in roots:
        try:
            if (r / "cmdline.txt").is_file():
                found.append(r)
        except OSError:
            pass  # unreadable or disconnected drive
    return found


# --------------------------------------------------------------------------- GUI

def gui() -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title("pimox-builder")
    root.resizable(False, False)
    frm = ttk.Frame(root, padding=12)
    frm.grid()

    ttk.Label(frm, text="PXVIRT on a Raspberry Pi. Fill in, Build, boot.",
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
    detected = find_boot_partitions()
    boot_var = tk.StringVar(value=str(detected[0]) if detected else "")
    ttk.Entry(frm, textvariable=boot_var, width=24).grid(row=row, column=1, sticky="we")
    ttk.Button(frm, text="Browse…",
               command=lambda: boot_var.set(filedialog.askdirectory() or boot_var.get())
               ).grid(row=row, column=2, sticky="we", padx=(6, 0))

    row += 1
    hint = (f"Found a Pi boot partition at {detected[0]}." if detected else
            "Flash Raspberry Pi OS Lite 64-bit first, then point me at bootfs.")
    status = ttk.Label(frm, text=hint, wraplength=380, foreground="grey30")
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
                           f"then wait ~30 min. PXVIRT lands at https://{cfg.addr}:8006",
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
    assert "psk=hunter2" in s and "do_wifi_country US" in s
    # stage 1 must strip systemd.unit= too, or the Pi reboots into a bare target
    assert r"'s| systemd\.[^ ]*||g'" in s
    assert "cgroup_" not in s.split("sed -i")[1]  # ...but must not eat cgroup args

    wired = Config(password="a", root_password="b")
    assert "bridge-ports eth0" in firstrun_sh(wired)
    s2, s2w = stage2_sh(wired), stage2_sh(cfg)
    assert "systemctl disable NetworkManager" in s2
    assert "systemctl disable NetworkManager" not in s2w
    assert "unmanaged-devices=interface-name:vmbr0" in s2w
    assert "mirrors.lierfang.com" in s2 and "apqa.cn" not in s2  # dead mirror gone
    assert "$VERSION_CODENAME" in s2 and f"signed-by={KEYRING}" in s2
    assert "$APT full-upgrade" not in s2 and "$APT dist-upgrade" not in s2
    assert "pve-edk2-firmware-aarch64" in s2

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        boot = Path(d)
        (boot / "cmdline.txt").write_text(
            "console=serial0 rootwait systemd.run=/old.sh cgroup_enable=memory\n")
        (boot / "config.txt").write_text("[pi5]\ndtparam=nvme\n")
        assert set(build(wired, boot)) == {"firstrun.sh", "cmdline.txt", "config.txt", "ssh"}
        line = (boot / "cmdline.txt").read_text()
        assert "/old.sh" not in line and line.count("systemd.run=") == 1
        assert line.count("cgroup_enable=memory") == 1 and "cgroup_enable=cpuset" in line
        cfgtxt = (boot / "config.txt").read_text()
        # [all] resets the model filter, else kernel= would only apply to [pi5]
        assert cfgtxt.endswith("[all]\nkernel=kernel8.img\n") and "dtparam=nvme" in cfgtxt

        assert build(wired, boot) == ["firstrun.sh", "cmdline.txt", "ssh"]  # idempotent
        assert (boot / "cmdline.txt").read_text() == line
        assert (boot / "config.txt").read_text() == cfgtxt

        assert boot in find_boot_partitions() or sys.platform == "win32"
    print("selftest ok")


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else gui()
