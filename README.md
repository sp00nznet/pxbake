# pimox-builder

**Proxmox on a Raspberry Pi, without the thirteen steps — and without following a dead guide.**

Every "install Proxmox on a Pi" walkthrough is thirteen steps of SSH, `nano`, and
two reboots in a specific order. That's forty minutes of typing you'll get wrong
at step 9, when you overwrite `/etc/network/interfaces` on the very link you're
SSH'd in over.

The bigger problem: **most of those guides no longer work.** The repo they all
point at is switched off.

This is the current procedure, as a form with a Build button.

```
┌─────────────────────────────────────────────┐
│  PXVIRT on a Raspberry Pi.                  │
│  Fill in, Build, boot.                      │
│                                             │
│         Hostname  [ pimox              ]    │
│  Static IP / CIDR  [ 192.168.0.50/24   ]    │
│          Gateway  [ 192.168.0.1        ]    │
│       DNS server  [ 8.8.8.8            ]    │
│      Pi username  [ pi                 ]    │
│      Pi password  [ ••••••••           ]    │
│   PXVIRT root pw  [ ••••••••           ]    │
│       Wi-Fi SSID  [                    ]    │
│                     ☑ Enable SSH            │
│   Boot partition  [ E:\        ] [Browse…]  │
│                                             │
│  [               Build               ]      │
└─────────────────────────────────────────────┘
```

## How you use it

1. Flash **Raspberry Pi OS Lite (64-bit)** with Raspberry Pi Imager. Skip the
   OS-customisation dialog entirely — that's this tool's job now.
2. Leave the card in. Windows mounts the small FAT32 partition as a drive letter;
   macOS and Linux mount it as `bootfs`. The tool finds it on its own.
3. Run it — `python pimox_builder.py`, or `pimox-builder.exe` on Windows.
4. Fill in the form. Hit **Build**.
5. Eject, boot the Pi, go make coffee. A long coffee — it's an `apt upgrade` plus
   a full virtualisation stack, over an SD card.
6. `https://<your-ip>:8006`, log in as `root` with the password you typed, realm
   **Linux PAM**.

No SSH session. No `nmtui`. No editing network config over the network.

## Wait — Proxmox or PXVIRT?

PXVIRT. Same thing, new name, and this is the part the guides missed.

