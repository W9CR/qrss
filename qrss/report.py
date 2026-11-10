"""The codeplug report: a text dump of every codeplug parameter and its setting.

WinRSS' File > Print Codeplug Report and File > Save Codeplug Report (RSS manual 5-31..5-33,
Figure 5-8).  The report is produced by native code in rsscore.dll, not by the Java front end,
so its layout is not in the decompiled sources.  What is recoverable is in the DLL's strings:
the section titles (STATION CONFIGURATION, WIRELINE CONFIGURATION, ...), the row labels in
the order the report prints them (the compiler emits them in reverse use order), and the
title/time line.  SECTIONS below is that list, matched row by row to the form fields in
qrss/data/forms.json.  The values come from the same codecs the screens use, so a report
always agrees with what the GUI shows.

Not reproduced from the DLL: rows for fields qrss has no codec for print "(not decoded)"
(WinRSS prints the value it holds); a row the DLL has no label for takes the form's own
label; and the WildCard section is a readable table dump rather than WinRSS' fixed layout.
"""
import datetime
import json
import os
import sys

from .codeplug import Codeplug, CpFile, cksum, trc_list
from .fields import CODECS, codec, detect_system_type
from .wildcard import Wildcard, io_group

DATA = os.path.join(os.path.dirname(__file__), 'data')
FORMS = json.load(open(os.path.join(DATA, 'forms.json')))
MENUS = json.load(open(os.path.join(DATA, 'menus.json')))

# same table app.py uses to pick the menu tree for a loaded codeplug
SYSTEM_FOR_VERSION = {0x28: 'CONVENTIONAL', 0x61: 'COMPARATOR', 0x62: 'COMPARATOR'}

TITLE = "BASE STATION PRODUCTS Codeplug Report"
LABEL_W, VALUE_W = 26, 8         # WinRSS' columns: label, value, unit

