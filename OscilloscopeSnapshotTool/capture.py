"""One-shot capture tool for the Rigol DS1104Z Plus, over LAN.

Grabs an AC-coupled snapshot (ripple/noise), an FFT of that same ripple
(frequency spectrum), and a DC-coupled snapshot (actual output level) of
one channel - each as a screenshot (PNG) + waveform data (CSV), saved
together in their own timestamped folder under Snapshots/ so it's always
obvious which files belong together.

The AC pass uses a fixed scale/offset every run. The FFT pass uses the
scope's own built-in MATH FFT feature on the same AC-coupled signal. The DC
pass computes scale and offset automatically from a quick measurement of
the actual DC level, since that varies a lot between the different rails
this board outputs (3.3V, 5V, 12V, 15V, ...) and would otherwise need to be
re-tuned by hand every time.
"""

import math
import os
import sys
import time
from datetime import datetime

import numpy as np
import pyvisa

CHANNEL = 1  # which scope channel is probing the power supply output

# Rigol scopes return a sentinel value around 9.9E37 for an invalid
# measurement (e.g. not enough valid signal to compute it).
_INVALID_MEASUREMENT_THRESHOLD = 1e30

MIN_CHANNEL_SCALE = 0.001
MAX_CHANNEL_SCALE = 10

# Fixed AC-coupled viewing setup, used every run instead of trying to
# preserve/restore whatever was manually set before - simpler and more
# consistent between runs. Adjust here if 10mV/div isn't the right default
# for your ripple levels.
AC_FIXED_SCALE = 0.01
AC_FIXED_OFFSET = 0
# The scope only supports timebase values in a standard 1-2-5 step
# sequence (...50ns, 100ns, 200ns, 500ns, 1us...). Confirmed on real
# hardware that requesting a non-standard value (250ns) gets rounded UP to
# the next valid step (500ns), not to the nearest one - so 100ns is set
# directly here as an exact valid step, avoiding any rounding ambiguity.
AC_FIXED_TIMEBASE = 100e-9

# DC pass just needs to read a level, not characterize fast transients, so
# the exact timebase barely matters here - a moderate, fixed value just to
# avoid depending on whatever the scope was last left at.
DC_FIXED_TIMEBASE = 1e-3  # 1ms/div

# FFT is computed on the AC-coupled signal (the ripple/noise), using the
# scope's own built-in MATH FFT feature. HANNing is the standard
# general-purpose window for this kind of noise/ripple analysis - better
# balance of frequency resolution vs. amplitude accuracy than the scope's
# own default (Rectangle, more suited to one-shot transients). DB gives a
# log scale, which reads better than VRMS when multiple harmonics span a
# wide dynamic range.
FFT_WINDOW = "HANNing"
FFT_UNIT = "DB"

# The FFT's frequency range/resolution comes from the underlying time-
# domain timebase, NOT a separate setting - reusing AC's fast 500ns/div
# would push the Nyquist range into the 100s of MHz, squeezing whatever
# switching-frequency content you actually care about (likely under a few
# MHz) into a tiny sliver of the spectrum. This slower value trades some
# max frequency for better low-frequency resolution - a starting point,
# likely needs tuning once you see real spectra.
FFT_FIXED_TIMEBASE = 10e-6  # 10us/div

# ---- Long-trace capture (separate, on-demand - not part of the regular
# AC/FFT/DC snapshot sequence) -------------------------------------------
# Reads back the scope's full internal acquisition memory for CH1 (using
# the same fixed AC-coupled view as the regular AC pass) instead of just
# the ~1200 on-screen points, for a much longer/finer-resolution record -
# and computes an independent, full-resolution FFT from it in Python.
LONG_TRACE_POINTS = 1_000_000
# Confirmed against the real programming manual: with one channel enabled,
# :ACQuire:MDEPth only accepts {AUTO|12000|120000|1200000|12000000|
# 24000000} - not an arbitrary integer - so 1,000,000 itself isn't valid.
# This is the next step up; only the first LONG_TRACE_POINTS of it get
# read back and saved, so the saved trace is still exactly 1,000,000
# points.
LONG_TRACE_MDEPTH = 1_200_000
# Max points per single :WAVeform:DATA? query in RAW/BYTE mode - matches
# the chunk size used by the well-tested pklaus/ds1054z open-source driver
# for this same scope family.
_RAW_READ_CHUNK = 250_000
# Pause before reconnecting fresh specifically for the long capture (see
# run_capture()'s bundled long-capture step) - gives the scope's LAN
# interface time to release the AC/FFT/DC passes' connection before a new
# one takes its place, same reasoning as the outer retry delay.
_LONG_CAPTURE_RECONNECT_DELAY_S = 5.0


def _resource_string(ip_address):
    return f"TCPIP0::{ip_address}::INSTR"


def _strip_tmc_header(raw_bytes):
    """Strips the IEEE-488.2 TMC block header ('#<n><n digits of length>')
    from a raw SCPI binary-block response, returning just the data bytes.
    """
    if not raw_bytes.startswith(b"#"):
        return raw_bytes
    digit_count = int(raw_bytes[1:2])
    header_len = 2 + digit_count
    data_len = int(raw_bytes[2:header_len])
    return raw_bytes[header_len:header_len + data_len]


def _parse_measurement(raw_value):
    value = float(raw_value)
    if math.isnan(value) or abs(value) > _INVALID_MEASUREMENT_THRESHOLD:
        return float("nan")
    return value


def open_scope(ip_address, timeout_ms=15000):
    rm = pyvisa.ResourceManager("@py")
    inst = rm.open_resource(_resource_string(ip_address))
    inst.timeout = timeout_ms
    inst.read_termination = "\n"
    inst.write_termination = "\n"
    return rm, inst


