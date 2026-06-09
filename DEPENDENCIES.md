# Dependencies

This toolset orchestrates several external programs. Below is what it needs,
the versions it has actually been tested against, and how to install them.

Run `./install-dependencies.sh` to install the apt-available pieces and clone
RecuperaBit automatically. The proprietary APFS driver must be obtained from the
vendor (see below).

## Tested environment

The tool is developed and run on:

| Component        | Tested version            |
|------------------|---------------------------|
| OS               | Linux Mint 22 (Ubuntu 24.04 base) |
| Kernel           | 6.8.x                     |
| Python           | 3.12.3 (standard library only — no pip packages) |

Other distros/versions will likely work, but the versions below are the ones
this has been exercised with. If you hit a behavioral difference, tool version
is the first thing to check — recovery utilities change defaults between releases.

## Required

| Tool            | Package (apt)   | Tested version          | Used for |
|-----------------|-----------------|-------------------------|----------|
| GNU ddrescue    | `gddrescue`     | **1.27**                | The core imaging engine. Behavior/flags are version-sensitive — 1.27 is the baseline. |
| The Sleuth Kit  | `sleuthkit`     | **4.12.1**              | `icat`/`istat`/`fls`/`fsstat` — extract the MFT and `$Bitmap`, locate allocated clusters. |
| expect          | `expect`        | **5.45.4**              | Drives the interactive `.exp` automation scripts. |
| util-linux      | (preinstalled)  | distro default          | `blockdev`, `losetup`, `partprobe`. |
| udev            | (preinstalled)  | distro default          | `udevadm` for device identity (serial/model) and auto-run rules. |

## Optional (filesystem- or workflow-specific)

| Tool            | Source / package                              | Tested version | Used for |
|-----------------|-----------------------------------------------|----------------|----------|
| RecuperaBit     | `github.com/Lazza/RecuperaBit` (pin `v1.1.6`) | **v1.1.6**     | Reconstructing NTFS partitions/trees when the partition table or boot sector is gone. |
| rsync-recovery (companion) | `github.com/mmj333/rsync-recovery` (`v1.9.3`) | **v1.9.3** | Optional companion: a partition-analyzer source/destination picker and a "smart rsync" file-copy recovery workflow. The tool falls back to manual device selection without it. |
| ntfs-3g / ntfsprogs | `ntfs-3g`                                 | any recent     | `ntfsinfo` and mounting recovered NTFS images. |
| TestDisk / PhotoRec | `testdisk`                                | any recent     | Deeper partition repair and signature-based carving. |
| Paragon APFS for Linux (`uapfs`) | **proprietary — see below**  | —              | Only if you want to work with APFS volumes. Not needed otherwise. |

### APFS support is optional (and is mount-only)

Be aware of the scope: this toolset has **targeted-recovery engines for NTFS,
HFS+, and FAT**. For **APFS it provides mounting only** — you can mount a
recovered or connected APFS volume to browse and copy files off it, but there is
**no APFS-aware targeted recovery**. For a failing APFS drive, do a full/plain
`ddrescue` clone of the device first, then mount the resulting image to pull
files.

So you only need the APFS driver if you actually want to mount an APFS volume.
NTFS, HFS+, and FAT recovery — and everything else in this toolset — work fully
without it.

**If you want to work with APFS, then:** the proprietary APFS driver must be
obtained from the vendor. APFS support uses Paragon Software's **"APFS for
Linux"** driver, which provides the `uapfs` kernel module / mount helper. It is
**proprietary and is not included in this repository** — download and install it
yourself from Paragon Software:

> Paragon Software — "APFS for Linux": <https://www.paragon-software.com/>
> (search their site for "APFS for Linux"; verify the current download URL and
> license terms.)

If `uapfs` is not installed, only APFS mounting is unavailable; everything else
still works, and the tool will tell you so and point you here.

### rsync-recovery companion (optional)

`recovery-manager.sh` can integrate an optional companion project,
**[rsync-recovery](https://github.com/mmj333/rsync-recovery)**, for two things: a
partition-analyzer-based source/destination picker, and a "smart rsync"
file-copy recovery workflow (used after mounting a recovered volume).

It is **not required**. If it isn't present at `$HOME/Projects/rsync-recovery`
(override with `RSYNC_RECOVERY_DIR=...`), the tool falls back to manual device
selection via `lsblk` and the rsync "smart recovery" option is simply
unavailable — nothing breaks.

Install (optional):

```bash
git clone https://github.com/mmj333/rsync-recovery "$HOME/Projects/rsync-recovery"
# or point RSYNC_RECOVERY_DIR at an existing checkout
```

## Install

Automated (recommended):

```bash
./install-dependencies.sh
```

Manual:

```bash
# Required
sudo apt update
sudo apt install -y gddrescue sleuthkit expect

# Optional
sudo apt install -y ntfs-3g testdisk

# RecuperaBit (pinned to the tested release)
git clone https://github.com/Lazza/RecuperaBit "$HOME/RecuperaBit"
git -C "$HOME/RecuperaBit" checkout v1.1.6
# Override the location with: export RECUPERABIT_DIR=/path/to/RecuperaBit

# APFS: install Paragon "APFS for Linux" from the vendor (see above) — not scriptable.
```