# Report rows per form: (label, field[, unit]).  A missing unit means the form's own unit;
# "" suppresses it (e.g. the band fields print "800     806-825" with no unit).  A form not
# listed here is printed from forms.json in form order with its labels uppercased.
SECTIONS = {
    'DSTCG': [                                   # "STATION CONFIGURATION"
        ("SYSTEM TYPE", "system_type"),
        ("SERIAL NUMBER", "serial_number"),
        ("STATION NAME", "station_name"),
        ("HARDWARE PLATFORM", "hw_platform"),
        ("RX FREQUENCY BAND 1", "rx_freq_band", ""),
        ("RX FREQUENCY BAND 2", "rx_freq_band2", ""),
        ("PA/EXCITER FREQ. BAND", "pa_freq_band", ""),
        ("PA POWER RATING", "pa_rated", ""),
        ("WIRELINE", "wireline"),
        ("POWER SUPPLY", "power_supply"),
        ("BATTERY TYPE", "battery_type"),
        ("STATION TYPE", "modulation"),
        ("INTELLIREPEATER FREQ", "ir_freq_band"),
        ("FREQUENCY REFERENCE", "ref_selection"),
        ("SIMULCAST", "simulcast_oper"),
        ("MULTI-CODED SQUELCH", "multi_coded_sq"),
        ("MRTI INTERFACE", "mrti_interface"),
        ("SITE NUMBER", "site_number"),
        ("SCANNING RECEIVER", "scan_rcvr_opt"),
        ("MAIN/STANDBY", "main_standby"),
    ],
    'DWIREL': [                                  # "WIRELINE CONFIGURATION"
        ("WIRELINE OPERATION", "wireline_oper"),
        ("CONSOLE PRIORITY(6809)", "con_priority"),
        ("REMOTE CONTROL TYPE", "remote_control"),
        ("  TRC INPUT", "trc_input"),
        ("OUTBND ANALOG LNK TIMER ", "out_link_timer"),
        ("COMPARATOR", "astro_spectra"),
        ("  FALL BACK IN-CABINET", "fallback_cabin"),
        ("  FALL BACK TIME", "fallback_timer"),
        ("STATUS TONE", "status_tone"),
        ("  STATUS TONE FREQ", "status_tone_frq"),
        ("WIRELINE SQUELCH", "wl_squelch"),
        ("  SQUELCH THRESHOLD    -", "wlsq_threshold"),
        ("  SQUELCH HYSTERESIS", "wlsq_hysteresis"),
        ("RX SECURENET/ASTRO TO WL", "rx_secure_to_wl"),
        ("EQUALIZATION", "equalization"),
        ("TRC:   HLGT FREQUENCY ", "hlgt_frequency", "Hz"),
        ("AUTO LINE CTRL", "auto_level_ctrl"),
        ("       TX NOTCH FILT", "wl_tx_notch"),
        ("       RX NOTCH FILT", "wl_rx_notch"),
        ("       LLGT UNDETECT", "llgt_undetect", "msec"),
        ("       TRC DETECT ALG", "trc_detection_algorithm"),
        ("       TRC FT DETECTED", "function_tones_detected"),
        ("ASTRO: WL INTERFACE", "astro_wl_interf"),
        ("       ANALOG LNK CHK", "alm"),
        ("       DIGITAL LNK CHK", "dlm"),
        ("       EXT XMIT CLOCK", "ext_tx_clock"),
        ("       MODEM RX LEVEL", "modem_rx_level"),
        ("       MODEM TX LEVEL", "modem_tx_level"),
        ("       RT/RT CONFIG", "rt_rt_config"),
    ],
    'DACC': [                                    # "ACCESS CODE TABLE INFORMATION"
        ("ACCESS TABLE #", "access_table"),
        ("RX SQUELCH TYPE", "rx_squelch_type"),
        ("RX PL/DPL CODE", "rx_pl_dpl_1"),
        ("TX SQUELCH TYPE", "tx_squelch_type"),
        ("TX PL/DPL CODE", "tx_pl_dpl_1"),
        ("ASTRO Carrier Squelch", "astro_car_squ"),
        ("RX ASTRO ACCESS CODE", "rx_codeword"),
        ("TX ASTRO ACCESS CODE", "tx_codeword"),
        ("Transmit Using Last NAC", "trsmt_last_nac"),
        ("Expiration Time", "exp_time"),
        ("RX NETWORK ACCESS CODE 1", "rx_nac1"),
        ("TX NETWORK ACCESS CODE 1", "tx_nac1"),
        ("RX NETWORK ACCESS CODE 2", "rx_nac2"),
        ("TX NETWORK ACCESS CODE 2", "tx_nac2"),
        ("RX NETWORK ACCESS CODE 3", "rx_nac3"),
        ("TX NETWORK ACCESS CODE 3", "tx_nac3"),
        ("RX NETWORK ACCESS CODE 4", "rx_nac4"),
        ("TX NETWORK ACCESS CODE 4", "tx_nac4"),
        ("RX NETWORK ACCESS CODE 5", "rx_nac5"),
        ("TX NETWORK ACCESS CODE 5", "tx_nac5"),
        ("RX NETWORK ACCESS CODE 6", "rx_nac6"),
        ("TX NETWORK ACCESS CODE 6", "tx_nac6"),
        ("RX NETWORK ACCESS CODE 7", "rx_nac7"),
        ("TX NETWORK ACCESS CODE 7", "tx_nac7"),
        ("RX NETWORK ACCESS CODE 8", "rx_nac8"),
        ("TX NETWORK ACCESS CODE 8", "tx_nac8"),
    ],
    'DCHN': [                                    # "CHANNEL INFORMATION", one block per channel
        ("CHANNEL NUMBER", "channel"),
        ("RX1 FREQUENCY", "rx_freq"),
        ("RX2 FREQUENCY", "rx2_freq"),
        ("TX FREQUENCY", "tx_freq"),
        ("TX IDLE FREQ.", "tx_idle_freq"),
        ("MODULATION TYPE", "mod_type"),
        ("TX RATED DEVIATION", "rated_dev"),
        ("RECEIVE CHANNEL BW", "chn_spacing", "kHz"),
        ("CALL SIGN", "callsign"),
        ("   CALLSIGN OVER WL", "callsign_wl"),
        ("ACCESS CODE TABLE", "acc_tbl_num"),
        ("CARRIER SQL TRANSITION", "carrier_sql_trs"),
        ("ANALOG RX ACTIVATION", "rx_active"),
        ("ANALOG RPTR ACTIVATION", "rptr_active"),
        ("ANALOG RPTR HOLDIN", "rptr_holdin"),
        ("ANALOG RPTR ACCESS", "rf_repeater_acc"),
        ("TX POWER OUT", "tx_pwr_out"),
        ("TX POWER OUT/BATTERY", "tx_pwr_out_batt"),
        ("WIRELINE TOT", "line_tot"),
        ("LOCAL TOT", "local_tot"),
        ("RPTR TOT", "rptr_tot"),
        ("MRTI TOT", "mrti_tot"),
        ("ALARM OVER AIR", "alarm_air"),
        ("ALARM TONE OVER WL", "alarm_wireline"),
        ("PRE-EMPHASIS", "pre_emphasis"),
        ("DE-EMPHASIS", "de_emphasis"),
        ("HI-PASS FILTER", "hi_pass_filter"),
        ("WL CSQ MUTE TIMER", "csq_delay_time"),
        ("RPTR DOD", "rptr_dod"),
        ("RPTR AUDIO HOLD-OFF", "rptr_holdoff"),
        ("COMPANDER", "compander"),
        ("NOISE CANCELLER", "noise_canceller"),
        ("MRTI OPERATION", "mrti_operation"),
        ("MONITOR BEFORE TRANSMIT", "monb4xmit"),
        ("ILLEGAL CARRIER SIGNAL LEVEL -", "ilgl_car_siglvl"),
        ("RSTAT MODE", "brstat"),
        ("RCV SIGNAL INVERSION", "data_polarity"),
        ("PTT PRIORITY", "ptt_priority"),
        ("ANALOG RPTR BOOST", "repeater_boost"),
    ],
    'DRFCFG': [                                  # "RF CONFIGURATION"
        ("REPEATER OPERATION", "repeater_oper"),
        ("MAX DEVIATION", "max_deviation"),
        ("LOW SPEED DEVIATION", "low_speed_data"),
        ("ANTENNA RELAY", "antenna_relay"),
        ("ANTENNA RELAY DELAY", "ant_relay_delay"),
        ("CALL SIGN INTERVAL", "call_sign_inter"),
        ("STRTP LAST ACTIVE CHAN", "last_active_chn"),
        ("STARTUP CHANNEL", "startup_chn"),
        ("ASTRO FADE TOLERANCE", "fade_tolerance"),
        ("ASTRO RDLAP: REPEAT", "rdlap_repeat"),
        ("   WIRELINE DOD", "rdlap_wl_dod"),
        ("ASTRO SIMUL TX FILTER", "simul_tx_filter"),
        ("ANALOG SIMUL REV BUR", "simulcast_pl_rb"),
        ("   RX DET FADE TIMER", "rx_fade_timer"),
        ("   FADE EOM TIMER", "fade_eom_timer"),
        ("FAST KEY-UP", "fast_keyup"),
        ("EXTENDED SQUELCH", "enhanced_squelch"),
    ],
    'DSCANL': [                                  # "SCAN LIST CONFIGURATION"
        ("SCAN ON AT RESET  ", "scan_at_reset"),
        ("SCAN DELAY        ", "scan_delay"),
        ("PRIORITY CHANNEL  ", "priority_chnl"),
        ("PRIORITY SCAN DELAY", "pri_scan_delay"),
        ("FLOATING PRIORITY ", "float_priority"),
        ("SCAN SAMPLE TIME  ", "scan_sample"),
        ("CHANNEL MARKING   ", "channel_marking"),
        ("RX QUALIFY TIME   ", "rx_qualify_tm"),
        ("RESUME SCAN TIMER ", "resume_scan_tm"),
    ],
    'DMCS': [                                    # "MULTI CODED SQUELCH INFORMATION"
        ("MCS ENTRY #", "entry_no"),
        ("ACCESS MODE", "rx_squelch_type"),
        ("RX FREQ/CODE", "rx_code"),
        ("TX FREQ/CODE", "tx_code"),
        ("USER ACCESS", "access"),
    ],
}