def get_preamble(inst):
    fields = inst.query(":WAVeform:PREamble?").strip().split(",")
    return {
        "xincrement": float(fields[4]),
        "xorigin": float(fields[5]),
    }


def capture_waveform(inst, source):
    """Returns (x_values, y_values) for the currently displayed trace on
    `source` (e.g. "CHANnel1", or "MATH" for the FFT result). For a normal
    channel, x is time in seconds and y is volts; for MATH set to FFT, x is
    frequency in Hz and y is in whatever :MATH:FFT:UNIT is set to (dB or
    Vrms) - the same :WAVeform:PREamble?/:WAVeform:DATA? mechanism applies
    either way, just with different units.
    """
    inst.write(f":WAVeform:SOURce {source}")
    inst.write(":WAVeform:MODE NORM")  # just the ~1200 on-screen points
    inst.write(":WAVeform:FORMat ASCii")  # ASCII values are already real units
    inst.write(":WAVeform:STARt 1")
    inst.write(":WAVeform:STOP 1200")

    preamble = get_preamble(inst)
    raw = inst.query(":WAVeform:DATA?")
    if raw.startswith("#"):
        digit_count = int(raw[1])
        header_len = 2 + digit_count
        raw = raw[header_len:]
    values = [float(v) for v in raw.strip().split(",") if v.strip()]
    x_values = [preamble["xorigin"] + i * preamble["xincrement"] for i in range(len(values))]
    return x_values, values


def _get_raw_preamble_fields(inst):
    """Like get_preamble(), but also pulls the Y-axis fields needed to
    convert RAW/BYTE waveform data (raw ADC codes, not real units) into
    volts - the ASCII format used by capture_waveform() returns
    already-scaled values, so it never needed these.
    """
    fields = inst.query(":WAVeform:PREamble?").strip().split(",")
    return {
        "xincrement": float(fields[4]),
        "xorigin": float(fields[5]),
        "yincrement": float(fields[7]),
        "yorigin": float(fields[8]),
        "yreference": float(fields[9]),
    }


def capture_raw_waveform(inst, channel, num_points):
    """Reads up to `num_points` samples of `channel`'s full internal
    acquisition memory (not just the ~1200 on-screen points capture_
    waveform() reads) and returns (times, volts) as numpy arrays. The
    scope must already be STOPped - RAW mode only exposes more than the
    on-screen buffer while stopped.

    A single :WAVeform:DATA? query can't return the whole thing at once,
    so this reads it in chunks of _RAW_READ_CHUNK points at a time,
    advancing :WAVeform:STARt/:WAVeform:STOP each round - the same
    approach used by the well-tested pklaus/ds1054z open-source driver for
    this scope family. BYTE format returns raw 8-bit ADC codes rather than
    real units, converted to volts via the formula confirmed against
    Rigol's own official deep-memory data collection example:
        volts = (code - yreference - yorigin) * yincrement
    """
    inst.write(f":WAVeform:SOURce CHANnel{channel}")
    inst.write(":WAVeform:MODE RAW")
    inst.write(":WAVeform:FORMat BYTE")

    preamble = _get_raw_preamble_fields(inst)

    # Chunk-by-chunk logging - this loop previously had zero diagnostic
    # visibility, unlike everywhere else in this file. A real-hardware
    # failure here showed nothing but silence between the last setup print
    # and the eventual "resource not found" error from the *next* connect
    # attempt, with no way to tell which chunk (or whether the transfer
    # itself, vs. something after it) actually failed. Logging each chunk
    # as it completes - and exactly which one failed, if any - closes that
    # gap for next time.
    total_chunks = math.ceil(num_points / _RAW_READ_CHUNK)
    raw_bytes = bytearray()
    pos = 1
    chunk_num = 0
    while len(raw_bytes) < num_points:
        chunk_num += 1
        stop = min(num_points, pos + _RAW_READ_CHUNK - 1)
        try:
            inst.write(f":WAVeform:STARt {pos}")
            inst.write(f":WAVeform:STOP {stop}")
            inst.write(":WAVeform:DATA?")
            chunk_data = _strip_tmc_header(inst.read_raw())
        except pyvisa.errors.VisaIOError as exc:
            print(f"[Long capture read] chunk {chunk_num}/{total_chunks} "
                  f"(points {pos}-{stop}) FAILED: {exc}")
            raise
        print(f"[Long capture read] chunk {chunk_num}/{total_chunks} "
              f"(points {pos}-{stop}) OK, {len(chunk_data)} bytes")
        raw_bytes += chunk_data
        pos = stop + 1

    codes = np.frombuffer(bytes(raw_bytes[:num_points]), dtype=np.uint8).astype(np.float64)
    volts = (codes - preamble["yreference"] - preamble["yorigin"]) * preamble["yincrement"]
    times = preamble["xorigin"] + np.arange(len(volts)) * preamble["xincrement"]
    return times, volts


def compute_fft(times, volts):
    """Computes a full-resolution FFT of a raw time-domain trace using
    numpy directly, rather than the scope's own on-screen MATH FFT - this
    is what actually benefits from a long capture's much finer frequency
    resolution, since the scope's own FFT readout is tied to whatever's on
    the current display/timebase either way. Uses a Hanning window (same
    choice as the scope's own FFT pass, for a consistent look) with
    coherent-gain correction so the result still reads in true volts.

    Returns (freqs_hz, magnitude_v, magnitude_db) - magnitude_v is the
    single-sided linear amplitude spectrum in volts, magnitude_db is
    20*log10(magnitude_v) in dBV (0dB = 1V amplitude). Both are saved so
    either can be plotted without recomputing.
    """
    n = len(volts)
    dt = times[1] - times[0]
    window = np.hanning(n)
    coherent_gain = window.mean()
    windowed = (volts - volts.mean()) * window  # remove DC before windowing

    spectrum = np.fft.rfft(windowed) / n / coherent_gain
    freqs = np.fft.rfftfreq(n, d=dt)

    magnitude_v = 2 * np.abs(spectrum)  # single-sided amplitude
    magnitude_v[0] /= 2  # DC bin isn't doubled like the rest

    with np.errstate(divide="ignore"):
        magnitude_db = 20 * np.log10(np.maximum(magnitude_v, 1e-12))

    return freqs, magnitude_v, magnitude_db


