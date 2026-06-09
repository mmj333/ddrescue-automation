#!/bin/bash
# install-dependencies.sh — install the external tools this toolset relies on.
# Installs the apt-available pieces and clones RecuperaBit at its tested release.
# The proprietary Paragon APFS driver is NOT installed here — see DEPENDENCIES.md.
#
# Safe to re-run: apt skips already-installed packages and the RecuperaBit clone
# is skipped if the directory already exists.

set -u

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}==>${NC} $*"; }
warn()  { echo -e "${YELLOW}!  ${NC} $*"; }
err()   { echo -e "${RED}x  ${NC} $*"; }

# Tested-against versions (informational; see DEPENDENCIES.md)
TESTED_DDRESCUE="1.27"
TESTED_TSK="4.12.1"
TESTED_EXPECT="5.45.4"
RECUPERABIT_TAG="v1.1.6"
RECUPERABIT_DIR="${RECUPERABIT_DIR:-$HOME/RecuperaBit}"

if ! command -v apt-get >/dev/null 2>&1; then
    err "This installer targets Debian/Ubuntu (apt). On other distros, install the"
    err "equivalents listed in DEPENDENCIES.md by hand."
    exit 1
fi

echo
info "This will install the recovery dependencies via apt (sudo required):"
echo "    required: gddrescue (ddrescue), sleuthkit, expect"
echo "    optional: ntfs-3g, testdisk (photorec)"
echo "    RecuperaBit ${RECUPERABIT_TAG} -> ${RECUPERABIT_DIR}"
echo
read -p "Proceed? [Y/n]: " ans
case "${ans:-y}" in [nN]*) echo "Aborted."; exit 0 ;; esac

# --- Required -------------------------------------------------------------
info "Installing required packages..."
sudo apt-get update
sudo apt-get install -y gddrescue sleuthkit expect || { err "apt install failed"; exit 1; }

# --- Optional -------------------------------------------------------------
read -p "Also install optional tools (ntfs-3g, testdisk)? [Y/n]: " ans
case "${ans:-y}" in
    [nN]*) warn "Skipping optional tools." ;;
    *)     sudo apt-get install -y ntfs-3g testdisk || warn "optional install had problems (non-fatal)" ;;
esac

# --- RecuperaBit ----------------------------------------------------------
if [ -d "$RECUPERABIT_DIR" ]; then
    warn "RecuperaBit already present at $RECUPERABIT_DIR — leaving as-is."
else
    read -p "Clone RecuperaBit ${RECUPERABIT_TAG} to ${RECUPERABIT_DIR}? [Y/n]: " ans
    case "${ans:-y}" in
        [nN]*) warn "Skipping RecuperaBit (NTFS partition reconstruction will be unavailable)." ;;
        *)
            if command -v git >/dev/null 2>&1; then
                git clone https://github.com/Lazza/RecuperaBit "$RECUPERABIT_DIR" \
                    && git -C "$RECUPERABIT_DIR" checkout "$RECUPERABIT_TAG" \
                    && info "RecuperaBit ${RECUPERABIT_TAG} installed at $RECUPERABIT_DIR" \
                    || warn "RecuperaBit clone/checkout failed — install it manually (see DEPENDENCIES.md)."
            else
                warn "git not found; install git, then clone RecuperaBit manually."
            fi
            ;;
    esac
fi

# --- APFS (proprietary, not scriptable) -----------------------------------
echo
info "APFS support is OPTIONAL — only needed if you want to work with APFS volumes."
warn "If you want APFS: install Paragon Software's proprietary 'APFS for Linux' driver"
warn "(provides the 'uapfs' module) from the vendor. It is NOT bundled / not auto-installable:"
warn "    https://www.paragon-software.com/   (search 'APFS for Linux')"
warn "Everything else (NTFS, HFS+, FAT, ...) works without it."

# --- Report ---------------------------------------------------------------
echo
info "Installed versions (tested baseline in parentheses):"
printf "    ddrescue: %s  (tested %s)\n" "$(ddrescue --version 2>/dev/null | head -1 | grep -oE '[0-9.]+' | head -1 || echo missing)" "$TESTED_DDRESCUE"
printf "    sleuthkit: %s  (tested %s)\n" "$(icat -V 2>/dev/null | grep -oE '[0-9.]+' | head -1 || echo missing)" "$TESTED_TSK"
printf "    expect: %s  (tested %s)\n" "$(expect -v 2>/dev/null | grep -oE '[0-9.]+' | head -1 || echo missing)" "$TESTED_EXPECT"
echo
info "Done. See DEPENDENCIES.md for details and the APFS driver."