# the report's own section titles (rsscore.dll); a form without one takes its menu label
SECTION_TITLES = {
    'DSTCG': 'STATION CONFIGURATION',
    'DWIREL': 'WIRELINE CONFIGURATION',
    'DACC': 'ACCESS CODE TABLE INFORMATION',
    'DMCS': 'MULTI CODED SQUELCH INFORMATION',
    'DCHN': 'CHANNEL INFORMATION',
    'DTRC': 'TRC CONFIGURATION',
    'DDCRC': 'DC CONFIGURATION',
    'DRFCFG': 'RF CONFIGURATION',
    'DSCANL': 'SCAN LIST CONFIGURATION',
}

# forms printed once per record (a channel, a port); everything else once per codeplug
PER_RECORD = {'DCHN': 'channel', 'DPORTCTL': 'row'}

# the WildCard forms are edited by wc_pages.py from the wildcard block, not from form fields
WILDCARD_FORMS = {'DWCINPUT', 'DWCOUTPUT', 'DWCSTATE'}

# TRC / DC command lists: list 0 is the guard tone, list N the function tone N (2050 Hz down to
# 450 Hz in 100 Hz steps).  The DC form is the same table: its twelve rows are WinRSS' six
# currents (DDCRCConfigWindow: +12.5, +5.5, +2.5, -2.5, -5.5, -12.5 mA) each with a Detect
# and an Undetect set, in that order, so DC row N uses list N-1 and the last six lists are
# unused by either form (codeplug.py, "TRC / DC command lists").
TRC_LABELS = ["GUARD TONE"] + ["FT%d - %d Hz" % (k, 2150 - 100 * k) for k in range(1, 18)]
DC_LABELS = ["%s mA %s" % (cur, state) for cur in ("+12.5", "+5.5", "+2.5", "-2.5", "-5.5", "-12.5")
             for state in ("DETECT", "UNDETECT")]


