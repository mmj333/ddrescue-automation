# ddrescue-automation

Filesystem-aware automation around [GNU ddrescue](https://www.gnu.org/software/ddrescue/)
for recovering failing drives - recover the **used, important data first**, and
**survive drives that keep disconnecting** mid-recovery.

It parses the filesystem to image only allocated data (not free space),
prioritizes the user's real folders (Desktop / Documents / Pictures / cloud
sync) ahead of the bulk, and rides through source/destination disconnects: if a drive drops out
mid-recovery, it pauses cleanly and waits, then resumes automatically once the
**same** drive returns. It matches the drive by its serial number (when the drive
or its adapter exposes one), so recovery
continues correctly even when the OS brings the drive back at a different
device path (e.g. `/dev/sdc` instead of `/dev/sdb`). It also refuses to write to
a different drive that happens to land on the old path.

---

## 🛑 Before you start - physical risk to a failing drive

**This tool is for recoveries where you have judged the drive to be at low risk of
mechanical / head / platter damage** - e.g. logical corruption, a deleted or
damaged partition, a controller/firmware fault, or a drive that still reads but
has bad sectors.

**Powering on and running a failing drive - HDD *or* SSD - for any length of time
can make your data permanently unrecoverable.** On a hard drive, a failing head
can crash into and score the platters, destroying data that was previously
recoverable; on any drive, continued operation can accelerate the failure. Every
additional minute of runtime is a gamble.

**If your data is highly critical and you are not willing to accept ANY risk of
losing it, do not run this tool - or any DIY recovery tool.** Power the drive
down, leave it unplugged, and send it to a professional data-recovery service that
has a **clean room** and proper handling procedures for failing drives. A failed
DIY attempt can make a later professional recovery harder, more expensive, or
impossible.

**Stop immediately and consult a professional** if the drive makes clicking,
buzzing, grinding, or beeping noises; fails to spin up; gets unusually hot; or
repeatedly disconnects under read errors.

---

## ⚠️ Status: beta - no warranty

**Beta software, under active development, tested only minimally** (a handful of
real recoveries). It may contain bugs, and it comes with **absolutely no warranty
and no guarantees of any kind.** Data recovery is inherently risky - you use this
**entirely at your own risk**; the authors and contributors are not liable for
data loss, drive damage, or any other harm.

- Treat the failing drive as a **read-only source** and recover **to a separate,
  healthy destination** - never the reverse. Double-check which device is which.
- A failing drive can die at any moment. If the data is irreplaceable, consider a
  **professional recovery service** before DIY attempts.
- **Verify** your recovered data before wiping or returning anything.

---

## Supported filesystems

