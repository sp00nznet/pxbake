#!/usr/bin/env python3
"""pxbake — bake PXVIRT (Proxmox VE for ARM) into a Raspberry Pi SD card.

Pick a card, fill in the form, hit Bake. It downloads Raspberry Pi OS Lite,
writes it to the card, and drops a first-boot script that turns the Pi into a
PXVIRT node without you ever opening an SSH session.

Already flashed a card yourself? Pick its boot partition instead and it just
writes the config.

Run with --selftest to check the generator without touching any hardware.
"""

import hashlib
import ipaddress
import json
import lzma
import os
import queue
import string
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

# The old pimox/pveport repo (global.mirrors.apqa.cn) is gone — its packages were
# retired when Proxmox-Port became PXVIRT. This is the current one.
PXVIRT_REPO = "https://mirrors.lierfang.com/pxcloud/pxvirt"
PXVIRT_KEY = f"{PXVIRT_REPO}/pveport.gpg"
KEYRING = "/usr/share/keyrings/pxvirt.gpg"

_RPI = "https://downloads.raspberrypi.com"
# arm64 only — PXVIRT has no 32-bit build, so offering armhf would just be a
# guaranteed failure three reboots later. Bookworm is listed because PXVIRT 8
# carries far more arm64 packages than the trixie suite does.
IMAGES = [
    ("Raspberry Pi OS Lite 64-bit (Trixie)", f"{_RPI}/raspios_lite_arm64_latest"),
    ("Raspberry Pi OS Lite 64-bit (Bookworm)",
     f"{_RPI}/raspios_oldstable_lite_arm64_latest"),
    ("Raspberry Pi OS 64-bit, desktop (Trixie)", f"{_RPI}/raspios_arm64_latest"),
    ("Raspberry Pi OS 64-bit, desktop (Bookworm)",
     f"{_RPI}/raspios_oldstable_arm64_latest"),
]
LOCAL_IMAGE = "Local file…"

SETTINGS_NAME = "settings.json"

# What the GUI shows, in order. bake() reports against these keys.
STEPS = [
    ("image", "Get the image"),
    ("erase", "Erase the card"),
    ("write", "Write the image"),
    ("config", "Write the config"),
    ("verify", "Verify"),
]

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
    ("hostname", "Hostname", "pxvirt"),
    ("ip", "Static IP / CIDR", "192.168.0.50/24"),
    ("gateway", "Gateway", "192.168.0.1"),
    ("dns", "DNS server", "8.8.8.8"),
    ("username", "Pi username", "pi"),
    ("password", "Pi password", ""),
    ("root_password", "PXVIRT root password", ""),
    ("wifi_ssid", "Wi-Fi SSID (blank = ethernet)", ""),
    ("wifi_password", "Wi-Fi password", ""),
    ("wifi_country", "Wi-Fi country code", "US"),
    ("cluster_peer", "Cluster peer IP (blank = standalone)", ""),
    ("cluster_password", "Cluster peer root password", ""),
]
SECRET_FIELDS = {"password", "root_password", "wifi_password", "cluster_password"}

CHUNK = 4 << 20
SECTOR = 512
RETRIES = 5


@dataclass
class Config:
    hostname: str = "pxvirt"
    ip: str = "192.168.0.50/24"
    gateway: str = "192.168.0.1"
    dns: str = "8.8.8.8"
    username: str = "pi"
    password: str = ""
    root_password: str = ""
    wifi_ssid: str = ""
    wifi_password: str = ""
    wifi_country: str = "US"
    cluster_peer: str = ""
    cluster_password: str = ""
    ssh: bool = True
    conn_uuid: str = field(default_factory=lambda: str(uuid.uuid4()))

    @property
    def wifi(self) -> bool:
        return bool(self.wifi_ssid)

    @property
    def cluster(self) -> bool:
        return bool(self.cluster_peer)

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
    if cfg.cluster:
        try:
            if ipaddress.ip_address(cfg.cluster_peer) == ipaddress.ip_interface(cfg.ip).ip:
                errs.append("Cluster peer can't be this node's own IP.")
        except ValueError:
            errs.append("Cluster peer must be the plain IP of an existing node.")
        if not cfg.cluster_password:
            errs.append("Cluster peer root password is required to join a cluster.")
    for key, label, _ in FIELDS:
        if any(c in getattr(cfg, key) for c in "\r\n"):
            errs.append(f"{label} cannot contain line breaks.")
    return errs


# ------------------------------------------------------------------ generation

def _nm_connection(cfg: Config) -> str:
    """NetworkManager keyfile for the uplink. Stage 2 hands ethernet over to
    ifupdown2/vmbr0; on wifi NetworkManager keeps the link for good."""
    head = f"""[connection]
id=pxbake-uplink
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
        # ponytail: vmbr0 has no ports on wifi. A station-mode 802.11 link can't
        # carry other MACs — infrastructure data frames have three address fields
        # and no room for the original source, so the AP can't route replies to a
        # MAC that never associated. AP mode bridges fine; client mode needs
        # 4-address/WDS (`iw dev wlan0 set 4addr on`) and an AP that supports it.
        # Upgrade path if yours does: set 4addr and put wlan0 in bridge-ports.
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
rm -f /etc/NetworkManager/system-connections/pxbake-uplink.nmconnection
systemctl disable NetworkManager NetworkManager-wait-online || true
systemctl stop NetworkManager || true
rm -f /etc/resolv.conf
echo 'nameserver {cfg.dns}' >/etc/resolv.conf
""" if not cfg.wifi else """
mkdir -p /etc/NetworkManager/conf.d
printf '[keyfile]\\nunmanaged-devices=interface-name:vmbr0\\n' \\
    >/etc/NetworkManager/conf.d/99-pxbake.conf
"""

    return f"""#!/bin/bash
# generated by pxbake — docs.pxvirt.lierfang.com, minus the typing
exec >/var/log/pxbake-install.log 2>&1
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

systemctl disable pxbake-install.service
rm -f /etc/systemd/system/pxbake-install.service "$0"
systemctl reboot
"""