def capture_screenshot(inst):
    """Returns the raw PNG bytes of the current screen."""
    inst.write(":DISPlay:DATA? ON,OFF,PNG")
    raw = inst.read_raw()
    return _strip_tmc_header(raw)


def _drain_scope_errors(inst, label):
    """Reads and prints any errors sitting in the scope's SCPI error queue,
    tagged with `label` so it's obvious which stage of the sequence they
    came from. A rejected command (e.g. an out-of-range parameter) doesn't
    raise anything on the Python/VISA side - the write() call returns
    normally, but the scope silently queues an error and shows something
    like "Invalid Input!" on its own screen. This is how that gets
    surfaced instead of guessed at.

    This is a diagnostic only - it must never be able to take down the
    capture it's checking. Real-hardware testing found the DC pass's
    :SYSTem:ERRor? query occasionally timing out outright, which (before
    this try/except) raised a VisaIOError that run_capture()'s retry
    wrapper caught, restarting the *entire* AC/FFT/DC sequence from
    scratch - three timeouts in a row looked like it "got stuck in a
    loop", producing three dead folders that each only got as far as
    AC+FFT. A timeout here now just gets logged and skipped.
    """
    for _ in range(20):
        try:
            entry = inst.query(":SYSTem:ERRor?").strip()
        except pyvisa.errors.VisaIOError as exc:
            print(f"[{label}] could not read scope error queue ({exc}) - skipping this check")
            return
        if entry.startswith("0,"):
            return
        print(f"[{label}] scope error: {entry}")


def measure_dc_level(inst, channel):
    """Average (DC) voltage on `channel`, or NaN if the scope reports an
    invalid measurement.
    """
    inst.write(f":MEASure:ITEM VAVG,CHANnel{channel}")
    return _parse_measurement(inst.query(f":MEASure:ITEM? VAVG,CHANnel{channel}"))


# The DS1000Z's max allowed :OFFSet magnitude depends on the CURRENT
# :SCALe: only +-2V below 500mV/div, but +-100V from 500mV/div up (confirmed
# against real hardware - a requested -3.3V offset at a 0.33V/div scale
# came back clamped to only -2.0V). Every rail we care about (3.3V+) needs
# more than 2V of offset to center, so the DC scale must never be allowed
# to drop below 500mV/div, regardless of how tightly zoomed-in that would
# otherwise be for a small rail.
MIN_DC_SCALE_FOR_OFFSET_RANGE = 0.5


def choose_dc_scale(dc_level, target_divisions=6, margin_fraction=0.3,
                     min_scale=MIN_DC_SCALE_FOR_OFFSET_RANGE, max_scale=MAX_CHANNEL_SCALE):
    """Volts/div that keeps a DC level of `dc_level` comfortably framed once
    centered via offset, with margin_fraction extra headroom above/below
    (e.g. 0.3 = the display comfortably covers +-30% around the level, in
    case the actual output is off from nominal or has some ripple).
    """
    span = abs(dc_level) * 2 * margin_fraction
    if span <= 0:
        return max_scale
    return min(max(span / target_divisions, min_scale), max_scale)


def _save_npz(run_folder, filename, arrays):
    """Saves any number of named arrays into one compressed .npz file - the
    Python-friendly replacement for the old per-run CSVs (a PI's
    suggestion, since 1M+ point traces are unwieldy as text). Load it back
    with:
        import numpy as np
        data = np.load("..._AC.npz")
        times, volts = data["time_s"], data["CH1_volts"]
    (whatever array names were passed in `arrays` here) - each file is
    self-describing without needing a separate header row the way the
    CSVs did.
    """
    path = os.path.join(run_folder, filename)
    np.savez_compressed(path, **{k: np.asarray(v) for k, v in arrays.items()})
    return path


def _save_png(run_folder, filename, png_bytes):
    path = os.path.join(run_folder, filename)
    with open(path, "wb") as f:
        f.write(png_bytes)
    return path


def save_long_capture_plot(output_folder, file_prefix, times, volts, freqs, magnitude_db):
    """Saves a quick-look PNG (time-domain trace + FFT spectrum, stacked)
    next to the long capture's .npz files. Unlike the AC/FFT/DC passes,
    there's no on-scope screenshot for this data (nothing meaningful to
    show on the scope's own screen for a record this size), so this is
    the only visual reference for a long capture run - matplotlib is
    imported lazily here (not at the top of the file) so the routine
    AC/FFT/DC-only path never pays its import cost when a long capture
    isn't actually being done.
    """
    import matplotlib
    matplotlib.use("Agg")  # headless/file-only backend - no GUI, safe from any thread
    import matplotlib.pyplot as plt

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 7))

    ax1.plot(times * 1e3, volts * 1e3, linewidth=0.5)
    ax1.set_xlabel("Time (ms)")
    ax1.set_ylabel("Voltage (mV)")
    ax1.set_title(f"Long AC-coupled trace ({len(times):,} points)")
    ax1.grid(True, alpha=0.3)

    ax2.plot(freqs / 1e6, magnitude_db, linewidth=0.5)
    ax2.set_xlabel("Frequency (MHz)")
    ax2.set_ylabel("Magnitude (dBV)")
    ax2.set_title("Full-resolution FFT")
    ax2.grid(True, alpha=0.3)

    fig.tight_layout()
    path = os.path.join(output_folder, f"{file_prefix}_LONG.png")
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return path


