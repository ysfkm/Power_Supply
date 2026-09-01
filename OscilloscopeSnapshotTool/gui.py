"""Tkinter GUI for the power supply snapshot tool - wraps capture.py so you
don't need to type a command in a terminal every time you want a snapshot.
"""

import os
import queue
import threading

import tkinter as tk
from tkinter import ttk, messagebox

import pyvisa

import capture


class SnapshotApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Power Supply Snapshot Tool")
        self.geometry("560x420")
        self.resizable(False, False)

        self.result_queue = queue.Queue()
        self.worker_thread = None
        self._animation_jobs = {"ac": None, "fft": None, "dc": None, "long": None}

        self._build_scroll_container()
        self._build_widgets()
        self._poll_queue()

    # -- scrolling ---------------------------------------------------------

    def _build_scroll_container(self):
        """Wraps everything in a Canvas + Scrollbar, with mouse-wheel
        scrolling - the window is a fixed size (resizable(False, False)
        above), but content can still end up taller than that (larger
        system font/DPI scaling, or just more rows added over time), which
        would otherwise clip the bottom of the window with no way to
        reach it. Widgets get built into self.content (a plain ttk.Frame)
        instead of directly into `self` from here on.
        """
        container = ttk.Frame(self)
        container.pack(fill="both", expand=True)

        self._canvas = tk.Canvas(container, highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=scrollbar.set)
        self._canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.content = ttk.Frame(self._canvas)
        self._content_window = self._canvas.create_window((0, 0), window=self.content, anchor="nw")

        self.content.bind("<Configure>", self._on_content_configure)
        self._canvas.bind("<Configure>", self._on_canvas_configure)

        # Only scroll with the wheel while the pointer is actually over
        # this window's canvas, rather than binding globally for the
        # whole Tk application the entire time.
        self._canvas.bind("<Enter>", lambda e: self._canvas.bind_all("<MouseWheel>", self._on_mousewheel))
        self._canvas.bind("<Leave>", lambda e: self._canvas.unbind_all("<MouseWheel>"))

    def _on_content_configure(self, _event):
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        # Keep the inner content frame exactly as wide as the visible
        # canvas, so widgets packed with fill="x" still stretch the same
        # way they would have directly inside the window.
        self._canvas.itemconfigure(self._content_window, width=event.width)

    def _on_mousewheel(self, event):
        # Windows delivers <MouseWheel> with event.delta in multiples of
        # 120 per notch.
        self._canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    # -- UI construction -----------------------------------------------

    def _build_widgets(self):
        conn_frame = ttk.LabelFrame(self.content, text="Oscilloscope connection (LAN)")
        conn_frame.pack(fill="x", padx=8, pady=6)

        ttk.Label(conn_frame, text="Scope IP:").grid(row=0, column=0, sticky="w", padx=4, pady=6)
        self.ip_var = tk.StringVar(value="192.168.1.50")
        ttk.Entry(conn_frame, textvariable=self.ip_var, width=18).grid(row=0, column=1, padx=4, pady=6)

        self.test_button = ttk.Button(conn_frame, text="Test Connection", command=self._on_test_connection)
        self.test_button.grid(row=0, column=2, padx=8, pady=6)

        self.conn_status_var = tk.StringVar(value="Not tested")
        ttk.Label(conn_frame, textvariable=self.conn_status_var).grid(row=0, column=3, padx=4, pady=6, sticky="w")

        ttk.Label(conn_frame, text="Expected DC voltage (V):").grid(row=1, column=0, sticky="w", padx=4, pady=6)
        self.expected_dc_var = tk.StringVar(value="")
        ttk.Entry(conn_frame, textvariable=self.expected_dc_var, width=10).grid(
            row=1, column=1, padx=4, pady=6, sticky="w"
        )
        ttk.Label(conn_frame, text="(optional - helps set a safe scale and flags if it's off)").grid(
            row=1, column=2, columnspan=2, padx=4, pady=6, sticky="w"
        )

        self.include_long_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            conn_frame,
            text=f"Also do a long capture ({capture.LONG_TRACE_POINTS:,} points + FFT, "
            "saved in a subfolder - takes longer)",
            variable=self.include_long_var,
        ).grid(row=2, column=0, columnspan=4, sticky="w", padx=4, pady=(0, 6))

        progress_frame = ttk.LabelFrame(self.content, text="Snapshot progress")
        progress_frame.pack(fill="x", padx=8, pady=6)

        ttk.Label(progress_frame, text="AC (ripple/noise):").grid(row=0, column=0, sticky="w", padx=4, pady=8)
        self.ac_progress = ttk.Progressbar(progress_frame, mode="determinate", length=260, maximum=100)
        self.ac_progress.grid(row=0, column=1, padx=4, pady=8)
        self.ac_status_var = tk.StringVar(value="Idle")
        ttk.Label(progress_frame, textvariable=self.ac_status_var, width=10).grid(row=0, column=2, padx=4)

        ttk.Label(progress_frame, text="FFT (spectrum):").grid(row=1, column=0, sticky="w", padx=4, pady=8)
        self.fft_progress = ttk.Progressbar(progress_frame, mode="determinate", length=260, maximum=100)
        self.fft_progress.grid(row=1, column=1, padx=4, pady=8)
        self.fft_status_var = tk.StringVar(value="Idle")
        ttk.Label(progress_frame, textvariable=self.fft_status_var, width=10).grid(row=1, column=2, padx=4)

        ttk.Label(progress_frame, text="DC (output level):").grid(row=2, column=0, sticky="w", padx=4, pady=8)
        self.dc_progress = ttk.Progressbar(progress_frame, mode="determinate", length=260, maximum=100)
        self.dc_progress.grid(row=2, column=1, padx=4, pady=8)
        self.dc_status_var = tk.StringVar(value="Idle")
        ttk.Label(progress_frame, textvariable=self.dc_status_var, width=10).grid(row=2, column=2, padx=4)

        ttk.Label(progress_frame, text="Long capture (optional):").grid(
            row=3, column=0, sticky="w", padx=4, pady=8
        )
        self.long_progress = ttk.Progressbar(progress_frame, mode="determinate", length=260, maximum=100)
        self.long_progress.grid(row=3, column=1, padx=4, pady=8)
        self.long_status_var = tk.StringVar(value="Idle")
        ttk.Label(progress_frame, textvariable=self.long_status_var, width=10).grid(row=3, column=2, padx=4)

        control_frame = ttk.Frame(self.content)
        control_frame.pack(fill="x", padx=8, pady=10)

        self.capture_button = ttk.Button(control_frame, text="Take Snapshot", command=self._on_capture)
        self.capture_button.pack(side="left", padx=4)

        self.result_var = tk.StringVar(value="")
        ttk.Label(self.content, textvariable=self.result_var, wraplength=520, justify="left").pack(
            fill="x", padx=8, pady=4
        )

    # -- helpers ---------------------------------------------------------

    def _bar_and_var(self, stage):
        if stage == "ac":
            return self.ac_progress, self.ac_status_var
        if stage == "fft":
            return self.fft_progress, self.fft_status_var
        if stage == "long":
            return self.long_progress, self.long_status_var
        return self.dc_progress, self.dc_status_var

    def _set_stage(self, stage, status):
        bar, var = self._bar_and_var(stage)
        self._stop_fill_animation(stage)

        if status == "pending":
            bar["value"] = 0
            var.set("Idle")
        elif status == "in_progress":
            bar["value"] = 0
            var.set("Working...")
            self._animate_fill(stage)
        elif status == "done":
            bar["value"] = 100
            var.set("Done")

    def _animate_fill(self, stage):
        """Manually drives a determinate progress bar filling up and looping,
        instead of relying on ttk's built-in 'indeterminate' mode - that mode
        renders as a smoothly sliding block on most systems, but shows up as
        a glitchy flash on some Windows theme/DPI combinations. This gives
        full control over the look regardless of that.
        """
        bar, _ = self._bar_and_var(stage)
        bar["value"] = (bar["value"] + 4) % 100
        self._animation_jobs[stage] = self.after(40, lambda: self._animate_fill(stage))

    def _stop_fill_animation(self, stage):
        job = self._animation_jobs.get(stage)
        if job is not None:
            self.after_cancel(job)
            self._animation_jobs[stage] = None

    # -- button handlers --------------------------------------------------

    def _on_test_connection(self):
        ip = self.ip_var.get().strip()
        try:
            rm = pyvisa.ResourceManager("@py")
            inst = rm.open_resource(capture._resource_string(ip))
            inst.timeout = 3000
            idn = inst.query("*IDN?").strip()
            inst.close()
            rm.close()
            self.conn_status_var.set("Connected OK")
            messagebox.showinfo("Connection test", idn)
        except Exception as exc:
            self.conn_status_var.set("Connection failed")
            messagebox.showerror("Connection test failed", str(exc))

    def _on_capture(self):
        ip = self.ip_var.get().strip()
        if not ip:
            messagebox.showerror("Missing IP", "Enter the scope's IP address first.")
            return

        expected_str = self.expected_dc_var.get().strip()
        expected_dc_voltage = None
        if expected_str:
            try:
                expected_dc_voltage = float(expected_str)
            except ValueError:
                messagebox.showerror("Invalid value", "Expected DC voltage must be a number (or left blank).")
                return

        include_long_capture = self.include_long_var.get()

        self.capture_button.config(state="disabled")
        self.result_var.set("")
        self._set_stage("ac", "pending")
        self._set_stage("fft", "pending")
        self._set_stage("dc", "pending")
        # Reset to "Idle" regardless of whether it's actually used this run -
        # it just stays at Idle the whole time if the checkbox is off, since
        # capture.py only calls progress_callback("long", ...) when
        # include_long_capture is True.
        self._set_stage("long", "pending")

        def progress_callback(stage, status):
            self.result_queue.put(("progress", stage, status))

        def worker():
            try:
                run_folder, measured_dc = capture.run_capture(
                    ip, expected_dc_voltage=expected_dc_voltage,
                    include_long_capture=include_long_capture, progress_callback=progress_callback
                )
                self.result_queue.put(
                    ("done", (run_folder, measured_dc, expected_dc_voltage, include_long_capture), None)
                )
            except Exception as exc:
                self.result_queue.put(("error", str(exc), None))

        self.worker_thread = threading.Thread(target=worker, daemon=True)
        self.worker_thread.start()

    # -- background-thread -> UI bridge -----------------------------------

    def _poll_queue(self):
        try:
            while True:
                kind, a, b = self.result_queue.get_nowait()
                if kind == "progress":
                    self._set_stage(a, b)
                elif kind == "done":
                    self.capture_button.config(state="normal")
                    self.result_var.set(self._format_result(*a))
                elif kind == "error":
                    self.capture_button.config(state="normal")
                    self._set_stage("ac", "pending")
                    self._set_stage("fft", "pending")
                    self._set_stage("dc", "pending")
                    self._set_stage("long", "pending")
                    messagebox.showerror("Capture failed", a)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _format_result(self, run_folder, measured_dc, expected_dc_voltage, include_long_capture):
        lines = [f"Saved to: {run_folder}"]
        if include_long_capture:
            lines.append("(includes a long capture, in its own longcapture_<timestamp> subfolder)")
        if measured_dc != measured_dc:  # NaN check without importing math
            lines.append("Measured DC level: invalid/unavailable")
        elif expected_dc_voltage:
            diff = measured_dc - expected_dc_voltage
            direction = "higher than" if diff > 0 else "lower than" if diff < 0 else "exactly at"
            lines.append(
                f"Measured DC level: {measured_dc:.4f} V "
                f"({abs(diff):.4f} V {direction} expected {expected_dc_voltage:.4f} V)"
            )
        else:
            lines.append(f"Measured DC level: {measured_dc:.4f} V")
        return "\n".join(lines)
