# Power Supply Snapshot Tool

A small tool for grabbing a one-shot snapshot from the Rigol DS1104Z Plus
oscilloscope over LAN — for documenting the ripple/noise performance, its
frequency spectrum, and the actual output level of the power supply
board's outputs. Each run saves a screenshot and the underlying waveform
data for **three** passes — AC-coupled (ripple/noise), FFT (frequency
spectrum of that ripple), and DC-coupled (actual level) — together in
their own timestamped folder, so it's always obvious which files belong
together. There's also an optional **long capture** — a much longer
(1,000,000-point) raw trace and a full-resolution FFT computed from it —
you can tick on before clicking Take Snapshot; see
[Long trace capture](#long-trace-capture) below.

Waveform data is saved as compressed NumPy `.npz` files rather than CSV
(a PI's suggestion, since the long trace in particular is unwieldy as
text) — see [Reading the .npz files in Python](#reading-the-npz-files-in-python).

## What it does

Each time you click **Take Snapshot** (or run the command-line version),
the tool:

1. Connects to the oscilloscope over LAN.
2. **AC pass**: sets CH1 to AC coupling with a fixed **10mV/div scale,
   0V offset, and 100ns/div timebase** (so ripple is clearly visible
   regardless of what was set before), turns the "Measure All" statistics
   table **off** (to give the waveform as much of the plot as possible),
   freezes the trace, and saves the waveform (`.npz`) and screenshot (PNG)
   as `..._AC.npz` / `..._AC.png`.
3. **FFT pass**: uses the scope's built-in MATH FFT feature on that same
   AC-coupled signal, first switching to a **10µs/div timebase** (the
   FFT's frequency range/resolution comes from this, not a separate
   setting - a slower timebase than AC's gives better low-frequency
   resolution for typical switching-noise frequencies) - Hanning window,
   dB units, full-screen grid, CH1's time-domain trace still shown
   alongside it, auto-scaled via `:MATH:RESet` after giving the scope a
   moment to compute a real result first - to get the frequency spectrum,
   "Measure All" also off. It then repositions **0Hz to the left edge**
   and **0dB to the vertical center** of the spectrum plot (see "FFT axis
   positioning" below) and saves `..._FFT.npz` (frequency_hz,
   magnitude_dB) / `..._FFT.png`.
4. **DC pass**: switches CH1 to DC coupling, a fixed **1ms/div timebase**
   (this pass just reads a level, so the exact timebase barely matters -
   picked a fixed, moderate value rather than depending on whatever the
   scope was last left at), and a scale/offset that frames the actual
   output level — based on the **expected DC voltage** you type in (see
   below), or a safe wide default if left blank — turns "Measure All"
   **on** (useful alongside the level readout here), then freezes the
   trace and saves `..._DC.npz` / `..._DC.png`.
5. Measures the actual DC level and reports how it compares to what you
   expected (higher/lower, and by how much).
6. Resumes live acquisition on the scope so you can keep watching between
   captures — you never need to touch Run/Stop yourself. The scope is left
   showing the **DC pass's settings** (not reverted back to AC) and
   actively running, not frozen.

The AC pass uses **fixed** scale/offset/timebase every run (rather than
trying to preserve whatever was manually set before) — this is deliberate,
since letting it drift from run to run made AC data look inconsistent
between captures for no real circuit reason. If 10mV/div or 100ns/div
isn't right for what you're seeing, change `AC_FIXED_SCALE` /
`AC_FIXED_TIMEBASE` near the top of `capture.py`. The FFT window function,
units, and timebase are similarly fixed via `FFT_WINDOW` / `FFT_UNIT` /
`FFT_FIXED_TIMEBASE`, and the DC pass's timebase via `DC_FIXED_TIMEBASE`,
in the same file.

## Expected DC voltage field

This is optional but recommended. Typing in the rail's nominal voltage
(e.g. `12`, `5`, `3.3`) before capturing:
- Sets a scale/offset that safely frames and centers that value on screen
  before switching to DC coupling.
- Gets compared against the actual measured level afterward, so you can
  immediately see if the rail is reading higher or lower than expected and
  by how much.

If left blank, the DC pass falls back to a generously wide scale so
nothing clips, at the cost of less precise framing and no comparison.

## 1. One-time setup (per computer)

If you don't already have Python:
1. Download Python 3.11+ from https://www.python.org/downloads/
2. During install, check **"Add python.exe to PATH"**.
3. Verify from a terminal: `python --version`

If you already have another Python install (e.g. from MSYS2, Anaconda, or a
Microsoft Store install) and `python --version` doesn't show the one you
just installed, use the **`py`** launcher command instead of `python` for
everything below (`py --version`, `py -m pip install ...`, `py main.py`).

Then install the dependencies (only needs doing once per computer):
```
cd "G:\Shared drives\IAMSYbElectronics\Projects\Power_Supply\OscilloscopeSnapshotTool"
pip install -r requirements.txt
```

## 2. Find the oscilloscope's IP address

On the DS1104Z Plus:
1. Press **Utility** on the front panel.
2. Go to **IO Setting** (or **System → IO**, depending on firmware).
3. Select **LAN** and confirm it shows a real IP address (not `0.0.0.0`).

Make sure the scope and your computer are on the same network.

## 3. Wiring

Probe the power supply output you're testing with the scope's **CH1**
input. The tool always captures CH1 — if you need to probe a different
channel, change the `CHANNEL` constant near the top of `capture.py`.

## 4. Taking a snapshot

There are two ways to run this: the **GUI** (recommended, no typing
commands each time) or the **command line** script.

### GUI

1. Run:
   ```
   python main.py
   ```
   The window has a fixed size, but its content scrolls (mouse wheel, or
   the scrollbar on the right) if it doesn't fully fit - e.g. on a
   smaller screen or with larger system font/DPI scaling.
2. Enter the scope's IP address and click **Test Connection** to confirm
   it's reachable (you should see its `*IDN?` response pop up).
3. Optionally, type in the rail's **expected DC voltage**.
4. Optionally, tick **Also do a long capture** if you want the much
   longer raw trace + FFT this run too — see
   [Long trace capture](#long-trace-capture) below for what that gives
   you and when you'd want it.
5. Click **Take Snapshot**. The progress bars (AC, FFT, DC, and Long
   capture if ticked) show which pass is currently running, then switch
   to "Done" once each finishes. The scope freezes briefly during each
   pass and resumes automatically afterward. If the long capture checkbox
   was left unticked, its progress bar just stays "Idle" the whole time —
   that pass is skipped entirely.
6. Once everything shows "Done", the label below shows the folder it
   saved to, plus the measured DC level (and how it compares to what you
   typed in, if anything).

Click **Take Snapshot** again for each new output/condition you want to
document — every click gets its own timestamped folder, so nothing gets
overwritten.

### Command line (alternative)

```
python capture.py <scope_ip>
```
It'll then prompt for the expected DC voltage and whether to include a
long capture too (press Enter to skip either). Same behavior as the
GUI's Take Snapshot button, just without the window — useful if you want
to script/automate captures yourself.

Either way, output lands in `Snapshots\snapshot_<timestamp>\`, containing:
- `snapshot_<timestamp>_AC.npz` / `_AC.png` — ripple/noise
- `snapshot_<timestamp>_FFT.npz` / `_FFT.png` — frequency spectrum of the ripple
- `snapshot_<timestamp>_DC.npz` / `_DC.png` — actual output level
- `longcapture_<timestamp>\` — only if the long capture was included (see
  below)

All files share the same timestamp, so they're always easy to match up.

## Long trace capture

An optional capture (a PI's suggestion) for cases where the regular
~1200-point AC/FFT passes above aren't enough — it reads back the scope's
**full internal acquisition memory** for CH1 (1,000,000 points, same
fixed AC-coupled scale/offset/timebase as the regular AC pass — this is
meant to be the same ripple signal, just with far more of the actual
acquisition read back instead of only what fits on screen) and computes a
**full-resolution FFT from it in Python** (via numpy, not the scope's own
on-screen MATH FFT — the long record's whole benefit is finer frequency
resolution than a screen-limited display can show).

- **GUI**: tick **Also do a long capture** before clicking **Take
  Snapshot** — it then runs as a fourth pass in the same run, right after
  DC. Leave it unticked for a routine snapshot without the extra time
  this pass adds (transferring a million points over LAN, even in
  binary, isn't instant).
- **Reconnects fresh right before the long capture itself** (a short
  pause, then a brand new connection), rather than reusing the same
  connection the AC/FFT/DC passes just used. Real-hardware testing found
  the long capture's big chunked transfer noticeably more likely to fail
  partway (`VI_ERROR_IO`) on a connection that had already been through
  those three passes' own substantial command traffic, but consistently
  succeeded cleanly on a fresh connection - this gives it that same
  advantage without needing to redo the entire AC/FFT/DC sequence just to
  get one.
- **Command line**: answer "y" to the "Include long capture too?" prompt
  when running `python capture.py <scope_ip>`.
- **Isolated in its own subfolder**, `longcapture_<timestamp>\` (same
  timestamp as the rest of the run), nested inside the main
  `snapshot_<timestamp>\` folder rather than mixed in loose alongside the
  AC/FFT/DC files:
  - `snapshot_<timestamp>_LONG_AC.npz` (time_s, CH1_volts)
  - `snapshot_<timestamp>_LONG_FFT.npz` (frequency_hz, magnitude_V, magnitude_dB)
  - `snapshot_<timestamp>_LONG.png` - a quick-look plot (time-domain trace
    on top, FFT spectrum on bottom) generated directly from the `.npz`
    data with matplotlib. There's no on-scope screenshot for this data
    (nothing meaningful to show on the scope's own screen for a record
    this size - it can't display all 1,000,000 points or compute a
    matching on-screen FFT), so this PNG is the only visual reference for
    a long capture run.
- **Standalone alternative**: if you want *just* the long capture without
  running the full AC/FFT/DC sequence, use `python capture.py <scope_ip>
  --long` instead - this saves into its own separate top-level
  `Snapshots\longcapture_<timestamp>\` folder (not nested under a regular
  snapshot, since there isn't one in this case).
- The actual sample rate/memory depth the scope accepted, and the real
  time span those 1,000,000 points cover, are printed to the console
  (`[Long capture] ...`) each run — worth checking if the frequency range
  in the FFT doesn't look like what you expected.
- The chunked binary transfer itself (4 chunks of 250,000 points each)
  logs each chunk as it completes (`[Long capture read] chunk 1/4 ...`) -
  added after a real-hardware run went silent partway through this
  transfer with no indication of which chunk (or whether the transfer
  itself) had failed. If a chunk fails, the log now says exactly which
  one and what VISA error it hit, before the whole capture retries.
- If the long capture fails partway through, its `longcapture_<timestamp>`
  subfolder is never created at all (rather than being left behind
  empty) - the folder only appears once the raw capture and FFT have
  both actually succeeded and there's real data to save.
- `LONG_TRACE_POINTS` (target point count) and `LONG_TRACE_MDEPTH` (the
  actual memory depth requested from the scope — a fixed list of values on
  this hardware, not an arbitrary number) are both in `capture.py` if you
  need to change them.

## Reading the .npz files in Python

Every waveform (short or long trace) is saved as a compressed NumPy
`.npz`, not CSV — a PI's suggestion, since the long trace especially would
be an unwieldy multi-megabyte text file otherwise. Load one like this:

```python
import numpy as np

data = np.load("snapshot_20260810_120000_AC.npz")
print(list(data.keys()))       # e.g. ['time_s', 'CH1_volts']
times, volts = data["time_s"], data["CH1_volts"]
```

Each file's array names describe what they hold (`time_s`/`CH1_volts` for
AC/DC/long-trace captures, `frequency_hz`/`magnitude_dB` for the scope's
FFT pass, `frequency_hz`/`magnitude_V`/`magnitude_dB` for the long-trace
FFT) — no separate header or documentation needed to know what's inside.

## Notes

- The short-pass `.npz` files capture whatever is **currently on the
  screen** (~1200 points, the same "NORMal" waveform mode the scope itself
  displays) — not the scope's full internal memory depth. This matches
  what you see in the matching screenshot. (For the full internal memory
  depth, see [Long trace capture](#long-trace-capture) above.)
- **FFT is computed by the scope itself** (its built-in MATH FFT feature),
  not by this tool — frequency resolution and range depend on the current
  timebase/sample rate, same as if you'd set it up on the front panel.
  CH1's time-domain trace stays visible alongside the spectrum in the FFT
  screenshot — hiding CH1's display was tried, but real-hardware testing
  suggested the FFT stopped producing a result when its source channel
  wasn't visible, so that's kept on for now. "Measure All" statistics
  can't show anything FFT-specific either way, since the scope's
  measurement engine only supports the physical channels
  (CH1-CH4), never the MATH/FFT result.
- **FFT frequency axis**: on this scope, `:MATH:FFT:HCENter` sets whatever
  frequency sits at the *horizontal center* of the screen (it defaults to
  5MHz) — it does **not** default to putting 0Hz at the left edge, so
  without setting it explicitly, 0Hz could end up sitting well inside the
  plot with a dead, unused gap to its left (confusing to read, and exactly
  what earlier real captures showed). After `:MATH:RESet` auto-fits the
  frequency span (`:MATH:FFT:HSCale`) to the real sample rate, the code
  now repositions the center so **0Hz lands at the left edge** — using as
  much of the plot as possible for the actual spectrum, the same way you'd
  expect a real spectrum analyzer to lay it out. (If the display looks
  like there's no obvious dominant frequency, that's more likely a
  genuinely broadband/noisy signal, or the FFT span from the 10µs/div
  timebase not centering on the frequency you expected, than a display
  layout issue — the console output when running from the command line
  prints the actual HSCale/HCENter used for each run, useful for checking
  what frequency range was actually captured.) The vertical axis gets the
  same treatment: `:MATH:RESet` auto-fits the dB/div scale to the actual
  spectrum, then `:MATH:OFFSet` is explicitly set to 0 afterward, which
  *should* put 0dB at the exact vertical center the same way 0 offset
  centers a regular channel — **but a real capture showed the MATH
  zero-reference marker (the small arrow on the screen's left edge)
  sitting visibly below true center** despite the write going through
  with no error, so that convention doesn't seem to carry over to MATH
  the same way. Still open — the console now also prints the actual
  read-back `:MATH:OFFSet?` value (`[FFT vertical] ...`) so the next real
  run can confirm whether the write itself is taking hold before trying a
  numeric correction.
- The console output (visible when running `python capture.py` directly,
  or in whatever terminal launched the GUI) also prints any errors the
  scope's own `:SYSTem:ERRor?` queue reports — checked after the AC pass,
  the FFT setup, and the DC pass — if something like "Invalid Input!" ever
  shows up on the scope's screen again, check there first for the exact
  rejected command instead of guessing. Real-hardware runs came back
  completely clean across the AC/FFT checkpoints, with "Invalid Input!"
  still showing on screen — meaning it isn't coming from any SCPI command
  in this sequence. Worth checking whether it's a stale message left over
  from something else entirely, or a front-panel/touchscreen glitch from
  nearby EMI, given this runs right next to a power supply board under
  test.
- **This diagnostic caused its own bug once**: the DC pass's
  `:SYSTem:ERRor?` check occasionally timed out outright on real
  hardware. Since that raised the same VisaIOError the LAN reconnect
  logic watches for, `run_capture()`'s retry wrapper caught it and
  restarted the *entire* AC→FFT→DC sequence from scratch — three
  timeouts in a row looked like the tool "got stuck in a loop", leaving
  three dead folders that each only got as far as AC+FFT. Fixed by having
  `_drain_scope_errors()` catch a timeout on its own query and just skip
  that check, instead of letting it take down the whole capture.
- **Fine vs. Coarse vertical scale**: the code now explicitly forces
  `:CHANnel:VERNier ON` (Fine mode) after every coupling switch, before
  setting any scale. Without this, whatever Fine/Coarse state the front
  panel was last manually left in would govern whether a requested scale
  actually got applied exactly - Coarse mode only allows the standard
  1-2-5 step sequence (1mV, 2mV, 5mV, 10mV, 20mV...) and silently snaps
  anything else to the nearest one. This barely matters for the AC pass
  (10mV/div is already a standard step) but matters a lot for the DC
  pass, since `choose_dc_scale()` computes an arbitrary value (e.g.
  1.2V/div for a 12V rail) that Coarse mode would otherwise quietly
  distort.
- **Trigger settings are now fixed too**: sweep mode `AUTO`, edge trigger
  on CH1, level 0V for the AC/FFT/long-trace passes and the expected DC
  voltage (or 0V if none given) for the DC pass. Previously trigger
  settings were never touched at all, meaning they depended entirely on
  whatever was last set manually. If the scope was left in `Normal` sweep
  mode with a level the current signal doesn't reliably cross, it just
  holds onto whatever it last successfully triggered on - possibly stale
  data from a completely different pass or coupling state - instead of
  acquiring anything fresh, which could look like "different results every
  time" between runs with no other explanation. `AUTO` sweep mode avoids
  this failure mode entirely by always producing a fresh acquisition on a
  timeout even without a clean edge. Console lines `[AC trigger]` /
  `[DC trigger]` confirm what was set on each run.
- **Cursors are forced off** (`:CURSor:MODE OFF`) as the very first thing
  every run does, before anything else - the small AX/AY/BX/BY/BX-AX
  readout box left over from manual front-panel use would otherwise sit
  on top of the plot in every screenshot.
- **DC framing internals**: the scope's max allowed vertical offset
  depends on the current scale — under 500mV/div it's only ±2V, but from
  500mV/div up it's ±100V. Since every rail on this board needs more than
  2V of offset to center, the DC scale is never allowed to go below
  500mV/div (`MIN_DC_SCALE_FOR_OFFSET_RANGE` in `capture.py`), even if a
  tighter zoom would otherwise look nicer for a small rail like 3.3V.
- If you get a connection error, double-check the IP address and that
  nothing (e.g. a firewall) is blocking the connection — same
  troubleshooting as the filter sweep tool in the `LPFilters_Elliptical`
  project. Repeated captures can occasionally hit a stale-connection error;
  the tool automatically retries a few times (with a 5-second pause
  between attempts) before giving up. A folder is only created once the
  connection actually succeeds, so a failed retry doesn't leave an empty
  folder behind.
- **`VI_ERROR_RSRC_NFOUND` after a long capture**: real-hardware testing
  found that reconnecting right after a run that included a long capture
  could fail outright with "resource not found" (not just a slow
  response) - the LAN interface seems to need more recovery time after a
  connection that stayed open much longer and moved far more data than
  the routine passes. The retry pause was increased from 1.5s to 5s to
  give it more room; if this still happens, waiting a bit longer before
  retrying manually, or power-cycling the scope's LAN interface (Utility →
  IO Setting), should clear it.