def run_capture(ip_address, expected_dc_voltage=None, include_long_capture=False,
                 progress_callback=None, max_attempts=3, retry_delay_s=5.0):
    """Connects to the scope, grabs an AC-coupled snapshot, an FFT of that
    ripple, and a DC-coupled snapshot into their own timestamped folder
    under Snapshots/, and returns (run_folder, measured_dc_level) -
    measured_dc_level is NaN if that measurement was invalid.

    expected_dc_voltage, if given, is the nominal voltage you expect this
    rail to be (e.g. 12.0 for a 12V rail). It's used to set a safe starting
    scale *before* switching to DC coupling and measuring - without it, the
    scope would still be on whatever tiny scale was tuned for AC ripple
    viewing, and a real DC level would clip off-screen immediately, making
    the very first measurement inaccurate (a measurement taken while
    clipped doesn't reflect the true voltage). If not given, a generously
    wide default scale is used instead so nothing clips, at the cost of a
    less-precise first measurement (still corrected in a second pass).

    include_long_capture, if True, also runs the long-trace capture
    (LONG_TRACE_POINTS raw samples + a full-resolution Python FFT) as part
    of this same run, reusing the same connection - saved in a
    longcapture_<timestamp> subfolder alongside the AC/FFT/DC files rather
    than its own separate top-level folder, so it's obvious it belongs to
    this particular snapshot. For a long capture on its own, without the
    rest of the sequence, use run_long_capture() instead.

    progress_callback(stage, status), if given, is called with
    stage in ("ac", "fft", "dc") - plus "long" if include_long_capture is
    True - and status in ("in_progress", "done") - lets a GUI show
    per-stage progress without this function knowing about Tkinter.

    Retries the whole capture (fresh connection each time) up to
    max_attempts times if it fails with a VISA I/O error - this scope's
    LAN/VXI-11 connection can occasionally get into a stale state after
    several connections in quick succession (same issue seen with the
    filter sweep tool), and a fresh reconnect after a pause usually clears
    it. The default pause (retry_delay_s) is longer than it used to be -
    real-hardware testing with a bundled long capture saw the *next*
    reconnect attempt fail outright with VI_ERROR_RSRC_NFOUND (the scope
    not found at all, not just a slow response) after a capture that kept
    the connection open much longer and moved far more data than the
    routine passes - suggesting the LAN interface needs more than a
    couple seconds to fully release a heavier connection before it's
    ready to accept a new one.
    """
    progress_callback = progress_callback or (lambda stage, status: None)

    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            return _run_capture_once(ip_address, expected_dc_voltage, include_long_capture,
                                      progress_callback)
        except pyvisa.errors.VisaIOError as exc:
            last_error = exc
            if attempt < max_attempts:
                time.sleep(retry_delay_s)
    raise last_error


