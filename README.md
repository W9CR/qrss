# QBS - Quantar Base Station Programmer

A cross-platform (macOS, Linux, Windows) replacement for Motorola's WinRSS, for
**conventional** Quantar and Quantro stations, and for ASTRO-TAC comparators.  It reads and
writes the station's codeplug, runs the service screens over the front-panel serial port, and
downloads SCM firmware - without WinRSS, Windows, or a serial-port driver hack.

The Python package and repository are called `qrss`; the application calls itself **QBS**
(that is the window title and the name on the installers).

* **Codeplug editor** - every screen of WinRSS' Conventional Configuration, rebuilt from its
  own metadata: channel table, access-code/Multi-NAC tables, wildcard (PL/DPL decode, MDC),
  TRC and DC command tables.  Fields that are not decoded yet are shown greyed out.
  Conventional Quantar codeplugs (v0x28) and comparator codeplugs (v0x61/v0x62).
* **Codeplug report** - WinRSS' report, with its own section titles and order, written to a
  text file or a printer; diffable, and needs no station.
* **Service screens over the serial port** - software version, status panel, alarm log,
  metering (exciter/PA, receivers, power supply, UHSO, wireline), test and measurement
  (ASTRO BER/RSSI, test patterns), alignment, battery equalization, dial-up and terminal.
* **Station backup and restore** - codeplug, wildcard block and alignment partition in one
  `.qbk` file; the recovery path after an EEPROM erase or a bad write.
* **Firmware download** - programs SCM firmware from a Motorola SWDL package over the serial
  port or the station's Ethernet, using its own SLIP/IP/TCP/FTP stack, so it needs no network
  setup or administrator rights.
* **A simulator** - `--sim` connects the whole GUI to a virtual station, for trying the
  screens without hardware.

Only conventional operation is implemented; trunked SmartZone/OmniLink stations are not.

## Install

Download the package for your platform from the [releases page]:

| platform | file |
|---|---|
| macOS (Apple Silicon) | `QBS-<version>-macos-arm64.dmg` - open it, drag QBS to Applications |
| Windows (64-bit) | `QBS-<version>-win64-setup.exe` |
| Linux (x86_64) | `QBS-<version>-x86_64.AppImage`, `.deb`, or the `.tar.gz` |

The builds are not signed with a commercial certificate.  On macOS, the first launch needs
**right-click > Open** (or `System Settings > Privacy & Security > Open Anyway`); on Windows,
SmartScreen shows "More info > Run anyway".  The AppImage needs `chmod +x` once.

[releases page]: https://github.com/W9CR/qrss/releases

### From source

```
pip3 install -r requirements.txt
python3 -m qrss [codeplug.cp]        # or: python3 -m qrss --sim
```

Python 3.9 or later, PySide6 and pyserial; on macOS `pip` also installs pyobjc for the Dock
icon.  `pip install .` gives a `qbs` command on the PATH.

## Using it

`File > New` starts from a blank conventional codeplug, `File > Open` takes a `.cp` archive
written by WinRSS or by QBS, and `Station > Read Codeplug` reads one from a connected station.
An archive from an older RSS (codeplug version 0x25-0x27) is upgraded to 0x28 when opened.

`Station > Connect...` opens the front-panel serial port (8N1, 9600 baud, like WinRSS) or a
modem/dial-up link.  The service screens appear under **Service (station)**.  The connect
dialog also has an **MMI speed** setting for stations running H026 ham firmware with
`PCTL -BAUD`; the port is put back to 9600 on disconnect.

## Safety

This program talks to a real transmitter and programs real flash.

* Anything that keys the transmitter - metering KEY UP, alignment tabs, the ASTRO test
  patterns - needs a dummy load on the TX port.
* Do not interrupt power or the serial link while firmware is downloading.
* Do not write a codeplug to a station unless you have a backup: `Station > Back Up Station...`
  first.
* On macOS/Linux, a station left at a fast MMI baud rate cannot be reached by WinRSS until it
  is reset.  QBS restores 9600 on disconnect and on exit; close it cleanly.

## Building the installers

`release/` builds a self-contained bundle with PyInstaller, then wraps it for each platform.
Each script needs Python 3.9+ with `pip install -r requirements.txt pyinstaller`:

```
release/build-macos.sh      # release/out/QBS-<version>-macos-<arch>.dmg
release/build-linux.sh      # release/out/ .AppImage, .deb and .tar.gz
release/build-windows.ps1   # release/out/QBS-<version>-win64-setup.exe and .zip
```

PyInstaller cannot cross-compile: each package is built on its own OS.  GitHub Actions does
all three when a `v*` tag is pushed (`.github/workflows/release.yml`), and attaches the
results to the release.  The version comes from `qrss/__init__.py` (YY.MM.DD + stage, e.g.
`26.11.09a1`); bump it there only.  Build details and troubleshooting (including a macOS
15.5 `hdiutil` kernel panic) are in `release/README.md`.

## Licence

GNU Affero General Public License v3.0 or later; see `LICENSE`.