def _form_fields(form):
    return {f.get('fieldName'): f for f in FORMS.get(form, {}).get('fields', [])}


def _rows(form):
    """(label, field, unit) for one form: the report's own list, else the form's."""
    spec = SECTIONS.get(form)
    if spec is not None:
        return [(e[0], e[1], e[2] if len(e) > 2 else _unit(form, e[1])) for e in spec]
    out = []
    for f in FORMS.get(form, {}).get('fields', []):
        n = f.get('fieldName')
        if not n:
            continue
        lab = (f.get('label') or n).strip().rstrip(':').upper()
        out.append((lab, n, _unit(form, n)))
    return out


def _unit(form, field):
    return _form_fields(form).get(field, {}).get('unit') or ''


def _value(cp, form, field, ctx):
    cd = codec(form, field)
    if cd is None:
        return "(not decoded)"
    try:
        return cd.get(cp, ctx)
    except (IndexError, KeyError):
        return "(n/a)"


def _line(label, value, unit=''):
    lw = max(LABEL_W, len(label) + 2)
    s = label.ljust(lw) + str(value)
    if unit:
        s = s.ljust(lw + max(VALUE_W, len(str(value)) + 2)) + unit
    return s.rstrip()


def _heading(text):
    return "  " + text


def _dacc_view(cp):
    """A codeplug to print the ACCESS CODE TABLE from.  In MULTI-PL ONLY mode the same records
    are the multi-coded-squelch store, which spells PL/DPL 6xxx/7xxx instead of the DACC form's
    1xxx/2xxx (rsscore FUN_1002e480); translate a copy so the table still reads as PL/DPL."""
    try:
        mode = CODECS[('DSTCG', 'multi_coded_sq')].get(cp, {})
    except Exception:
        mode = None
    if mode != 'MULTI-PL ONLY':
        return cp
    img = bytearray(cp.img)
    for t in range(cp.naccess()):
        for off in (4, 6):
            a = cp.access_off(t) + off
            w = int.from_bytes(img[a:a + 2], 'big')
            t_ = w >> 12
            if t_ in (6, 7):
                img[a:a + 2] = ((t_ - 5) << 12 | (w & 0xFFF)).to_bytes(2, 'big')
    view = Codeplug(img)
    for attr in ('mcs', 'mcs_stamp'):
        if hasattr(cp, attr):
            setattr(view, attr, getattr(cp, attr))
    return view


def _block(cp, form, ctx):
    return [_line(lab, _value(cp, form, fld, ctx), unit) for lab, fld, unit in _rows(form)]