def _run_capture_once(ip_address, expected_dc_voltage, include_long_capture, progress_callback):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_name = f"snapshot_{stamp}"
    run_folder = os.path.join(script_dir, "Snapshots", run_name)

    # A longer timeout than the AC/FFT/DC passes alone would need, since
    # an optional long capture's chunked binary reads take noticeably
    # longer than the routine passes' small ASCII queries.
    rm, inst = open_scope(ip_address, timeout_ms=30000)
    try:
        inst.query("*IDN?").strip()

        # Only create the folder once the connection is actually working -
        # otherwise a failed reconnect (e.g. retrying right after a long
        # capture, before the scope's LAN interface has released the
        # previous connection) leaves an empty, confusing folder behind
        # for every failed attempt.
        os.makedirs(run_folder, exist_ok=True)

        # Cursors (the small AX/AY/BX/BY/BX-AX box) can be left on from
        # manual front-panel use and sit right on top of the plot in every
        # screenshot - turned off up front, before anything else, so it's
        # never left to whatever state the scope was in before this run.
        inst.write(":CURSor:MODE OFF")

        # ----- AC pass: ripple/noise, fixed scale/offset every run -----
        # (previously this read back and restored "whatever was there before",
        # but that read-back could itself be corrupted by the coupling-switch
        # side effects below, making AC data look inconsistent between runs
        # for no real circuit reason - a fixed, known value sidesteps that
        # entirely.)
        inst.write(f":CHANnel{CHANNEL}:COUPling AC")
        # Fine (Vernier) mode lets :SCALe apply arbitrary values instead of
        # snapping to the standard 1-2-5 step sequence (1mV, 2mV, 5mV,
        # 10mV...) - AC_FIXED_SCALE happens to already be a standard step,
        # but forcing this explicitly means it's never silently dependent
        # on whatever Fine/Coarse state the front panel was last left in.
        inst.write(f":CHANnel{CHANNEL}:VERNier ON")
        inst.write(f":CHANnel{CHANNEL}:OFFSet {AC_FIXED_OFFSET}")
        inst.write(f":CHANnel{CHANNEL}:SCALe {AC_FIXED_SCALE}")
        # Fixed, deterministic trigger too - previously left at whatever was
        # last set manually, which could leave the scope in Normal sweep
        # mode without a trigger level the small AC-coupled ripple signal
        # reliably crosses. In that state the scope just holds onto
        # whatever it last successfully triggered on (stale data, possibly
        # from a totally different pass/coupling/scale) instead of
        # acquiring anything fresh - a likely source of "different results
        # every time". AUTO sweep mode always produces a fresh acquisition
        # on a timeout even without a clean edge, and 0V is the right level
        # for this AC-coupled, zero-offset signal.
        inst.write(":TRIGger:SWEep AUTO")
        inst.write(":TRIGger:MODE EDGE")
        inst.write(f":TRIGger:EDGE:SOURce CHANnel{CHANNEL}")
        inst.write(f":TRIGger:EDGE:LEVel {AC_FIXED_OFFSET}")
        print(f"[AC trigger] sweep=AUTO, source=CH{CHANNEL}, level={AC_FIXED_OFFSET}V")
        # Make sure the scope is actively running before changing timebase -
        # changing it while stopped doesn't trigger a new acquisition, it
        # just re-interprets old data at the new scale (the exact bug found
        # in the FFT pass below). Defensive here in case the scope happened
        # to already be stopped when this run started.
        inst.write(":RUN")
        inst.write(f":TIMebase:MAIN:SCALe {AC_FIXED_TIMEBASE}")
        actual_timebase = float(inst.query(":TIMebase:MAIN:SCALe?"))
        print(f"[AC timebase] requested {AC_FIXED_TIMEBASE * 1e9:.0f}ns/div, "
              f"scope reports actual {actual_timebase * 1e9:.0f}ns/div")
        # "Measure All" statistics take up screen space that would otherwise
        # go to the waveform itself - off for AC, since the whole point here
        # is maximizing how much of the plot the ripple signal fills.
        inst.write(":MEASure:ADISplay OFF")
        time.sleep(0.3)
        # "Invalid Input!" has shown up on real captures without the FFT
        # pass's own error-queue checks ever catching anything - checking
        # here too, in case it's actually coming from earlier in the
        # sequence (AC pass) rather than the FFT setup.
        _drain_scope_errors(inst, "AC pass")

        # Freeze the trace so the .npz and PNG are guaranteed to reflect the
        # exact same acquisition, instead of risking the display updating
        # in between the two separate captures below.
        inst.write(":STOP")

        progress_callback("ac", "in_progress")
        times, values = capture_waveform(inst, f"CHANnel{CHANNEL}")
        _save_npz(run_folder, f"{run_name}_AC.npz", {"time_s": times, f"CH{CHANNEL}_volts": values})
        png_bytes = capture_screenshot(inst)
        _save_png(run_folder, f"{run_name}_AC.png", png_bytes)
        progress_callback("ac", "done")

        # ----- FFT pass: frequency spectrum of the same AC-coupled signal -----
        progress_callback("fft", "in_progress")
        # The AC pass left the scope STOPPED (frozen) at 100ns/div. Changing
        # the timebase while stopped does NOT trigger a new acquisition - it
        # just re-interprets the same old frozen (very short, ~6us of real
        # samples) data as if it were captured at the new timebase, which is
        # exactly the "not enough data" mismatch seen on real hardware. Must
        # explicitly :RUN first so the scope is actively acquiring when the
        # timebase changes, forcing a genuine fresh capture at 10us/div.
        inst.write(":RUN")
        # The FFT's frequency range/resolution comes from this timebase, not
        # a separate FFT-only setting.
        inst.write(f":TIMebase:MAIN:SCALe {FFT_FIXED_TIMEBASE}")
        time.sleep(0.5)  # let a fresh acquisition actually complete at the new timebase
        inst.write(":MATH:DISPlay ON")
        inst.write(":MATH:OPERator FFT")
        inst.write(f":MATH:SOURce1 CHANnel{CHANNEL}")
        inst.write(f":MATH:FFT:WINDow {FFT_WINDOW}")
        inst.write(f":MATH:FFT:UNIT {FFT_UNIT}")
        inst.write(":MATH:FFT:SPLit OFF")  # full-screen FFT grid, easier to read/screenshot
        # NOTE: previously also turned CH1's own display off here for a
        # spectrum-only screenshot, on the assumption that display state is
        # purely cosmetic and separate from the acquired data the FFT is
        # computed from. Removed again - real-hardware testing suggests the
        # FFT actually does need CH1 visible to keep producing a result,
        # contrary to that assumption. Time-domain trace stays visible
        # alongside the spectrum for now.
        # "Measure All" can't show anything FFT-specific (measurement items
        # only support the physical channels, never MATH) - off here too,
        # same reasoning as AC: maximize the space for the spectrum plot.
        inst.write(":MEASure:ADISplay OFF")
        # Catches a rejected command anywhere in the FFT setup above (this
        # is where the "Invalid Input!" seen on real captures most likely
        # originates) instead of continuing to guess which one it was.
        _drain_scope_errors(inst, "FFT setup")

        # Let the scope actually compute at least one real FFT result before
        # asking it to auto-fit the scale to that result - sending
        # :MATH:RESet immediately after switching to FFT mode (before any
        # real spectrum exists yet) had nothing meaningful to fit to, which
        # is why the auto-scale wasn't visibly doing anything.
        time.sleep(1.0)
        # Auto-fits the MATH result's own vertical scale/offset to whatever
        # the current operator (FFT) actually produced, instead of leaving
        # it at some leftover/default scale that doesn't fit the data well.
        inst.write(":MATH:RESet")
        time.sleep(0.5)  # let the new scale settle into a fresh acquisition
        # :MATH:RESet auto-fits the vertical SCALe to the actual spectrum,
        # but its auto-picked OFFSet centers on wherever the data happens to
        # sit, not necessarily on 0dB. The manual documents :MATH:OFFSet's
        # default as 0, and by analogy with the already-verified
        # :CHANnel:OFFSet convention an offset of 0 *should* put the value
        # 0 - here, 0dB - exactly at the screen's vertical center - but
        # that analogy hasn't been directly confirmed for MATH specifically,
        # and a real capture showed the MATH zero-reference marker sitting
        # visibly below true center despite this write going through with
        # no error. Reading back the actual value here at least confirms
        # whether the write itself took hold (as opposed to the 0=center
        # convention just not applying the same way to MATH).
        inst.write(":MATH:OFFSet 0")
        time.sleep(0.2)
        actual_math_offset = float(inst.query(":MATH:OFFSet?"))
        print(f"[FFT vertical] requested offset=0, scope reports actual offset={actual_math_offset}")
        _drain_scope_errors(inst, "FFT reset")

        # Push 0Hz to the left edge of the screen instead of leaving it
        # wherever :MATH:RESet's auto-scale happened to center it. Confirmed
        # against the real programming manual: :MATH:FFT:HCENter sets the
        # frequency that appears at the horizontal CENTER of the screen
        # (default 5MHz) - so with nothing setting this explicitly, 0Hz
        # could sit well inside the plot with a dead, unused gap to its
        # left, exactly what was hard to read in real captures. This reuses
        # whatever :MATH:RESet already picked for HSCale (its span-fitting
        # is based on the real sample rate - no reason to second-guess it)
        # and just repositions the center. The screen is 12 horizontal
        # divisions wide, so putting 0Hz at the left edge means the center
        # frequency should be 6 divisions' worth to the right of 0.
        # HCENter's documented valid range is 0 to (current screen sample
        # rate * 2/5) - but real-hardware testing shows the FFT operation
        # uses its own internal effective sample rate (the "10.0M Sa/s"
        # shown in the FFT info tag), not the channel's raw acquisition
        # rate from :ACQuire:SRATe? (1GSa/s) - those aren't the same thing,
        # and pre-computing a bound from the wrong one could be more
        # permissive than the scope's real limit. Rather than guess which
        # rate the manual actually means, just write the target and trust
        # the read-back (actual_hcenter below) plus the error-queue check
        # to reveal the truth if it gets clamped or rejected.
        fft_hscale = float(inst.query(":MATH:FFT:HSCale?"))
        target_hcenter = 6 * fft_hscale
        inst.write(f":MATH:FFT:HCENter {target_hcenter}")
        time.sleep(0.3)
        actual_hcenter = float(inst.query(":MATH:FFT:HCENter?"))
        print(f"[FFT axis] hscale={fft_hscale:.0f}Hz/div, requested hcenter={target_hcenter:.0f}Hz, "
              f"scope reports actual={actual_hcenter:.0f}Hz")
        _drain_scope_errors(inst, "FFT hcenter")

        inst.write(":STOP")
        freqs, magnitudes = capture_waveform(inst, "MATH")
        y_label = "magnitude_dB" if FFT_UNIT == "DB" else "magnitude_Vrms"
        _save_npz(run_folder, f"{run_name}_FFT.npz", {"frequency_hz": freqs, y_label: magnitudes})
        png_bytes = capture_screenshot(inst)
        _save_png(run_folder, f"{run_name}_FFT.png", png_bytes)
        inst.write(":MATH:DISPlay OFF")  # don't clutter the DC pass's view
        inst.write(":RUN")
        progress_callback("fft", "done")

        # ----- DC pass: actual output level -----
        progress_callback("dc", "in_progress")

        # Switch coupling to DC FIRST, before touching scale/offset - three
        # attempts at setting offset *before* the coupling switch (with
        # different signs, different timing) all clipped the same way,
        # which points at the offset being reset/ignored by the coupling
        # change itself rather than the sign being wrong.
        inst.write(f":CHANnel{CHANNEL}:COUPling DC")
        # Fine (Vernier) mode matters a lot more here than in the AC pass -
        # choose_dc_scale() computes an arbitrary value (e.g. 1.2V/div for
        # a 12V rail), not a standard 1-2-5 step. Without this, Coarse mode
        # would silently snap it to the nearest standard step instead of
        # the value actually calculated, throwing off the framing with no
        # error or warning.
        inst.write(f":CHANnel{CHANNEL}:VERNier ON")
        # Same deterministic-trigger reasoning as the AC pass, but the
        # level needs to track the actual DC value instead of 0V - a DC
        # rail sitting at, say, 12V would rarely if ever cross a 0V trigger
        # level, which combined with Normal sweep mode could leave the
        # scope stuck showing a stale acquisition instead of the current
        # one. AUTO sweep mode means this matters less than it would in
        # Normal mode, but using the real expected level (falling back to
        # 0V if none was given, matching the wide-default framing below)
        # still gives it the best chance of a clean edge trigger.
        dc_trigger_level = expected_dc_voltage if expected_dc_voltage else 0
        inst.write(":TRIGger:SWEep AUTO")
        inst.write(":TRIGger:MODE EDGE")
        inst.write(f":TRIGger:EDGE:SOURce CHANnel{CHANNEL}")
        inst.write(f":TRIGger:EDGE:LEVel {dc_trigger_level}")
        print(f"[DC trigger] sweep=AUTO, source=CH{CHANNEL}, level={dc_trigger_level}V")
        inst.write(":RUN")
        inst.write(f":TIMebase:MAIN:SCALe {DC_FIXED_TIMEBASE}")
        # Keep the "Measure All" statistics table on for DC - unlike AC,
        # here we want the extra numeric readout alongside the level.
        inst.write(":MEASure:ADISplay ON")
        time.sleep(0.5)

        if expected_dc_voltage:
            # Use your typed value directly for framing - simpler and more
            # predictable than deriving it from a fresh measurement (which
            # adds another round of scale/offset changes and settle timing
            # that could itself go wrong).
            inst.write(f":CHANnel{CHANNEL}:SCALe {choose_dc_scale(expected_dc_voltage)}")
            time.sleep(0.3)
            inst.write(f":CHANnel{CHANNEL}:OFFSet {-expected_dc_voltage}")
            time.sleep(0.3)
        else:
            # No expected value given - use a generously wide, safe default
            # so nothing clips; less precisely framed, but won't clip.
            inst.write(f":CHANnel{CHANNEL}:SCALe {MAX_CHANNEL_SCALE}")
            time.sleep(0.3)
            inst.write(f":CHANnel{CHANNEL}:OFFSet 0")
            time.sleep(0.3)

        time.sleep(0.5)  # extra settle for a fresh acquisition at the new scale/offset

        # Read back what the scope actually applied - if this doesn't match
        # what was requested, the value is being clamped/rejected rather
        # than the sign being wrong. Printed so it shows up in the console
        # (and GUI users can check Snapshots folder console log if run via
        # a terminal) as concrete diagnostic data instead of guessing again.
        actual_scale = float(inst.query(f":CHANnel{CHANNEL}:SCALe?"))
        actual_offset = float(inst.query(f":CHANnel{CHANNEL}:OFFSet?"))
        print(f"[DC framing] requested offset={-expected_dc_voltage if expected_dc_voltage else 0}, "
              f"scope reports actual scale={actual_scale}, actual offset={actual_offset}")
        _drain_scope_errors(inst, "DC pass")

        # Measured only for reporting (expected-vs-actual comparison) - not
        # used to further adjust scale/offset, so a bad/unexpected reading
        # here can't throw off the framing.
        dc_level = measure_dc_level(inst, CHANNEL)

        inst.write(":STOP")
        times, values = capture_waveform(inst, f"CHANnel{CHANNEL}")
        _save_npz(run_folder, f"{run_name}_DC.npz", {"time_s": times, f"CH{CHANNEL}_volts": values})
        png_bytes = capture_screenshot(inst)
        _save_png(run_folder, f"{run_name}_DC.png", png_bytes)
        progress_callback("dc", "done")

        # ----- Optional long-trace capture: same run, own subfolder -----
        if include_long_capture:
            progress_callback("long", "in_progress")
            # Isolated in its own subfolder (same naming convention as the
            # standalone long-capture's top-level folder) rather than
            # mixed in loose alongside the AC/FFT/DC files, but still
            # nested inside this run's folder instead of a separate
            # top-level one, since it's now part of the same snapshot.
            long_folder = os.path.join(run_folder, f"longcapture_{stamp}")

            # Real-hardware testing showed the long capture's big chunked
            # transfer noticeably more likely to fail partway (VI_ERROR_IO)
            # on a connection that's already been through the AC/FFT/DC
            # passes' own substantial command traffic - but the very next
            # retry (which starts with a completely fresh connection)
            # consistently succeeded cleanly through all 4 chunks. Rather
            # than only getting that benefit by redoing the *entire*
            # AC/FFT/DC sequence on a retry, reconnect fresh right here,
            # specifically for the long capture, so it gets the same
            # advantage on the first attempt. Same reasoning as the outer
            # retry delay - the old connection needs time to release
            # before a new one can take its place.
            inst.close()
            rm.close()
            time.sleep(_LONG_CAPTURE_RECONNECT_DELAY_S)
            rm, inst = open_scope(ip_address, timeout_ms=30000)
            inst.query("*IDN?").strip()

            _capture_long_trace(inst, long_folder, run_name)
            progress_callback("long", "done")

        # Leave the scope showing whatever the last pass that ran left it
        # on (DC pass's settings normally; the long capture's AC-coupled
        # settings if include_long_capture was used, since that runs
        # after DC) rather than switching back to anything else - just
        # resume live acquisition instead of leaving it frozen.
        inst.write(":RUN")

    finally:
        inst.close()
        rm.close()

    return run_folder, dc_level