def stage3_sh(cfg: Config) -> str:
    """Runs on the third boot: join an existing cluster, once PXVIRT is up.

    Separate from stage 2 because `pvecm add` registers the node under whatever
    address it currently holds — run it before the vmbr0 handover and the cluster
    records the wrong link."""
    return f"""#!/bin/bash
# generated by pxbake — stage 3, joins a cluster then deletes itself
exec >/var/log/pxbake-cluster.log 2>&1
set -x

# Nothing to do if a previous run already got us in.
[ -f /etc/pve/corosync.conf ] && exit 0

for _ in $(seq 1 60); do
    systemctl is-active --quiet pve-cluster && break
    sleep 5
done

export DEBIAN_FRONTEND=noninteractive
command -v sshpass >/dev/null || apt-get -y install sshpass

# pvecm shells out to ssh, which refuses to continue on an unknown host key.
mkdir -p /root/.ssh && chmod 700 /root/.ssh
ssh-keyscan -H {cfg.cluster_peer} >>/root/.ssh/known_hosts 2>/dev/null

# ponytail: sshpass feeds the peer's root password to the ssh prompt pvecm
# raises. The alternative is authorising a key on the peer first, which pxbake
# has no way to do from here.
if sshpass -p "$(cat /root/.pxbake-peer-pw)" \\
        pvecm add {cfg.cluster_peer} --use_ssh 1; then
    shred -u /root/.pxbake-peer-pw
    systemctl disable pxbake-cluster.service
    rm -f /etc/systemd/system/pxbake-cluster.service "$0"
else
    echo "JOIN FAILED — retrying on next boot. To do it by hand:" >&2
    echo "  pvecm add {cfg.cluster_peer} --use_ssh 1" >&2
fi
"""


def _cluster_unit(cfg: Config) -> str:
    """Stage 1 lays down stage 3 too, but After= stage 2 so it can't race it.

    Stage 2 ends in a reboot, so on the boot where the install runs, stage 3
    simply waits and never fires. It gets its turn on the boot after."""
    if not cfg.cluster:
        return ""
    return f"""
umask 077
printf '%s' '{cfg.cluster_password}' >/root/.pxbake-peer-pw
cat >/usr/local/sbin/pxbake-cluster.sh <<'STAGE3EOF'
{stage3_sh(cfg)}STAGE3EOF
chmod 700 /usr/local/sbin/pxbake-cluster.sh

cat >/etc/systemd/system/pxbake-cluster.service <<'CUNITEOF'
[Unit]
Description=Join PXVIRT cluster (pxbake)
After=pxbake-install.service pve-cluster.service network-online.target
Wants=network-online.target
ConditionPathExists=/usr/bin/pvecm
ConditionPathExists=!/etc/pve/corosync.conf

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/pxbake-cluster.sh
TimeoutStartSec=0
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
CUNITEOF
systemctl enable pxbake-cluster.service
umask 022
"""


def firstrun_sh(cfg: Config) -> str:
    """Runs on the first boot in a minimal systemd target: local config only."""
    wifi_bits = f"""
rfkill unblock wifi || true
raspi-config nonint do_wifi_country {cfg.wifi_country.upper()} || true
""" if cfg.wifi else ""
    ssh_bits = "systemctl enable ssh\n" if cfg.ssh else ""

    return f"""#!/bin/bash
# generated by pxbake — stage 1, deletes itself when done
BOOT=/boot/firmware
[ -d "$BOOT" ] || BOOT=/boot
exec >"$BOOT/pxbake-stage1.log" 2>&1
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
echo '{cfg.username} ALL=(ALL) NOPASSWD: ALL' >/etc/sudoers.d/010-pxbake-nopasswd
chmod 440 /etc/sudoers.d/010-pxbake-nopasswd
systemctl disable userconfig.service || true
rm -f /etc/ssh/sshd_config.d/rename_user.conf
{ssh_bits}
mkdir -p /etc/NetworkManager/system-connections
cat >/etc/NetworkManager/system-connections/pxbake-uplink.nmconnection <<'NMEOF'
{_nm_connection(cfg)}NMEOF
chmod 600 /etc/NetworkManager/system-connections/pxbake-uplink.nmconnection
{wifi_bits}
cat >/usr/local/sbin/pxbake-install.sh <<'STAGE2EOF'
{stage2_sh(cfg)}STAGE2EOF
chmod 700 /usr/local/sbin/pxbake-install.sh

cat >/etc/systemd/system/pxbake-install.service <<'UNITEOF'
[Unit]
Description=Install PXVIRT (pxbake)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/pxbake-install.sh
TimeoutStartSec=0
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNITEOF
systemctl enable pxbake-install.service
{_cluster_unit(cfg)}
# Drop our own boot hooks, keep everything else (cgroup args included).
sed -i 's| systemd\\.[^ ]*||g' "$BOOT/cmdline.txt"
rm -f "$BOOT/firstrun.sh"
"""


def patch_cmdline(path: Path) -> None:
    """Add the cgroup args and the stage-1 hooks, idempotently."""
    # A kernel command line is plain text. Anything else means we're reading a
    # damaged filesystem, and appending our arguments to garbage yields a card
    # that boots to nothing and gives no clue why. Seen on a failing SD card.
    damaged = f"{path} is not readable text — the card's filesystem is damaged.\n" \
              "Re-flash it; if it happens again, the card is failing."
    try:
        existing = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        raise OSError(damaged) from None
    if any(not (c.isprintable() or c in "\r\n\t") for c in existing):
        raise OSError(damaged)
    words = [w for w in existing.split()
             if not w.startswith("systemd.") and not w.startswith("cgroup_")]
    write_sync(path, " ".join(words) + f" {CGROUP_ARGS} {STAGE1_ARGS}\n")