def _region_rows(cp, form, region, ctx):
    """A form's table region (DSCANL's scan list, DMCS's MCS entries) as a grid."""
    meta = [r for r in FORMS.get(form, {}).get('regions', []) if r['regionName'] == region]
    if not meta:
        return []
    r = meta[0]
    cols = [f for f in r.get('fields', []) if f.get('fieldName') != 'dummy']
    heads = [(f.get('label') or f.get('fieldName') or '').strip() for f in cols]
    nrows = cp.naccess() if form == 'DMCS' else r.get('rows', 0)   # one row per access code table
    rows = []
    for row in range(nrows):
        rctx = dict(ctx, row=row)
        cells = [_value(cp, form, "%s.%s" % (region, f.get('fieldName')), rctx) for f in cols]
        if any(c not in ('', '(not decoded)') for c in cells):
            rows.append(cells)
    widths = [max([len(heads[i])] + [len(x[i]) for x in rows]) + 2 for i in range(len(cols))]
    lines = ["", _heading(region.upper().replace('_', ' ')),
             "  " + "".join(h.ljust(w) for h, w in zip(heads, widths)).rstrip()]
    lines += ["  " + "".join(c.ljust(w) for c, w in zip(cells, widths)).rstrip() for cells in rows]
    return lines


def _command_lists(cp, labels):
    """TRC / DC command tables: one line per function tone or DC current."""
    out = []
    failed = 0
    for k, label in enumerate(labels):
        try:
            cmds = trc_list(cp, k)
        except (IndexError, KeyError, ValueError):
            cmds, failed = [], failed + 1
        out.append(_line(label, ", ".join(cmds) or "-"))
    return ["(no command table in this codeplug)"] if failed == len(labels) else out


def _wildcard(wc):
    """The WildCard block as WinRSS' report prints it: three tables under its own headings
    ("WILDCARD INPUT CONFIGURATION", "... OUTPUT ...", "... STATE CONDITION/ACTION ..." -
    the strings sit together in rsscore.dll at 0x98a24..0x98d80, printed by the routine at
    0x1003F170 into 120-character lines).  WinRSS' exact column spacing is not reproduced."""
    w = Wildcard(wc)
    out = ["", _heading("WILDCARD")]
    roles = {0x4: "Channel", 0x8: "MCS", 0xC: "Activation"}
    for title, heading, words, hw, act in (
            ("INPUT", "WILDCARD INPUT CONFIGURATION", w.inputs,
             ("Transistor Input", "Optocoupler Input"), "Active"),
            ("OUTPUT", "WILDCARD OUTPUT CONFIGURATION", w.outputs,
             ("Transistor", "Relay"), "Active Level")):
        out += ["", _heading(heading)]
        if title == "INPUT":
            out.append("  %-12s%-20s%-10s%s" % ("Aux Input", "HW Input Type", "Active",
                                                 "Group Definition"))
        else:
            out.append("  %-12s%-20s%-8s%-8s%s" % ("Aux Output", "HW Output Type", "Active",
                                                    "Level", "Group Definition"))
        for i, word in enumerate(words):
            hi, group, role = io_group(word)
            kind = hw[0] if (i < (8 if title == "INPUT" else 6)) else hw[1]
            g = "%s %d" % (roles.get(role, "Group"), group) if role else "-"
            if title == "INPUT":
                cols = "%-12d%-20s%-10s%s" % (i + 1, kind, "HI" if hi else "LO", g)
            else:
                # the output record's active bit is its polarity; there is no separate level
                cols = "%-12d%-20s%-8s%-8s%s" % (i + 1, kind, "HI" if hi else "LO", "-", g)
            out.append("  " + cols.rstrip())
    if w.tables:
        out += ["", _heading("WILDCARD STATE CONDITION/ACTION CONFIGURATION")]
    for i, t in enumerate(w.tables):
        out += ["", "  TABLE %d  %s" % (i + 1, t.desc), "  State                Cond",
                "  " + _wc_states(t),
                "     ACTION:   " + (", ".join(map(str, t.actions)) or "-"),
                "     INACTION: " + (", ".join(map(str, t.inactions)) or "-")]
    return out