def _capture_long_trace(inst, output_folder, file_prefix):
    """Runs the long-trace capture sequence (AC-coupled, LONG_TRACE_POINTS
    raw samples + a full-resolution Python FFT) on an already-connected,
    already-open `inst`, saving `{file_prefix}_LONG_AC.npz` /
    `{file_prefix}_LONG_FFT.npz` into `output_folder`.

    `output_folder` is only created right before the first file is
    actually saved (after the raw waveform + FFT have both succeeded) -
    not upfront - so a failure partway through (e.g. one of the chunked
    reads below) never leaves an empty folder behind. Real-hardware
    testing hit exactly that: a failed long capture left an empty
    longcapture_<timestamp> subfolder sitting next to otherwise-valid
    AC/FFT/DC files from the same run.

    Doesn't open/close its own connection and doesn't leave the scope
    running at the end - all of that is the caller's job. This is what
    lets the same sequence be reused both by the standalone long-capture
    entry point (run_long_capture(), its own folder/connection) and by
    run_capture() when a long capture is bundled into the same run.
    """
    inst.write(f":CHANnel{CHANNEL}:COUPling AC")
    # Same reasoning as the regular AC pass - forces exact scale values
    # regardless of whatever Fine/Coarse state the front panel was last
    # left in.
    inst.write(f":CHANnel{CHANNEL}:VERNier ON")
    inst.write(f":CHANnel{CHANNEL}:OFFSet {AC_FIXED_OFFSET}")
    inst.write(f":CHANnel{CHANNEL}:SCALe {AC_FIXED_SCALE}")
    # Same deterministic-trigger reasoning as the regular AC pass.
    inst.write(":TRIGger:SWEep AUTO")
    inst.write(":TRIGger:MODE EDGE")
    inst.write(f":TRIGger:EDGE:SOURce CHANnel{CHANNEL}")
    inst.write(f":TRIGger:EDGE:LEVel {AC_FIXED_OFFSET}")
    inst.write(f":ACQuire:MDEPth {LONG_TRACE_MDEPTH}")
    # Make sure the scope is actively running before changing timebase/
    # memory depth - same reasoning as the regular AC pass: changing these
    # while stopped doesn't trigger a fresh acquisition.
    inst.write(":RUN")
    inst.write(f":TIMebase:MAIN:SCALe {AC_FIXED_TIMEBASE}")
    time.sleep(1.0)  # let a full acquisition at this memory depth actually complete
    _drain_scope_errors(inst, "Long capture setup")
    inst.write(":STOP")

    # Diagnostic readback - memory depth is one of the enumerated values
    # the scope actually accepted (may differ from what was requested),
    # and the achieved sample rate determines both how long a time window
    # LONG_TRACE_POINTS actually spans and the frequency resolution/range
    # the FFT below will have.
    actual_mdepth = inst.query(":ACQuire:MDEPth?").strip()
    actual_srate = float(inst.query(":ACQuire:SRATe?"))
    span_s = LONG_TRACE_POINTS / actual_srate
    print(f"[Long capture] requested mdepth={LONG_TRACE_MDEPTH}, scope reports actual "
          f"mdepth={actual_mdepth}, sample rate={actual_srate:.3e} Sa/s -> "
          f"{LONG_TRACE_POINTS} points spans {span_s * 1e3:.3f} ms")

    times, volts = capture_raw_waveform(inst, CHANNEL, LONG_TRACE_POINTS)
    freqs, magnitude_v, magnitude_db = compute_fft(times, volts)

    # Only reaches here once both the raw capture and the FFT computation
    # have fully succeeded - safe to create the folder now.
    os.makedirs(output_folder, exist_ok=True)
    _save_npz(output_folder, f"{file_prefix}_LONG_AC.npz",
              {"time_s": times, f"CH{CHANNEL}_volts": volts})
    _save_npz(output_folder, f"{file_prefix}_LONG_FFT.npz",
              {"frequency_hz": freqs, "magnitude_V": magnitude_v, "magnitude_dB": magnitude_db})
    save_long_capture_plot(output_folder, file_prefix, times, volts, freqs, magnitude_db)


