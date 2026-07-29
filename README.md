# pimox-builder

**Proxmox on a Raspberry Pi, without the thirteen steps.**

The [Pimox guide](https://gist.github.com/enjikaka/52d62c9c5462748dbe35abe3c7e37f9a)
is thirteen steps of SSH, `nano`, and two reboots you have to remember to do in
the right order. It works. It's also forty minutes of typing you'll get wrong at
step 9 when you swap the interfaces file and lock yourself out of the box.

This is that guide, as a form with a Build button.

```
┌─────────────────────────────────────────────┐
│  Proxmox on a Raspberry Pi, without         │
│  the 13 steps.                              │
│                                             │
│         Hostname  [ pimox              ]    │
│  Static IP / CIDR  [ 192.168.0.50/24   ]    │
│          Gateway  [ 192.168.0.1        ]    │
│       DNS server  [ 8.8.8.8            ]    │
│      Pi username  [ pi                 ]    │
│      Pi password  [ ••••••••           ]    │
│   Proxmox root pw  [ ••••••••          ]    │
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
   macOS and Linux mount it as `bootfs`.
3. `python pimox_builder.py`
4. Fill in the form. Point **Boot partition** at that drive. Hit **Build**.
5. Eject, boot the Pi, go make coffee. A long coffee — it's a full
   `dist-upgrade` plus Proxmox over an SD card.
6. `https://<your-ip>:8006`, log in as `root` with the password you typed.

Nothing else. No SSH session, no `nmtui`, no editing `/etc/network/interfaces`
over the very link you're about to reconfigure.

## What it actually writes

Three files onto the boot partition — that's the whole payload:

| File | Why |
| --- | --- |
| `firstrun.sh` | Stage 1. Hostname, `/etc/hosts`, your user, Wi-Fi, static IP. |
| `cmdline.txt` | Patched with `systemd.run=` so the kernel runs stage 1 once. |
| `ssh` | The magic empty file that turns on sshd. |

Stage 1 also installs a `pimox-install.service` that fires on the **second**
boot, once the network is genuinely up, and does the heavy half: the pveport
repo and key, the four flavours of upgrade, `proxmox-ve` and friends, the
`vmbr0` bridge, the root password. Then it deletes itself and reboots.

Two stages because stage 1 runs in a stripped-down boot target with no network.
Trying to `apt-get install proxmox-ve` there is how you get a brick.

Watching it go sideways? `/boot/firmware/pimox-stage1.log` and
`/var/log/pimox-install.log` on the Pi. Both are `set -x`, so they're loud.

## The parts worth knowing about

**Wi-Fi Pis get a port-less bridge.** 802.11 can't be bridged — that's a
protocol fact, not a bug we skipped. On Wi-Fi the Pi's own IP stays on `wlan0`
under NetworkManager, and `vmbr0` comes up with `bridge-ports none`. The web UI
works fine; your VMs get an island. If you want them on the LAN, run Ethernet.

**Your passwords ride on the SD card in plaintext, briefly.** They sit in
`firstrun.sh` until first boot, which deletes it. `crypt` left the Python
standard library in 3.13 and hashing them properly would mean a dependency for
one function. If someone can read your SD card before its first boot, you have
a bigger problem — but now you know.

**Ethernet hands the network over.** Stage 2 drops the NetworkManager profile
and disables NM so `ifupdown2` owns `vmbr0` alone. Two daemons fighting over
one interface is exactly the 3am page this tool exists to prevent.

**The repo is a third-party mirror.** `global.mirrors.apqa.cn` — straight from
the guide. Proxmox doesn't ship an official ARM64 build. This is Pimox; that's
the deal you're taking.

## Requirements

Python 3.10+ with Tk, which is every python.org install and every Windows
install. `pip install` nothing. The whole thing is one file and the standard
library.

```
python pimox_builder.py --selftest
```

That runs the generator against a fake boot partition and asserts the output —
including that re-running Build twice doesn't stack duplicate kernel arguments,
which it did, once, until the test said so.

## Targets

Raspberry Pi 4 and 5, Raspberry Pi OS Lite (Bookworm, 64-bit). Older Pis are
32-bit or ARMv7; Pimox needs arm64.

The generator is covered by `--selftest`. The generated scripts follow the
guide step for step, but they haven't been through a full hardware run yet —
so keep a monitor on the Pi for the first one, and read the two logs.