Proxmox VE has no official ARM build. The community port was called
**Proxmox-Port**, and its packages were served as `pveport` from `apqa.cn`. In
2025 the project was renamed **PXVIRT** — Proxmox® is a trademark — and the
maintainers [announced they were shutting off pveport
distribution](https://github.com/jiangcuo/pxvirt) to free up server budget.

They did. As of this writing:

```
$ curl https://global.mirrors.apqa.cn/proxmox/debian/pve/dists/
curl: (7) Failed to connect to global.mirrors.apqa.cn port 443: Connection refused

$ curl -sI https://mirrors.apqa.cn/proxmox/debian/pve/dists/ | head -1
HTTP/2 521
```

Both hosts are down. Any guide still pointing at them fails at `apt update`. The
live repo is `https://mirrors.lierfang.com/pxcloud/pxvirt`, and it's healthy —
1400 arm64 packages on `bookworm`, 319 on `trixie`. The web UI, the CLI, `qm`,
the whole thing is identical. You just install it from somewhere else now.

## What both guides get wrong

Checked against
[the gist](https://gist.github.com/enjikaka/52d62c9c5462748dbe35abe3c7e37f9a),
[the It's FOSS walkthrough](https://itsfoss.com/install-proxmox-raspberry-pi/),
and the [PXVIRT docs](https://docs.pxvirt.lierfang.com/en/installfromdebian.html).
Every row here is something the tool does differently.

| The guides say | What actually happens |
| --- | --- |
| `apqa.cn` repo | Dead, both hosts. Verified above. Use `mirrors.lierfang.com/pxcloud/pxvirt`. |
| Key into `/etc/apt/trusted.gpg.d/` *(gist)* | That key then vouches for **every** repo on the box. It's FOSS gets this right; we use `signed-by=` and `/usr/share/keyrings/`. |
| Edit `/etc/dhcpcd.conf` for a static IP *(It's FOSS)* | dhcpcd isn't in Bookworm. NetworkManager owns the network. Editing that file does exactly nothing — you reboot and still have DHCP. We write an NM keyfile. |
| `apt full-upgrade` and `dist-upgrade` *(gist)* | Those can **remove** packages, and with a PXVIRT repo enabled that means a generic kernel replacing the Pi's. Plain `upgrade` never removes anything. |
| Web UI at `http://…:8005` *(gist)* | It's `https`, and it's `8006`. |
| — | Nothing about **page size**. The Pi 5 boots `kernel_2712.img` with 16K pages; PXVIRT needs a 4K-page kernel. Without `kernel=kernel8.img` you get a broken install. |
| — | Nothing about **cgroups**. Without `cgroup_enable=cpuset cgroup_enable=memory cgroup_memory=1`, every LXC container reports zero memory and CPU. |
| Omits `pve-edk2-firmware-aarch64` *(gist)* | You need it for UEFI guest boot on arm64. It's FOSS includes it; we do too. |
| Bridge config at step 9, mid-session *(gist)* | That's the step that locks you out. We write it last and reboot straight into it. |
| Set the root password by hand at the end | Set non-interactively during install. Postfix's "Local only" prompt is preseeded too. |
| Bookworm only | The `trixie` repo exists now (PXVIRT 9). We read `$VERSION_CODENAME` from `/etc/os-release`, so whichever RPi OS you flashed just works. |

Only one thing left unfixed on purpose — see *port-less bridge* below.

## What it actually writes

Four files on the boot partition. That's the whole payload:

| File | Why |
| --- | --- |
| `firstrun.sh` | Stage 1. Hostname, `/etc/hosts`, your user, Wi-Fi, static IP. |
| `cmdline.txt` | Patched with the cgroup args, plus `systemd.run=` to fire stage 1 once. |
| `config.txt` | Patched with `kernel=kernel8.img` under a fresh `[all]`. |
| `ssh` | The magic empty file that turns on sshd. |

Stage 1 also installs a `pimox-install.service` that fires on the **second** boot,
once the network is genuinely up, and does the heavy half: keyring, repo,
`ifupdown2`, `proxmox-ve` and friends, the `vmbr0` bridge, the network handover,
the root password. Then it deletes itself and reboots.

Two stages because stage 1 runs in a stripped-down boot target with no network.
Trying to `apt install proxmox-ve` there is how you get a brick.

Watching it go sideways? `/boot/firmware/pimox-stage1.log` and
`/var/log/pimox-install.log` on the Pi. Both are `set -x`, so they're loud.

## The parts worth knowing about

**Wi-Fi Pis get a port-less bridge.** 802.11 can't be bridged — protocol fact,
not a corner we cut. On Wi-Fi the Pi's IP stays on `wlan0` under NetworkManager
and `vmbr0` comes up with `bridge-ports none`. The web UI is fine; your VMs get
an island. Want them on the LAN? Use Ethernet.

**Ethernet hands the network over completely.** Stage 2 deletes the NM profile
and disables NetworkManager so `ifupdown2` owns `vmbr0` alone. Two daemons
fighting over one interface is exactly the 3am page this tool exists to prevent.

**Your passwords ride on the SD card in plaintext, briefly.** They sit in
`firstrun.sh` until first boot, which deletes it. `crypt` left the Python
standard library in 3.13, and hashing them properly means a dependency for one
function. If someone can read your card before its first boot you have a larger
problem — but now you know about this one.

**It's a third-party repo run by one company.** Lierfang, not Proxmox GmbH.
That's the deal ARM virtualisation comes with; there is no official option.

## Running it

Python 3.10+ with Tk — that's every python.org install and every Windows install.
`pip install` nothing. One file, standard library only.

```
python pimox_builder.py
python pimox_builder.py --selftest
```

### Windows build

```powershell
.\build_win32.ps1     # -> dist\pimox-builder.exe, ~11 MB, no Python needed
```

PyInstaller is this repo's only dependency and it never ships inside the tool.
The script builds in a local `.venv\` on purpose: PyInstaller flat-refuses to run
if the obsolete `pathlib` backport is installed anywhere on the path, and plenty
of machines have it lying around. A clean venv can't.

The `.exe` is GUI-only (`--noconsole`), so run `--selftest` through Python.

## Tests

`--selftest` runs the generator against a fake boot partition and asserts on the
output: secrets survive shell quoting, wifi and ethernet produce different
bridges, the dead mirror is gone, `[all]` precedes `kernel=`, and running Build
twice doesn't stack duplicate kernel arguments.

That last one isn't hypothetical. The first version quietly appended
`systemd.unit=kernel-command-line.target` a second time on re-run, and its
cleanup `sed` didn't strip it either — which would have left the Pi rebooting
into a bare systemd target, forever, with no console message explaining why. The
assert found it before any hardware did.

## Targets

Raspberry Pi 4 and 5, Raspberry Pi OS Lite 64-bit (Bookworm or Trixie). Pi 3
works per the PXVIRT hardware notes but 4 GB of RAM is the realistic floor. Older
Pis are ARMv7 — no.

The generator is covered by `--selftest`, and every command it emits is traced to
a primary source in the table above. It has not yet been through a full hardware
run, so keep a monitor on the Pi for the first one and read the two logs.