def run_long_capture(ip_address, progress_callback=None, max_attempts=3, retry_delay_s=5.0):
    """Standalone, on-demand long-trace capture (its own top-level
    Snapshots/longcapture_<timestamp>/ folder, its own connection) - for
    when you want just the long trace without the full AC/FFT/DC sequence.
    If you want it bundled into a regular snapshot instead (saved in a
    subfolder alongside the AC/FFT/DC files), pass
    include_long_capture=True to run_capture() instead.

    Returns run_folder. Same auto-retry-on-VISA-I/O-error behavior as
    run_capture().
    """
    progress_callback = progress_callback or (lambda status: None)

    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            return _run_long_capture_once(ip_address, progress_callback)
        except pyvisa.errors.VisaIOError as exc:
            last_error = exc
            if attempt < max_attempts:
                time.sleep(retry_delay_s)
    raise last_error


def _run_long_capture_once(ip_address, progress_callback):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_name = f"longcapture_{stamp}"
    run_folder = os.path.join(script_dir, "Snapshots", run_name)

    # Longer timeout than the default - transferring a million points in
    # 250k-point chunks takes noticeably longer than the routine passes.
    rm, inst = open_scope(ip_address, timeout_ms=30000)
    try:
        inst.query("*IDN?").strip()
        progress_callback("in_progress")

        # Same reasoning as the regular capture - cursors left on from
        # manual use would otherwise sit on top of the plot.
        inst.write(":CURSor:MODE OFF")

        _capture_long_trace(inst, run_folder, run_name)

        # Resume live acquisition rather than leaving the scope frozen -
        # same end-of-run convention as the regular snapshot sequence.
        inst.write(":RUN")
        progress_callback("done")
    finally:
        inst.close()
        rm.close()

    return run_folder