def patch_config_txt(path: Path) -> bool:
    """Force the 4K-page kernel. Returns True if the file was changed."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if "kernel=kernel8.img" in text:
        return False
    write_sync(path, text.rstrip("\n") + CONFIG_TXT_FIX)
    return True


def write_sync(path: Path, text: str) -> None:
    """Write and force it to the card, not just to Windows' cache.

    Path.write_text returns once the OS has the bytes; on removable media they
    can still be sitting in a cache when the card is pulled, which leaves a
    directory entry pointing at a cluster that was never written. fsync is the
    difference between "saved" and "on the card"."""
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())


def is_raspios(boot: Path) -> bool:
    """Raspberry Pi OS specifically, not just any Pi-bootable card.

    pi-gen stamps issue.txt with "Raspberry Pi reference". LibreELEC, RetroPie,
    Ubuntu and friends all ship a cmdline.txt too, and everything pxbake writes
    (systemd.run, useradd, apt) assumes Debian — so cmdline.txt alone is not
    enough to tell them apart."""
    try:
        return "Raspberry Pi reference" in (boot / "issue.txt").read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return False


def build(cfg: Config, boot: Path) -> list[str]:
    cmdline = boot / "cmdline.txt"
    if not cmdline.exists():
        raise FileNotFoundError(
            f"No cmdline.txt in {boot} — that's not a Raspberry Pi boot partition.\n"
            "Pick the small FAT32 one (usually labelled 'bootfs')."
        )
    if not is_raspios(boot):
        raise OSError(
            f"{boot} is a Pi boot partition, but not Raspberry Pi OS — no\n"
            "'Raspberry Pi reference' in issue.txt. LibreELEC, RetroPie and Ubuntu\n"
            "all look like this, and everything pxbake writes assumes Debian.\n"
            "Pick the whole disk instead to erase it and install Raspberry Pi OS."
        )
    write_sync(boot / "firstrun.sh", firstrun_sh(cfg))
    patch_cmdline(cmdline)
    written = ["firstrun.sh", "cmdline.txt"]
    if patch_config_txt(boot / "config.txt"):
        written.append("config.txt")
    if cfg.ssh:
        write_sync(boot / "ssh", "")
        written.append("ssh")
    return written


# ----------------------------------------------------------------- image + disk

def cache_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or (Path.home() / ".cache")
    d = Path(base) / "pxbake"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ps(script: str) -> str:
    """Run PowerShell and return stdout. Windows disk plumbing only."""
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout


def parse_disks(raw: str) -> list[dict]:
    """Removable disks with media in them, from Get-Disk JSON.

    An empty card reader still enumerates as a USB disk — Size 0, OperationalStatus
    "No Media". Offering that as a target just fails later with a confusing error,
    so drop it here and let Rescan pick the card up once it's seated."""
    if not raw.strip():
        return []
    data = json.loads(raw)
    if isinstance(data, dict):
        data = [data]
    return [d for d in data
            if d.get("BusType") in ("USB", "SD", "MMC")
            and not d.get("IsSystem") and not d.get("IsBoot")
            and (d.get("Size") or 0) > 0
            and d.get("OperationalStatus") != "No Media"]


def list_disks() -> list[dict]:
    if sys.platform != "win32":
        return []  # ponytail: enumeration is Windows-only; elsewhere type a device path
    try:
        return parse_disks(_ps(
            "Get-Disk | Select-Object Number,FriendlyName,Size,BusType,IsSystem,"
            "IsBoot,OperationalStatus | ConvertTo-Json -Compress"))
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError):
        return []


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


def is_admin() -> bool:
    if sys.platform != "win32":
        return os.geteuid() == 0
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def resolve_image(source: str, progress) -> Path:
    """A URL to fetch, or a path to an image already on disk."""
    if not source.lower().startswith(("http://", "https://")):
        path = Path(source)
        if not path.is_file():
            raise FileNotFoundError(f"No such image file: {path}")
        if path.suffix.lower() not in (".img", ".xz"):
            raise OSError(f"{path.name} is not a .img or .img.xz.")
        progress("image", f"Using {path.name} ({path.stat().st_size >> 20} MB)", 1.0)
        return path
    return download_image(source, progress)


def download_image(url: str, progress) -> Path:
    """Fetch a Raspberry Pi OS image, cached and checksum-verified."""
    progress("image", "Looking up the current image…", None)
    sha_line = urllib.request.urlopen(f"{url}.sha256", timeout=60).read().decode()
    expected, name = sha_line.split()[0], sha_line.split()[1].strip("*")
    dest = cache_dir() / name

    if dest.exists() and _sha256(dest, progress, f"Checking cached {name}") == expected:
        progress("image", f"Using cached {name}", 1.0)
        return dest

    progress("image", f"Downloading {name}", 0.0)
    tmp = dest.with_suffix(dest.suffix + ".part")
    # Half a gigabyte over one socket; a single stall shouldn't cost the lot.
    # Resume from what's on disk with a Range request, and if the server ignores
    # it (200 rather than 206) fall back to starting over rather than appending
    # a second copy onto the first.
    for attempt in range(1, RETRIES + 1):
        have = tmp.stat().st_size if tmp.exists() else 0
        req = urllib.request.Request(url)
        if have:
            req.add_header("Range", f"bytes={have}-")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                resuming = getattr(r, "status", 200) == 206
                have = have if resuming else 0
                total = have + int(r.headers.get("Content-Length") or 0)
                with open(tmp, "ab" if resuming else "wb") as f:
                    done = have
                    while chunk := r.read(CHUNK):
                        f.write(chunk)
                        done += len(chunk)
                        progress("image",
                                 f"Downloading {name} — {done >> 20} of "
                                 f"{total >> 20} MB",
                                 done / total if total else None)
            break
        except urllib.error.HTTPError as exc:
            if exc.code != 416:
                raise
            break  # Range past the end: the .part is already whole, go verify it
        except OSError as exc:  # TimeoutError and URLError are both OSError
            if attempt == RETRIES:
                raise
            progress("image", f"Stalled at {tmp.stat().st_size >> 20} MB — "
                              f"resuming, attempt {attempt + 1} of {RETRIES}", None)
            time.sleep(2)

    if _sha256(tmp, progress, f"Checking {name}") != expected:
        tmp.unlink()
        raise OSError("Downloaded image failed its SHA-256 check. Try again.")
    tmp.replace(dest)
    progress("image", f"Downloaded and verified {name}", 1.0)
    return dest