| Filesystem | Capability |
|------------|------------|
| **NTFS**   | Full targeted recovery. Parses `$MFT` + `$Bitmap`, uses ddrescue to clone **only the allocated clusters** (skipping free space), and recovers the user's folders (Desktop / Documents / Pictures / cloud-sync) **first**. The flagship. |
| **HFS+**   | Targeted recovery. Parses the Volume Header + Allocation File (the HFS+ bitmap), then uses ddrescue to clone **only the allocated blocks**. User-folder prioritization is planned (see [Roadmap](#roadmap)). |
| **FAT (12/16/32)** | Targeted recovery. Starts by using ddrescue to clone **only the known allocated areas** of the drive: it reads the FAT (which doubles as the allocation map) to list the allocated clusters, then images those. (FAT12 is implemented but untested.) |
| **exFAT**  | Not yet supported / untested. exFAT tracks allocation in a dedicated Allocation Bitmap (unlike FAT12/16/32), so use the full-drive clone path for now. On the [roadmap](#roadmap). |
| **APFS**   | **Mount-only** (minimal). With the optional, proprietary [Paragon "APFS for Linux"](https://www.paragon-software.com/) driver you can mount a recovered/connected APFS volume to copy files off it. *No APFS-aware targeted recovery*; for a failing APFS drive, do a plain `ddrescue` clone first, then mount. |

## What it does

For an NTFS source, the pipeline runs roughly:

1. Recover the **boot sector** and **MFT** (plus mirror).
2. Parse `$Bitmap` to map **allocated clusters**, and build a ddrescue domain so
   only used data is imaged (skipping free space - often a huge time saver on a
   dying drive).
3. **Priority pass** - parse the MFT to find the user's folders and recover
   Desktop / Documents / Pictures / cloud-sync **first**, before the rest.
4. Full allocated-data sweep, then **aggressive retry** on remaining bad sectors.
5. Optional **full clone** of the remaining (free/unknown) areas.

### Drive-disconnect resilience

Failing USB/SATA drives often drop off the bus mid-recovery. In the **NTFS**
engine, every ddrescue run is wrapped so that on a disconnect it:

- **stops ddrescue cleanly** (SIGINT/SIGTERM - never SIGKILL, so the mapfile is
  never truncated mid-write),
- **waits** for the drive to come back (indefinitely; Ctrl-C to abort),
- **verifies by serial** that it's the *same physical drive* - even if it
  re-enumerated to a different `/dev` node - and never writes to an unverified
  device,
- then **resumes** from the mapfile automatically.

The same serial-based relocation applies when resuming a job whose drive moved
nodes between runs.

**Scope:** this currently lives in the **NTFS** engine only. The HFS+ and FAT
engines still use plain ddrescue (resumable from the mapfile, but without the
automatic clean-stop / wait / serial-reconnect). Porting it to them is on the
[roadmap](#roadmap).

> **Known limitation:** matching relies on the drive (or its USB adapter)
> exposing a serial number. Some cheap bridges report no serial, or a blank or
> duplicate one; in that case relocation to a new device node and same-drive
> verification are limited, and it falls back to matching by path. Hardening this
> (model + size + partition signature, with a prompt on any ambiguous match) is on
> the [roadmap](#roadmap).

## Quick start

```bash
# 1. Install dependencies (apt tools + RecuperaBit; see DEPENDENCIES.md)
./install-dependencies.sh

# 2. Run the interactive recovery manager
sudo ./recovery-manager.sh
```

Or drive the NTFS engine directly:

```bash
sudo python3 scripts/iterative-targeted-recovery.py \
    --bootable-first /dev/sdX /dev/sdY recovery.log ./job_example
#                    ^source   ^dest    ^mapfile      ^job dir
```

## Dependencies

See **[DEPENDENCIES.md](DEPENDENCIES.md)** for the full list and the **tested
versions** (GNU ddrescue 1.27, The Sleuth Kit 4.12.1, expect 5.45.4, RecuperaBit
v1.1.6, Python 3.12 - standard library only). `./install-dependencies.sh`
installs the apt-available pieces and clones RecuperaBit at its pinned release.
APFS support is minimal, optional, and requires a proprietary vendor driver
(see DEPENDENCIES.md).

## How this compares to existing tools

This isn't the first NTFS-aware ddrescue helper, and it builds on ideas from
prior art - credit where due:

- **[ddrutility](https://sourceforge.net/projects/ddrutility/)** (`ddru_ntfsbitmap`)
  pioneered building a ddrescue domain from the NTFS `$Bitmap` to image only
  allocated clusters.
- **[ddrescue-loop](https://github.com/gumanzoy/ddrescue-loop)** restarts
  ddrescue across disconnects using hardware power-cycling and VID:PID matching.
- **[HDDSuperClone / OpenSuperClone](https://www.hddsuperclone.com/)** are
  heavier, ATA-passthrough imagers with head-mapping and USB-relay control.

What this project adds on top: an **all-in-one wizard** that **prioritizes the
user's actual folders first** (NTFS), plus **software-only disconnect
resilience** - it pauses cleanly on a drop and re-attaches to the drive by serial
number, with no extra hardware to buy or wire up.

Honest trade-offs, though: unlike the relay-based tools above, it **cannot
power-cycle a drive that has stopped responding entirely** - if a drive needs a
hard reset to come back, you'd want HDDSuperClone/OpenSuperClone or a USB relay.
And the serial verification only holds **when the drive exposes a serial**; if it
doesn't, it falls back to matching by device path, where a different drive landing
on the old path could in principle be written to (see the Known limitation above).
So "won't touch the wrong drive" is the design intent and the common case, not a
hard guarantee.

## Roadmap

- Pre-recovery **HTML map**: parse the MFT before recovering, show the folders
  available, let you **select/prioritize** what to recover and build a recovery
  plan executed in that order.
- Populate the visualizer's **file-level map** for non-NTFS filesystems: extract
  the HFS+ Catalog File to `catalog.raw` (the visualizer already supports it), and
  add FAT directory parsing plus a `parse_fat` module (net-new). NTFS already does
  this via `mft.raw`.
- Fix MFT **file-size** reading: `entry.size` under-reads real file sizes, so the
  visualizer's file tree shows sizes smaller than reality (recovery *status* and
  the overall percentages are unaffected).
- Bring **HFS+** (and eventually FAT) up to the NTFS feature set: user-folder
  prioritization via the HFS+ Catalog B-tree, etc.
- Add an **exFAT** engine: parse the exFAT Allocation Bitmap (same bitmap-driven
  pattern as the NTFS/HFS+ engines). Until then, exFAT drives use the full clone.
- Port the **drive-disconnect resilience** (clean-stop + serial-verified
  reconnect) from the NTFS engine to the HFS+ and FAT engines.
- More robust **drive matching** when a serial number is missing or non-unique:
  fall back to model + size + partition signature, and prompt on an ambiguous
  match instead of guessing.
- Manual destination override (re-point a job to a moved destination image).

## License

GPLv3 (see `LICENSE`). As required by the license, this program is distributed
**WITHOUT ANY WARRANTY**; see the beta notice above.