def main():
    if "--long" in sys.argv:
        remaining_args = [a for a in sys.argv[1:] if a != "--long"]
        ip_address = remaining_args[0] if remaining_args else input("Scope IP address: ").strip()

        def print_long_progress(status):
            if status == "in_progress":
                print(f"Capturing long trace ({LONG_TRACE_POINTS:,} points - this takes longer "
                      f"than a regular snapshot)...")
            elif status == "done":
                print("  Long trace + FFT saved.")

        run_folder = run_long_capture(ip_address, progress_callback=print_long_progress)
        print(f"\nDone. Saved to: {run_folder}")
        return

    ip_address = sys.argv[1] if len(sys.argv) > 1 else input("Scope IP address: ").strip()
    expected_str = input("Expected DC voltage (V, blank to skip): ").strip()
    expected_dc_voltage = float(expected_str) if expected_str else None
    include_long_capture = input(
        f"Include long capture too? ({LONG_TRACE_POINTS:,} points + FFT, takes longer, "
        "saved in a subfolder) [y/N]: "
    ).strip().lower().startswith("y")

    def print_progress(stage, status):
        label = {"ac": "AC (ripple)", "fft": "FFT (spectrum)", "dc": "DC (level)",
                  "long": "Long capture"}[stage]
        if status == "in_progress":
            print(f"Capturing {label}...")
        elif status == "done":
            print(f"  {label} saved.")

    run_folder, measured_dc = run_capture(
        ip_address, expected_dc_voltage=expected_dc_voltage,
        include_long_capture=include_long_capture, progress_callback=print_progress
    )
    print(f"\nDone. Saved to: {run_folder}")
    if math.isnan(measured_dc):
        print("Measured DC level: invalid/unavailable")
    elif expected_dc_voltage:
        diff = measured_dc - expected_dc_voltage
        direction = "higher" if diff > 0 else "lower" if diff < 0 else "exactly as expected"
        print(f"Measured DC level: {measured_dc:.4f} V (expected {expected_dc_voltage:.4f} V, "
              f"{abs(diff):.4f} V {direction})")
    else:
        print(f"Measured DC level: {measured_dc:.4f} V")


if __name__ == "__main__":
    main()