def _sha256(path: Path, progress, label: str) -> str:
    h = hashlib.sha256()
    total, done = path.stat().st_size, 0
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
            done += len(chunk)
            progress("image", f"{label} — {done >> 20} of {total >> 20} MB",
                     done / total)
    return h.hexdigest()


def wipe_disk(number: int) -> None:
    """Clear the partition table so Windows releases every volume on the disk.

    `diskpart clean` rather than Clear-Disk: it works on RAW and uninitialised
    disks too, which Clear-Disk refuses."""
    script = cache_dir() / "clean.txt"
    script.write_text(f"select disk {number}\nclean\n", encoding="ascii")
    subprocess.run(["diskpart", "/s", str(script)], capture_output=True,
                   text=True, check=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    script.unlink(missing_ok=True)


def verify_config(cfg: Config, boot: Path) -> None:
    """Read back what build() wrote. Cheap, and the difference between a card
    that boots and one that silently doesn't."""
    want = firstrun_sh(cfg)
    got = (boot / "firstrun.sh").read_text(encoding="utf-8", errors="replace")
    cmdline = (boot / "cmdline.txt").read_text(encoding="utf-8", errors="replace")
    if got != want or CGROUP_ARGS not in cmdline or "systemd.run=" not in cmdline:
        raise OSError(
            f"Wrote the config to {boot}, but reading it back gave something "
            f"else.\nThe card did not store what we sent it — re-seat it and "
            f"bake again;\nif it repeats, the card is failing.")


def write_image(img_xz: Path, device: str, progress) -> None:
    """Stream-decompress the .img.xz straight onto the raw device.

    Sector 0 goes on last, deliberately. The image's partition table lives in the
    first 512 bytes, so writing it first lets Windows re-detect the layout and
    auto-mount bootfs seconds into a multi-minute write — after which the
    filesystem driver is caching structures for a volume whose bytes we are still
    overwriting, and every config file written afterwards reads back as garbage.
    Leaving sector 0 zeroed means Windows sees an unpartitioned disk and has
    nothing to mount until we are finished.

    `diskpart clean` does not cover this — it dismounts what was there, not what
    our own write is about to create. Set-Disk -IsOffline does not either: it
    silently no-ops on removable media."""
    def aligned(b: bytes) -> bytes:  # raw device writes must be sector-aligned
        return b + b"\0" * (-len(b) % SECTOR)

    opener = lzma.open if img_xz.suffix.lower() == ".xz" else open
    with opener(img_xz, "rb") as src, open(device, "rb+", buffering=0) as dst:
        head = src.read(CHUNK)
        if len(head) < SECTOR:
            raise OSError(f"{img_xz.name} is too small to be a disk image.")
        mbr, head = head[:SECTOR], head[SECTOR:]
        dst.write(b"\0" * SECTOR)  # placeholder; the real one goes on at the end
        dst.write(aligned(head))
        written = SECTOR + len(head)
        while chunk := src.read(CHUNK):
            dst.write(aligned(chunk))
            written += len(chunk)
            progress("write", f"Writing {written >> 20} MB", None)
        dst.flush()
        os.fsync(dst.fileno())

        dst.seek(0)
        dst.write(mbr)
        dst.flush()
        os.fsync(dst.fileno())
    progress("write", f"Wrote {written >> 20} MB", 1.0)


def wait_for_boot_partition(before: set, timeout: int = 90, poll=None) -> Path:
    """After flashing, wait for the OS to mount the new bootfs."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for p in find_boot_partitions():
            if p not in before:
                return p
        if poll:
            poll()
        time.sleep(2)
    raise TimeoutError(
        "The card was written, but its boot partition never appeared.\n"
        "Re-plug the card, then run pxbake again and pick the boot partition.")


def bake(cfg: Config, number: int, source: str, progress) -> Path:
    """Fetch, flash, configure. Destroys everything on the chosen disk."""
    if not is_admin():
        raise PermissionError(
            "Writing a raw disk needs Administrator. Right-click pxbake and "
            "'Run as administrator', or pick an already-flashed boot partition.")
    img = resolve_image(source, progress)

    before = set(find_boot_partitions())
    progress("erase", f"Erasing disk {number}…", None)
    wipe_disk(number)
    progress("erase", f"Erased disk {number}", 1.0)

    progress("write", "Writing the image…", None)
    write_image(img, rf"\\.\PhysicalDrive{number}", progress)

    progress("config", "Waiting for the card to remount…", None)
    _ps("Update-HostStorageCache")
    boot = wait_for_boot_partition(before)
    build(cfg, boot)
    progress("config", f"Wrote the config to {boot}", 1.0)

    progress("verify", "Reading it back…", None)
    verify_config(cfg, boot)
    progress("verify", f"Verified — {boot} matches what we sent", 1.0)
    return boot


# ------------------------------------------------------------------- settings

def settings_path() -> Path:
    return cache_dir() / SETTINGS_NAME


def save_settings(cfg: Config, source: str) -> None:
    """Remember the form between runs — everything except the passwords.

    ponytail: secrets deliberately not persisted. They'd be plaintext on disk
    forever, to save typing them once per card."""
    data = {k: getattr(cfg, k) for k, _, _ in FIELDS if k not in SECRET_FIELDS}
    data["ssh"] = cfg.ssh
    data["image"] = source
    try:
        settings_path().write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass  # a settings file we can't write is not worth failing a bake over


def load_settings() -> dict:
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


# --------------------------------------------------------------------------- GUI

@dataclass
class Target:
    label: str
    boot: Path | None = None
    disk: int | None = None


def list_targets() -> list[Target]:
    targets = [Target(f"{p}  —  flashed card, write config only", boot=p)
               for p in find_boot_partitions() if is_raspios(p)]
    for d in list_disks():
        gb = round(d["Size"] / 1e9, 1)
        targets.append(Target(
            f"Disk {d['Number']}  —  {d['FriendlyName'].strip()}, {gb} GB  "
            f"—  ERASE and bake", disk=d["Number"]))
    return targets


def gui() -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    saved = load_settings()
    root = tk.Tk()
    root.title("pxbake")
    root.resizable(False, False)
    frm = ttk.Frame(root, padding=12)
    frm.grid()

    ttk.Label(frm, text="PXVIRT on a Raspberry Pi. Fill in, bake, boot.",
              font=("", 10, "bold")).grid(row=0, column=0, columnspan=3,
                                          sticky="w", pady=(0, 10))

    entries = {}
    for i, (key, label, default) in enumerate(FIELDS, start=1):
        ttk.Label(frm, text=label).grid(row=i, column=0, sticky="e", padx=(0, 8), pady=2)
        e = ttk.Entry(frm, width=40, show="*" if key in SECRET_FIELDS else "")
        e.insert(0, "" if key in SECRET_FIELDS else str(saved.get(key, default)))
        e.grid(row=i, column=1, columnspan=2, sticky="we", pady=2)
        entries[key] = e

    row = len(FIELDS) + 1
    ssh_var = tk.BooleanVar(value=bool(saved.get("ssh", True)))
    ttk.Checkbutton(frm, text="Enable SSH", variable=ssh_var).grid(
        row=row, column=1, sticky="w", pady=(6, 2))

    # -- image source ---------------------------------------------------------
    row += 1
    ttk.Separator(frm, orient="horizontal").grid(row=row, column=0, columnspan=3,
                                                 sticky="we", pady=(8, 6))
    row += 1
    ttk.Label(frm, text="Image").grid(row=row, column=0, sticky="e", padx=(0, 8))
    image_labels = [lbl for lbl, _ in IMAGES] + [LOCAL_IMAGE]
    image_combo = ttk.Combobox(frm, width=34, state="readonly", values=image_labels)
    image_combo.grid(row=row, column=1, sticky="we", pady=2)
    local_path = tk.StringVar()

    saved_image = saved.get("image", "")
    if saved_image and not saved_image.startswith("http"):
        image_combo.set(LOCAL_IMAGE)
        local_path.set(saved_image)
    else:
        match = [i for i, (_, u) in enumerate(IMAGES) if u == saved_image]
        image_combo.current(match[0] if match else 0)

    def on_browse():
        p = filedialog.askopenfilename(
            title="Pick a Raspberry Pi image",
            filetypes=[("Pi images", "*.img *.img.xz *.xz"), ("All files", "*.*")])
        if p:
            local_path.set(p)
            image_combo.set(LOCAL_IMAGE)
            image_note.config(text=Path(p).name)

    browse = ttk.Button(frm, text="Browse…", command=on_browse, width=10)
    browse.grid(row=row, column=2, sticky="we", padx=(6, 0))

    row += 1
    image_note = ttk.Label(frm, text="", foreground="grey40", wraplength=300)
    image_note.grid(row=row, column=1, columnspan=2, sticky="w")

    def chosen_image() -> str:
        """The URL to fetch, or the path to a local file."""
        if image_combo.get() == LOCAL_IMAGE:
            return local_path.get()
        return dict(IMAGES)[image_combo.get()]

    def on_image_change(_=None):
        if image_combo.get() == LOCAL_IMAGE:
            image_note.config(text=Path(local_path.get()).name if local_path.get()
                              else "Pick a .img or .img.xz with Browse…")
        else:
            image_note.config(text="Downloaded once, then cached and reused.")

    image_combo.bind("<<ComboboxSelected>>", on_image_change)
    on_image_change()

    # -- target ---------------------------------------------------------------
    row += 1
    ttk.Label(frm, text="Target").grid(row=row, column=0, sticky="e", padx=(0, 8))
    targets = list_targets()
    combo = ttk.Combobox(frm, width=34, state="readonly",
                         values=[t.label for t in targets])
    if targets:
        combo.current(0)
    combo.grid(row=row, column=1, sticky="we", pady=2)
    refresh = ttk.Button(frm, text="Rescan", width=10)
    refresh.grid(row=row, column=2, sticky="we", padx=(6, 0))

    # -- steps ----------------------------------------------------------------
    row += 1
    ttk.Separator(frm, orient="horizontal").grid(row=row, column=0, columnspan=3,
                                                 sticky="we", pady=(8, 6))
    row += 1
    steps_frame = ttk.Frame(frm)
    steps_frame.grid(row=row, column=0, columnspan=3, sticky="we")
    step_labels = {}
    for n, (key, title) in enumerate(STEPS):
        lbl = ttk.Label(steps_frame, text=f"  ·  {title}", foreground="grey55")
        lbl.grid(row=n, column=0, sticky="w")
        step_labels[key] = lbl

    row += 1
    bar = ttk.Progressbar(frm, mode="determinate", length=380)
    bar.grid(row=row, column=0, columnspan=3, sticky="we", pady=(8, 4))

    row += 1
    status = ttk.Label(frm, text="Insert a card, or pick a flashed one.",
                       wraplength=440, foreground="grey30")
    status.grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 6))

    row += 1
    buttons = ttk.Frame(frm)
    buttons.grid(row=row, column=0, columnspan=3, sticky="we", pady=(4, 0))
    save_btn = ttk.Button(buttons, text="Save settings", width=16)
    save_btn.pack(side="left")
    bake_btn = ttk.Button(buttons, text="Bake")
    bake_btn.pack(side="left", fill="x", expand=True, padx=(8, 0))

    events: queue.Queue = queue.Queue()

    def current_config() -> Config:
        return Config(ssh=ssh_var.get(),
                      **{k: entries[k].get().strip() for k, _, _ in FIELDS})

    def on_save():
        # Deliberately no validation — saving a half-filled form is the whole
        # point of a Save button. Validation belongs on Bake.
        save_settings(current_config(), chosen_image())
        status.config(text=f"Settings saved to {settings_path()} — passwords are "
                           f"not stored.", foreground="green4")

    save_btn.config(command=on_save)

    def reset_steps():
        for key, title in STEPS:
            step_labels[key].config(text=f"  ·  {title}", foreground="grey55")

    def mark_steps(active: str, done: bool):
        """Everything above the active step is finished, by construction."""
        order = [k for k, _ in STEPS]
        if active not in order:
            return
        here = order.index(active)
        for n, (key, title) in enumerate(STEPS):
            if n < here or (n == here and done):
                step_labels[key].config(text=f"  ✓  {title}", foreground="green4")
            elif n == here:
                step_labels[key].config(text=f"  →  {title}", foreground="black")
            else:
                step_labels[key].config(text=f"  ·  {title}", foreground="grey55")

    def on_refresh():
        nonlocal targets
        targets = list_targets()
        combo["values"] = [t.label for t in targets]
        if targets:
            combo.current(0)
        status.config(text=f"Found {len(targets)} target(s).", foreground="grey30")

    refresh.config(command=on_refresh)

    def finish(kind: str):
        bar.stop()
        bar.config(mode="determinate", value=100 if kind == "done" else 0)
        bake_btn.config(state="normal")
        refresh.config(state="normal")
        on_refresh()

    def pump():
        """Drain worker events on the Tk thread. Tk is not thread-safe."""
        try:
            while True:
                kind, step, text, frac = events.get_nowait()
                status.config(text=text, foreground={
                    "err": "red3", "done": "green4"}.get(kind, "grey30"))
                if step:
                    mark_steps(step, frac == 1.0)
                if frac is None:
                    bar.config(mode="indeterminate")
                    bar.start(15)
                else:
                    bar.stop()
                    bar.config(mode="determinate", value=frac * 100)
                if kind in ("done", "err"):
                    finish(kind)
        except queue.Empty:
            pass
        root.after(100, pump)

    def on_bake():
        if not targets:
            messagebox.showerror("No target", "No card found. Insert one and Rescan.")
            return
        cfg = current_config()
        errs = validate(cfg)
        if image_combo.get() == LOCAL_IMAGE and not local_path.get():
            errs.append("Pick an image file, or choose one to download.")
        if errs:
            messagebox.showerror("Fix these first", "\n".join(f"• {e}" for e in errs))
            return

        source = chosen_image()
        save_settings(cfg, source)
        target = targets[combo.current()]

        if target.boot is not None:
            try:
                written = build(cfg, target.boot)
                verify_config(cfg, target.boot)
            except OSError as exc:
                messagebox.showerror("Bake failed", str(exc))
                return
            reset_steps()
            mark_steps("verify", True)
            status.config(text=f"Wrote and verified {', '.join(written)}. Eject, "
                               f"boot the Pi, then https://{cfg.addr}:8006",
                          foreground="green4")
            return

        if not messagebox.askyesno(
                "Erase this disk?",
                f"{target.label}\n\nEverything on it will be destroyed, then "
                f"the image is written and configured.\n\nContinue?"):
            return

        bake_btn.config(state="disabled")
        refresh.config(state="disabled")
        reset_steps()

        def progress(step, text, frac=None):
            events.put(("info", step, text, frac))

        def worker():
            try:
                bake(cfg, target.disk, source, progress)
                events.put(("done", "verify",
                            f"Done. Eject the card, boot the Pi, wait ~30 min, "
                            f"then https://{cfg.addr}:8006", 1.0))
            except Exception as exc:
                events.put(("err", None, f"{type(exc).__name__}: {exc}", 0.0))

        threading.Thread(target=worker, daemon=True).start()

    bake_btn.config(command=on_bake)
    root.after(100, pump)
    root.mainloop()


# ---------------------------------------------------------------------- selftest

def _selftest_download() -> None:
    """A stalled socket 90% through a 500 MB download must not cost the download.

    Not hypothetical: the first real run died at 472/500 MB and started over.
    Fakes the network rather than touching it, so this stays hermetic and fast."""
    import io
    import tempfile

    data = b"pxbake" * 20_000
    digest = hashlib.sha256(data).hexdigest()
    sha_body = f"{digest}  test.img.xz\n".encode()

    class Resp(io.BytesIO):
        def __init__(self, payload, status=200):
            super().__init__(payload)
            self.status = status
            self.headers = {"Content-Length": str(len(payload))}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.close()

    class Stalls(Resp):
        """Hands over `limit` bytes, then behaves like a socket that hung."""
        def __init__(self, payload, limit):
            super().__init__(payload)
            self.limit = limit

        def read(self, n=-1):
            if self.tell() >= self.limit:
                raise TimeoutError("The read operation timed out")
            return super().read(min(n, self.limit - self.tell()))

    def run(second_response, preload=None):
        seen = []

        def fake_urlopen(req, timeout=None):
            url = req if isinstance(req, str) else req.full_url
            if url.endswith(".sha256"):
                return Resp(sha_body)
            rng = None if isinstance(req, str) else req.get_header("Range")
            seen.append(rng)
            return Stalls(data, 40_000) if rng is None else second_response(rng)

        real_open, real_cache, real_sleep = (
            urllib.request.urlopen, globals()["cache_dir"], time.sleep)
        with tempfile.TemporaryDirectory() as d:
            if preload is not None:
                (Path(d) / "test.img.xz.part").write_bytes(preload)
            try:
                urllib.request.urlopen = fake_urlopen
                globals()["cache_dir"] = lambda: Path(d)
                time.sleep = lambda _: None
                got = download_image("https://example.invalid/img",
                                     lambda *a, **k: None)
                return got.read_bytes(), seen
            finally:
                urllib.request.urlopen = real_open
                globals()["cache_dir"] = real_cache
                time.sleep = real_sleep

    # a 206 resumes from exactly where the stall left off — no gap, no overlap
    body, seen = run(lambda rng: Resp(data[int(rng.split("=")[1].rstrip("-")):],
                                      status=206))
    assert body == data, f"resumed download corrupt: {len(body)} vs {len(data)}"
    assert seen == [None, "bytes=40000-"], seen

    # a server that ignores Range answers 200 with the whole file; appending that
    # to the 40 KB already on disk would produce a corrupt image, so start over
    body, seen = run(lambda rng: Resp(data, status=200))
    assert body == data, f"restart after ignored Range corrupt: {len(body)}"

    # an already-complete .part asks for a range past the end and gets 416 —
    # that means done, not broken, and must not burn every retry before failing
    def http_416(_rng):
        raise urllib.error.HTTPError("u", 416, "Range Not Satisfiable", {}, None)

    body, seen = run(http_416, preload=data)
    assert body == data and seen == [f"bytes={len(data)}-"], (len(body), seen)


def _selftest_write_image() -> None:
    """The image must land byte-exact, with sector 0 written last.

    Writing the partition table first is what let Windows mount bootfs mid-write
    and corrupt every config file that followed. A plain file stands in for the
    device — same open("rb+") path, same fsync."""
    import tempfile

    # Must exceed CHUNK, or the write loop never iterates and the "mid-write"
    # snapshot below is really a post-write one — which is how this test first
    # fooled itself.
    src = bytes(range(256)) * 36_864  # 9 MiB, and obviously ordered
    with tempfile.TemporaryDirectory() as d:
        xz = Path(d) / "img.xz"
        xz.write_bytes(lzma.compress(src))
        dev = Path(d) / "device.bin"
        dev.write_bytes(b"\xee" * len(src))  # pre-existing junk, as on a used card

        # mid-write the partition table must still be absent, so snapshot then
        seen = {}

        def progress(step, text, frac=None):
            seen.setdefault("first_sector_during", dev.read_bytes()[:SECTOR])

        write_image(xz, str(dev), progress)

        assert dev.read_bytes() == src, "image did not land byte-exact"
        if "first_sector_during" in seen:
            assert seen["first_sector_during"] == b"\0" * SECTOR, \
                "partition table appeared before the write finished"

    # a file too short to hold a partition table is not a disk image
    with tempfile.TemporaryDirectory() as d:
        xz = Path(d) / "tiny.xz"
        xz.write_bytes(lzma.compress(b"nope"))
        dev = Path(d) / "device.bin"
        dev.write_bytes(b"\0" * SECTOR)
        try:
            write_image(xz, str(dev), lambda *a, **k: None)
            raise AssertionError("should reject an undersized image")
        except OSError as exc:
            assert "too small" in str(exc), exc


def _selftest_settings_and_source() -> None:
    """Settings must survive a round trip without ever storing a password."""
    import tempfile

    cfg = Config(hostname="node7", ip="10.0.0.9/24", gateway="10.0.0.1",
                 password="secret-pi", root_password="secret-root",
                 wifi_ssid="net", wifi_password="secret-wifi",
                 cluster_peer="10.0.0.5", cluster_password="secret-peer",
                 ssh=False)
    real_cache = globals()["cache_dir"]
    with tempfile.TemporaryDirectory() as d:
        try:
            globals()["cache_dir"] = lambda: Path(d)
            assert load_settings() == {}  # nothing saved yet
            save_settings(cfg, IMAGES[1][1])
            raw = settings_path().read_text(encoding="utf-8")
            for secret in ("secret-pi", "secret-root", "secret-wifi", "secret-peer"):
                assert secret not in raw, f"{secret} was written to disk"
            got = load_settings()
            assert got["hostname"] == "node7" and got["ip"] == "10.0.0.9/24"
            assert got["cluster_peer"] == "10.0.0.5"   # peer address is not secret
            assert got["ssh"] is False and got["image"] == IMAGES[1][1]

            settings_path().write_text("{not json", encoding="utf-8")
            assert load_settings() == {}  # a corrupt file must not stop the app

            # a local image is used as-is; a URL goes to the downloader
            img = Path(d) / "custom.img"
            img.write_bytes(b"\0" * SECTOR)
            assert resolve_image(str(img), lambda *a: None) == img
            try:
                resolve_image(str(Path(d) / "nope.img"), lambda *a: None)
                raise AssertionError("should reject a path that isn't there")
            except FileNotFoundError as exc:
                assert "No such image" in str(exc), exc

            notes = Path(d) / "notes.txt"
            notes.touch()
            try:
                resolve_image(str(notes), lambda *a: None)
                raise AssertionError("should reject a file that isn't an image")
            except OSError as exc:
                assert "not a .img" in str(exc), exc
        finally:
            globals()["cache_dir"] = real_cache

    # an uncompressed .img must write too, not just .img.xz
    with tempfile.TemporaryDirectory() as d:
        src = bytes(range(256)) * 36_864
        raw = Path(d) / "plain.img"
        raw.write_bytes(src)
        dev = Path(d) / "dev.bin"
        dev.write_bytes(b"\xee" * len(src))
        write_image(raw, str(dev), lambda *a, **k: None)
        assert dev.read_bytes() == src, "plain .img did not land byte-exact"


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
    assert s.count("STAGE2EOF") == 2 and "pxbake-install.service" in s
    assert "psk=hunter2" in s and "do_wifi_country US" in s
    # stage 1 must strip systemd.unit= too, or the Pi reboots into a bare target
    assert r"'s| systemd\.[^ ]*||g'" in s
    assert "cgroup_" not in s.split("sed -i")[1]  # ...but must not eat cgroup args
    assert "pimox" not in s and "Pimox" not in s  # renamed everywhere

    # cluster join is opt-in and leaves no trace when it's off
    assert "pxbake-cluster" not in s and "sshpass" not in s
    joiner = Config(password="a", root_password="b", ip="192.168.1.9/24",
                    cluster_peer="192.168.1.5", cluster_password="peer'pw")
    assert validate(joiner) == [], validate(joiner)
    assert validate(Config(password="a", root_password="b",
                           cluster_peer="192.168.1.5"))          # needs a password
    assert validate(Config(password="a", root_password="b", ip="192.168.1.5/24",
                           cluster_peer="192.168.1.5", cluster_password="x"))  # self
    j = firstrun_sh(joiner)
    assert j.count("STAGE3EOF") == 2 and "systemctl enable pxbake-cluster" in j
    assert "peer'pw" in j and "ssh-keyscan -H 192.168.1.5" in j
    # stage 3 must not race stage 2, and must no-op on an already-clustered node
    assert "After=pxbake-install.service" in j
    assert "ConditionPathExists=!/etc/pve/corosync.conf" in j
    assert "shred -u /root/.pxbake-peer-pw" in stage3_sh(joiner)

    wired = Config(password="a", root_password="b")
    assert "bridge-ports eth0" in firstrun_sh(wired)
    s2, s2w = stage2_sh(wired), stage2_sh(cfg)
    assert "systemctl disable NetworkManager" in s2
    assert "systemctl disable NetworkManager" not in s2w
    assert "unmanaged-devices=interface-name:vmbr0" in s2w
    assert "mirrors.lierfang.com" in s2 and "apqa.cn" not in s2
    assert "$VERSION_CODENAME" in s2 and f"signed-by={KEYRING}" in s2
    assert "$APT full-upgrade" not in s2 and "$APT dist-upgrade" not in s2
    assert "pve-edk2-firmware-aarch64" in s2

    # never offer the disk Windows is running from, nor an empty card reader
    disks = parse_disks(json.dumps([
        {"Number": 0, "FriendlyName": "NVMe", "Size": 1e12, "BusType": "NVMe",
         "IsSystem": True, "IsBoot": True, "OperationalStatus": "Online"},
        {"Number": 1, "FriendlyName": "SanDisk", "Size": 3e10, "BusType": "USB",
         "IsSystem": False, "IsBoot": False, "OperationalStatus": "Online"},
        {"Number": 2, "FriendlyName": "USB but booted", "Size": 3e10,
         "BusType": "USB", "IsSystem": False, "IsBoot": True,
         "OperationalStatus": "Online"},
        {"Number": 5, "FriendlyName": "Mass Storage Device", "Size": 0,
         "BusType": "USB", "IsSystem": False, "IsBoot": False,
         "OperationalStatus": "No Media"},
    ]))
    assert [d["Number"] for d in disks] == [1], disks
    assert parse_disks("") == []
    assert parse_disks(json.dumps({"Number": 3, "FriendlyName": "Lone SD",
                                   "Size": 1e10, "BusType": "SD"}))  # single = dict

    _selftest_download()
    _selftest_write_image()
    _selftest_settings_and_source()

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        boot = Path(d)
        (boot / "cmdline.txt").write_text(
            "console=serial0 rootwait systemd.run=/old.sh cgroup_enable=memory\n")
        (boot / "config.txt").write_text("[pi5]\ndtparam=nvme\n")

        # a cmdline.txt is not proof of Raspberry Pi OS — LibreELEC has one too,
        # and writing a Debian firstrun.sh onto it just breaks someone's Kodi box
        assert not is_raspios(boot)
        try:
            build(wired, boot)
            raise AssertionError("should refuse a non-Raspberry-Pi-OS partition")
        except OSError as exc:
            assert "not Raspberry Pi OS" in str(exc), exc
        (boot / "issue.txt").write_text(
            "Raspberry Pi reference 2026-06-18\nGenerated using pi-gen\n")
        assert is_raspios(boot)
        assert set(build(wired, boot)) == {"firstrun.sh", "cmdline.txt",
                                           "config.txt", "ssh"}
        line = (boot / "cmdline.txt").read_text()
        assert "/old.sh" not in line and line.count("systemd.run=") == 1
        assert line.count("cgroup_enable=memory") == 1 and "cgroup_enable=cpuset" in line
        cfgtxt = (boot / "config.txt").read_text()
        # [all] resets the model filter, else kernel= would only apply to [pi5]
        assert cfgtxt.endswith("[all]\nkernel=kernel8.img\n") and "dtparam=nvme" in cfgtxt

        assert build(wired, boot) == ["firstrun.sh", "cmdline.txt", "ssh"]  # idempotent

        # a good write reads back identical; a mangled one must be caught here
        verify_config(wired, boot)
        (boot / "firstrun.sh").write_text("truncated by a stale mount\n")
        try:
            verify_config(wired, boot)
            raise AssertionError("should notice firstrun.sh read back wrong")
        except OSError as exc:
            assert "did not store what we sent it" in str(exc), exc
        build(wired, boot)  # put it back

        # a corrupt cmdline.txt must stop the bake, not get our args appended to it
        (boot / "cmdline.txt").write_bytes(b"\xfb\x2c\x0b\x4d\xfc\x2c\x0b\x4d" * 8)
        try:
            build(wired, boot)
            raise AssertionError("should refuse to patch a garbage cmdline.txt")
        except OSError as exc:
            assert "damaged" in str(exc), exc
        (boot / "cmdline.txt").write_text(line)  # put it back for later asserts
        assert (boot / "cmdline.txt").read_text() == line
        assert (boot / "config.txt").read_text() == cfgtxt

        # The new-partition wait must ignore partitions that were already there.
        # Seed `before` with whatever is really plugged into this machine, or the
        # test passes or fails depending on whether a card happens to be in.
        try:
            wait_for_boot_partition(set(find_boot_partitions()) | {boot}, timeout=1)
            raise AssertionError("should have timed out on a known partition")
        except TimeoutError:
            pass
    print("selftest ok")


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else gui()
