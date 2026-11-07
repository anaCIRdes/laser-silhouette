# laser-silhouette

CLI-Tool: wandelt Bilder mit Silhouetten (z. B. Logos, Stanzformen, schwarze Formen auf weißem Grund) in laser-optimierte Vektordateien (**SVG** oder **DXF**) um.

## Installation

```bash
pip install -r requirements.txt
```

## Verwendung

```bash
python3 laser_silhouette.py bild.png -o teil.svg
python3 laser_silhouette.py bild.png -o teil.dxf --kerf 0.1
```

## Pipeline (Laser-Optimierung)

1. **Binarisierung** – automatisch (Otsu) oder manueller Schwellwert (`--threshold`), optional invertiert für helle Formen auf dunklem Grund (`--invert`)
2. **Rauschunterdrückung** – Median-Filter (`--blur`)
3. **Konturextraktion** – äußere Formen plus Innenkonturen (Löcher) als geschlossene Polygone
4. **Kantenglättung** – 4×-Hochskalierung + Weichzeichnung für subpixelglatte Kanten, plus Chaikin-Glättung (`--smooth`, Standard 2 Iterationen)
5. **Knotenreduktion** – Douglas-Peucker (`--simplify`, Standard 0,1 mm Toleranz)
6. **Kerf-Kompensation** – `--kerf 0.1` vergrößert die Außenkontur (und verkleinert Löcher) um die halbe Schnittfuge-Breite, damit das Teil Maßhaltigkeit behält
7. **Überlappungs-Merge** – `--merge-overlaps` vereinigt überlappende Formen, um doppelte Schnittlinien zu vermeiden
8. **Schnittreihenfolge** – `--order topleft` (Lesart) oder `inside-out` (Löcher zuerst)

## Optionen

| Option | Default | Bedeutung |
|---|---|---|
| `--px-per-mm` | 10 | Auflösung des Quellbilds → Maßstab des Ergebnisses |
| `--threshold` | auto (Otsu) | manueller Binärisierungs-Schwellwert 1–255 |
| `--invert` | aus | helle Silhouetten auf dunklem Grund |
| `--blur` | 3 | Median-Blur zur Rauschunterdrückung (0 = aus) |
| `--min-area-mm2` | 1 | kleinste übernommene Fläche in mm² |
| `--simplify` | 0.1 | Vereinfachungstoleranz in mm |
| `--smooth` | 2 | Chaikin-Glättungsiterationen (0 = aus) |
| `--kerf` | 0 | Schnittfugen-Kompensation in mm (z. B. 0.1) |
| `--holes` | keep | `discard` verwirft Innenkonturen |
| `--fit-circles` | aus | kreisförmige Teile (z. B. Ballons) als perfekte Kreise exportieren |
| `--circle-min-r` | 2 | Mindestradius der Kreiserkennung in mm |
| `--merge-overlaps` | aus | überlappende Formen zusammenführen |
| `--order` | topleft | Schnittreihenfolge (`topleft`, `inside-out`, `none`) |
| `--dpi-hint` | 96 | DPI-Angabe im SVG |

## Ausgabe

- **SVG**: Pfade in mm, Löcher über `fill-rule="evenodd"`, dünne Schnittlinie
- **DXF**: R2000 ASCII, Einheiten mm (`$INSUNITS=4`), alle Konturen auf Layer `CUT`, geschlossen (`closed=True`)

DXF-Import getestet für LightBurn, LaserGRBL, Inkscape und LibreCAD.
