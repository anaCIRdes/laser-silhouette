#!/usr/bin/env python3
"""GUI for laser_silhouette: convert silhouette images to SVG/DXF."""

from __future__ import annotations

import logging
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import laser_silhouette as core

logger = logging.getLogger("laser_silhouette.gui")

SUPPORTED_INPUTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp", ".gif"}


class ConverterGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Laser Silhouette Converter")
        self.root.minsize(980, 640)

        self.input_path: Path | None = None
        self.preview_photo = None
        self.result_polys = None
        self.result_bounds = None

        self._build_layout()
        self._bind_preview()

    # ---------- UI construction ----------

    def _build_layout(self):
        top = ttk.Frame(self.root)
        top.pack(fill=tk.X, padx=8, pady=6)

        ttk.Button(top, text="Bild öffnen…", command=self.choose_file).pack(side=tk.LEFT)
        self.file_label = ttk.Label(top, text="keine Datei geladen", anchor="w")
        self.file_label.pack(side=tk.LEFT, padx=10, fill=tk.X, expand=True)

        body = ttk.Frame(self.root)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        # left: options
        opts = ttk.LabelFrame(body, text="Optionen", padding=8)
        opts.pack(side=tk.LEFT, fill=tk.Y)

        self.vars: dict[str, tk.Variable] = {}

        def add_entry(label, key, default, width=8, suffix=""):
            row = ttk.Frame(opts)
            row.pack(fill=tk.X, pady=2)
            ttk.Label(row, text=label).pack(side=tk.LEFT)
            var = tk.StringVar(value=str(default))
            self.vars[key] = var
            ttk.Entry(row, textvariable=var, width=width).pack(side=tk.RIGHT)
            if suffix:
                ttk.Label(row, text=suffix).pack(side=tk.RIGHT)

        def add_check(label, key, default):
            var = tk.BooleanVar(value=default)
            self.vars[key] = var
            ttk.Checkbutton(opts, text=label, variable=var).pack(anchor="w", pady=2)

        def add_combo(label, key, values, default):
            row = ttk.Frame(opts)
            row.pack(fill=tk.X, pady=2)
            ttk.Label(row, text=label).pack(side=tk.LEFT)
            var = tk.StringVar(value=default)
            self.vars[key] = var
            ttk.Combobox(
                row, textvariable=var, values=values, state="readonly", width=12
            ).pack(side=tk.RIGHT)

        add_entry("Pixel pro mm", "px_per_mm", 10.0)
        add_entry("Schwellwert (leer=auto)", "threshold", "", width=5)
        add_check("Invertieren (hell auf dunkel)", "invert", False)
        add_entry("Blur (0=aus)", "blur", 3)
        add_entry("Min. Fläche", "min_area_mm2", 1.0, suffix="mm²")
        add_entry("Vereinfachung", "simplify", 0.1, suffix="mm")
        add_entry("Kantenglättung", "smooth", 2, width=3)
        add_entry("Kerf", "kerf", 0.0, suffix="mm")
        add_combo("Löcher", "holes", ["keep", "discard"], "keep")
        add_check("Kreise erkennen (Ballons etc.)", "fit_circles", False)
        add_entry("Min. Kreisradius", "circle_min_r", 2.0, suffix="mm")
        add_check("Überlappungen mergen", "merge_overlaps", False)
        add_combo("Schnittreihenfolge", "order", ["topleft", "inside-out", "none"], "topleft")

        ttk.Separator(opts).pack(fill=tk.X, pady=6)
        ttk.Button(opts, text="Vorschau aktualisieren", command=self.rebuild).pack(fill=tk.X)
        ttk.Button(opts, text="Als SVG exportieren…", command=lambda: self.export(".svg")).pack(
            fill=tk.X, pady=3
        )
        ttk.Button(opts, text="Als DXF exportieren…", command=lambda: self.export(".dxf")).pack(
            fill=tk.X
        )
        self.status = ttk.Label(opts, text="", wraplength=220, justify=tk.LEFT)
        self.status.pack(fill=tk.X, pady=(8, 0))

        # right: preview canvas
        self.canvas = tk.Canvas(body, bg="white")
        self.canvas.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

    def _bind_preview(self):
        for var in self.vars.values():
            var.trace_add("write", lambda *_: self.schedule_rebuild())

        def on_resize(event):
            if self.result_polys is not None:
                self.draw_preview()

        self.canvas.bind("<Configure>", on_resize)

        def on_mousewheel(event):
            self.canvas.yview_scroll(int(-event.delta / 120), "units")

        self.canvas.bind_all("<MouseWheel>", on_mousewheel)

    # ---------- helpers ----------

    def choose_file(self):
        exts = [("Bilder", " ".join("*" + e for e in sorted(SUPPORTED_INPUTS))), ("Alle Dateien", "*.*")]
        path = filedialog.askopenfilename(title="Bild wählen", filetypes=exts)
        if not path:
            return
        p = Path(path)
        if p.suffix.lower() not in SUPPORTED_INPUTS:
            messagebox.showerror(
                "Format nicht unterstützt",
                "Gültige Importformate: PNG, JPG/JPEG, BMP, TIFF, WebP, GIF",
            )
            return
        self.input_path = p
        self.file_label.config(text=str(p))
        self.rebuild()

    def _read_var(self, key, cast, default):
        raw = self.vars[key].get().strip()
        if raw == "":
            return default
        try:
            return cast(raw)
        except ValueError:
            return default

    def make_args(self):
        class A:
            pass

        a = A()
        a.input = self.input_path
        a.px_per_mm = max(self._read_var("px_per_mm", float, 10.0), 0.1)
        a.threshold = self._read_var("threshold", int, None)
        if a.threshold is not None and not (1 <= a.threshold <= 255):
            a.threshold = None
        a.invert = bool(self.vars["invert"].get())
        a.blur = self._read_var("blur", int, 3)
        a.min_area_mm2 = self._read_var("min_area_mm2", float, 1.0)
        a.simplify = self._read_var("simplify", float, 0.1)
        a.smooth = self._read_var("smooth", int, 2)
        a.kerf = self._read_var("kerf", float, 0.0)
        a.holes = self.vars["holes"].get()
        a.merge_overlaps = bool(self.vars["merge_overlaps"].get())
        a.order = self.vars["order"].get()
        a.fit_circles = bool(self.vars["fit_circles"].get())
        a.circle_min_r = self._read_var("circle_min_r", float, 2.0)
        a.dpi_hint = 96
        return a

    # ---------- processing / preview ----------

    def schedule_rebuild(self):
        if getattr(self, "_job", None):
            self.root.after_cancel(self._job)
        self._job = self.root.after_idle(self.rebuild)

    def rebuild(self):
        if self.input_path is None:
            return
        try:
            args = self.make_args()
            polys = core.process(args)
            self.result_polys = polys
            self.result_bounds = core.bounds_mm(polys)
            minx, miny, maxx, maxy = self.result_bounds
            self.status.config(
                text=f"{len(polys)} Form(en)\n"
                f"{maxx - minx:.1f} × {maxy - miny:.1f} mm",
                foreground="black",
            )
        except SystemExit as e:
            self.result_polys = None
            self.status.config(text=str(e), foreground="red")
        except Exception as e:
            self.result_polys = None
            self.status.config(text=f"Fehler: {e}", foreground="red")
        self.draw_preview()

    def draw_preview(self):
        self.canvas.delete("all")
        polys = self.result_polys
        if not polys:
            return
        w = max(self.canvas.winfo_width(), 10)
        h = max(self.canvas.winfo_height(), 10)
        minx, miny, maxx, maxy = self.result_bounds
        gw, gh = maxx - minx, maxy - miny
        if gw <= 0 or gh <= 0:
            return
        margin = 20
        scale = min((w - 2 * margin) / gw, (h - 2 * margin) / gh)

        def tx(x):
            return margin + (x - minx) * scale

        def ty(y):
            return margin + (maxy - y) * scale

        for poly in polys:
            for ring in core.poly_rings(poly):
                pts = [(tx(x), ty(y)) for x, y in ring]
                if len(pts) >= 2:
                    self.canvas.create_polygon(
                        pts, fill="#dbe9ff", outline="#0a3d91", width=1
                    )

    # ---------- export ----------

    def export(self, suffix: str):
        if self.input_path is None:
            messagebox.showinfo("Kein Bild", "Bitte zuerst ein Bild öffnen.")
            return
        if self.result_polys is None and not self.rebuild_ok():
            return
        default = self.input_path.with_suffix(suffix)
        out = filedialog.asksaveasfilename(
            title="Exportieren", defaultextension=suffix, initialfile=str(default)
        )
        if not out:
            return
        out = Path(out)

        def run():
            try:
                if suffix == ".svg":
                    core.write_svg(self.result_polys, out, 96)
                else:
                    core.write_dxf(self.result_polys, out)
                self.root.after(0, lambda: messagebox.showinfo("Fertig", f"Gespeichert:\n{out}"))
            except Exception as e:
                self.root.after(0, lambda e=e: messagebox.showerror("Fehler", str(e)))

        threading.Thread(target=run, daemon=True).start()

    def rebuild_ok(self) -> bool:
        self.rebuild()
        return self.result_polys is not None


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    root = tk.Tk()
    try:
        from ctypes import windll

        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    ConverterGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