def _wc_states(t):
    """The condition a table tests: its state entries joined by their AND/OR (WinRSS prints
    them in its "State  Cond" columns)."""
    from .wildcard import COND
    parts = []
    for k, s in enumerate(t.states):
        if s is None:
            break
        if k:
            parts.append(COND[t.conds[k - 1]] if t.conds[k - 1] < 4 else "?")
        parts.append(s[0] + ("" if s[1] is None else " %s" % s[1]))
    return " ".join(parts) or "(always)"


def codeplug_report(cpfile, name=None, system=None, when=None):
    """The whole report as text.  `cpfile` is a CpFile (its wildcard section is reported too);
    `name` is what the header calls the file, `system` overrides the menu tree."""
    cp = Codeplug(cpfile.cp)
    system = system or SYSTEM_FOR_VERSION.get(cp.version) or 'CONVENTIONAL'
    when = when or datetime.datetime.now()
    out = [TITLE, when.strftime("%m/%d/%Y,  %I:%M:%S %p")]
    if name:
        out.append("File: %s" % name)
    out.append("Codeplug version 0x%02X, %d bytes, %d directory entries"
               % (cp.version, cp.size, cp.ndir))
    out.append("Checksum %04X (stored %04X)"
               % (cksum(cp.img), int.from_bytes(cp.img[0:2], 'big')))
    if system == 'COMPARATOR':     # no STATION_INFO in a comparator image: hw_platform is a Quantar decode
        out.append("Comparator type: %s" % _value(cp, 'DHWCFG', 'comparator_type', {}))
    else:
        out.append("System type: %s, hardware platform: %s"
                   % (detect_system_type(cp), _value(cp, 'DSTCG', 'hw_platform', {})))
    for entry in MENUS.get(system, []):
        form, title = entry['form'], SECTION_TITLES.get(entry['form'], entry['label'].upper())
        if form == 'DACC' and system != 'CONVENTIONAL':
            title += " (TRUNKING)"
        if form in WILDCARD_FORMS:
            continue                      # printed from the wildcard block, below
        out += ["", _heading(title)]
        key = PER_RECORD.get(form)
        if form in ('DTRC', 'DDCRC'):
            out += _command_lists(cp, TRC_LABELS if form == 'DTRC' else DC_LABELS)
            if form == 'DTRC':               # the form's one other field: the HLGT frequency
                out += [_line("TRC:   HLGT FREQUENCY ", _value(cp, form, 'hlgt_frequency', {}), "Hz")]
        elif key == 'channel':
            for ch in range(cp.nchan()):
                out += [""] + _block(cp, form, {'channel': ch, 'row': 0})
        elif key == 'row':
            for row in range(cp.nports()):
                out += [""] + _block(cp, form, {'channel': 0, 'row': row})
        elif form == 'DACC':                 # one block per access code table
            dacc = _dacc_view(cp)            # MULTI-PL ONLY records carry the store encoding
            for t in range(max(cp.naccess(), 1)):
                out += [""] + _block(dacc, form, {'channel': 0, 'row': 0, 'table': t})
        elif form == 'DMCS':                 # one block per multi-coded squelch entry
            for row in range(max(cp.naccess(), 1)):
                out += [""] + _block(cp, form, {'channel': 0, 'row': row})
        else:
            out += _block(cp, form, {'channel': 0, 'row': 0})
        for r in FORMS.get(form, {}).get('regions', []):
            out += _region_rows(cp, form, r['regionName'], {'channel': 0, 'row': 0})
    if system != 'COMPARATOR':
        if cpfile.wc:
            out += _wildcard(cpfile.wc)
        else:
            out += ["", _heading("WILDCARD"), "No wildcard block in this codeplug."]
    return "\n".join(out) + "\n"


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Write a codeplug report (like WinRSS File > Save Codeplug Report).")
    ap.add_argument('codeplug', help="WinRSS .cp archive")
    ap.add_argument('-o', '--output', help="write here instead of stdout")
    a = ap.parse_args(argv)
    f = CpFile(a.codeplug)
    text = codeplug_report(f, name=os.path.basename(a.codeplug))
    if a.output:
        open(a.output, 'w', encoding='latin1').write(text)
    else:
        sys.stdout.write(text)


if __name__ == '__main__':
    main()
