"""Render the ADR-0012 figure from measured results (docs/anytime-results.json).

Reads the JSON `anytime_experiment.py` writes and draws the two panels the README leads
with, so the picture cannot drift from the numbers — nothing here retypes a value.

Two files come out, light and dark, because a README is read in both GitHub themes and a
PNG cannot adapt on its own:

    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="docs/anytime-dark.png">
      <img src="docs/anytime-light.png" alt="...">
    </picture>

Drawing happens in two phases. Axes are laid out first, then `tight_layout` fixes the
bounding boxes, and only then are the marks drawn — a bar's rounded end has to measure the
same in both directions, and the conversion from pixels to data units is different per axis
and unknown until the layout is settled.

Unlike the rest of eval/, this needs matplotlib — it produces documentation, not a gate,
and nothing in CI depends on it.

    python anytime_experiment.py --json ../docs/anytime-results.json
    python plot_anytime.py
"""
import argparse
import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt                                        # noqa: E402
from matplotlib.path import Path as MplPath                            # noqa: E402
from matplotlib.patches import PathPatch                               # noqa: E402

# Validated categorical slots 1 and 2, stepped per mode (references/palette.md).
THEMES = {
    "light": {
        "surface": "#fcfcfb", "text": "#0b0b0b", "muted": "#52514e",
        "grid": "#e6e6e2", "series": ("#2a78d6", "#eb6834"),
    },
    "dark": {
        "surface": "#1a1a19", "text": "#ffffff", "muted": "#c3c2b7",
        "grid": "#33332f", "series": ("#3987e5", "#d95926"),
    },
}
NOMINAL = 5.0          # the 95% interval's advertised error budget, in percent
# Mark specs are given in *display* pixels, and a README scales this PNG down to roughly a
# third of its rendered width. Everything below is therefore multiplied through by that
# ratio, so a 4px corner and a 24px bar cap are 4px and 24px where someone reads them
# rather than invisible hairlines.
FIGURE_WIDTH_IN, DPI = 13.5, 190
DISPLAY_WIDTH_PX = 950
SCALE = (FIGURE_WIDTH_IN * DPI) / DISPLAY_WIDTH_PX
CORNER_PX = 4.0 * SCALE      # rounded data-end
GAP_PX = 2.0 * SCALE         # the surface gap between adjacent bars
BAR_CAP_PX = 24.0 * SCALE    # never fill the slot; the leftover is air


def px_to_data(ax, pixels: float) -> tuple[float, float]:
    """Pixels as (x, y) data units — different per axis, so bars round evenly."""
    inverse = ax.transData.inverted()
    (x0, y0), (x1, y1) = inverse.transform([(0.0, 0.0), (pixels, pixels)])
    return abs(x1 - x0), abs(y1 - y0)


def rounded_bar(ax, left: float, width: float, height: float, color: str,
                rx: float, ry: float) -> None:
    """A bar with a rounded data-end and a square baseline (marks-and-anatomy)."""
    rx = min(rx, width / 2)
    ry = min(ry, abs(height))
    verts = [(left, 0), (left, height - ry), (left, height), (left + rx, height),
             (left + width - rx, height), (left + width, height),
             (left + width, height - ry), (left + width, 0), (left, 0)]
    codes = [MplPath.MOVETO, MplPath.LINETO, MplPath.CURVE3, MplPath.CURVE3,
             MplPath.LINETO, MplPath.CURVE3, MplPath.CURVE3, MplPath.LINETO,
             MplPath.CLOSEPOLY]
    ax.add_patch(PathPatch(MplPath(verts, codes), facecolor=color, edgecolor="none",
                           zorder=3))


def style_axes(ax, theme: dict) -> None:
    ax.set_facecolor(theme["surface"])
    ax.patch.set_edgecolor("none")
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(theme["grid"])
    ax.spines["bottom"].set_linewidth(1.0)
    ax.tick_params(colors=theme["muted"], length=0, labelsize=9.5)
    ax.grid(axis="y", color=theme["grid"], linewidth=1.0, linestyle="-", zorder=0)
    ax.set_axisbelow(True)


def titled(ax, theme: dict, title: str, subtitle: str) -> None:
    ax.set_title(title, fontsize=13, fontweight="bold", color=theme["text"],
                 pad=34, loc="left")
    ax.text(0, 1.055, subtitle, transform=ax.transAxes, fontsize=10,
            color=theme["muted"])


# --------------------------------------------------------------------------------------
# panel 1 — what peeking costs
# --------------------------------------------------------------------------------------

def peeking_series(rows: list[dict]) -> tuple[list[int], list[float], list[float]]:
    lengths = sorted({row["queries"] for row in rows})
    fixed, anytime = [], []
    for length in lengths:
        cell = [row for row in rows if row["queries"] == length]
        fixed.append(100 * sum(r["fixed_n_escape_rate"] for r in cell) / len(cell))
        anytime.append(100 * sum(r["anytime_escape_rate"] for r in cell) / len(cell))
    return lengths, fixed, anytime


def setup_peeking(ax, rows: list[dict], theme: dict) -> None:
    lengths, fixed, _ = peeking_series(rows)
    style_axes(ax, theme)
    ax.set_xticks(range(len(lengths)))
    ax.set_xticklabels([str(n) for n in lengths])
    ax.set_xlabel("queries watched", fontsize=10, color=theme["muted"], labelpad=9)
    ax.set_ylabel("runs where the true score escaped", fontsize=10,
                  color=theme["muted"], labelpad=9)
    ax.set_ylim(0, max(fixed) * 1.32)
    ax.set_xlim(-0.66, len(lengths) - 0.34)
    ax.set_yticks([0, 10, 20, 30])
    ax.set_yticklabels(["0", "10%", "20%", "30%"])
    titled(ax, theme, "A 95% interval, checked after every query",
           "the longer you look, the more often it is wrong")


def draw_peeking(ax, rows: list[dict], theme: dict) -> None:
    lengths, fixed, anytime = peeking_series(rows)
    gap_x, _ = px_to_data(ax, GAP_PX)
    rx, ry = px_to_data(ax, CORNER_PX)
    cap_x, _ = px_to_data(ax, BAR_CAP_PX)
    width = min((0.66 - gap_x) / 2, cap_x)
    _, label_pad = px_to_data(ax, 7 * SCALE)

    for i, (a, b) in enumerate(zip(fixed, anytime)):
        rounded_bar(ax, i - width - gap_x / 2, width, a, theme["series"][0], rx, ry)
        rounded_bar(ax, i + gap_x / 2, width, b, theme["series"][1], rx, ry)
        ax.text(i - width / 2 - gap_x / 2, a + label_pad, f"{a:.0f}%", ha="center",
                va="bottom", fontsize=11, fontweight="bold", color=theme["text"])
        # These values sit just under the 5% rule, so the label carries a surface
        # backing rather than being crossed out by it.
        ax.text(i + width / 2 + gap_x / 2, b + label_pad, f"{b:.1f}%", ha="center",
                va="bottom", fontsize=10, color=theme["muted"], zorder=4,
                bbox=dict(facecolor=theme["surface"], edgecolor="none", pad=1.5))

    ax.axhline(NOMINAL, color=theme["muted"], linewidth=1.0, linestyle=(0, (4, 3)),
               zorder=2)
    ax.text(len(lengths) - 0.42, NOMINAL + label_pad, "the 5% it promised", ha="right",
            va="bottom", fontsize=9, color=theme["muted"])

    handles = [plt.Line2D([], [], marker="s", linestyle="none", markersize=9,
                          color=theme["series"][0], label="fixed-N interval"),
               plt.Line2D([], [], marker="s", linestyle="none", markersize=9,
                          color=theme["series"][1], label="confidence sequence")]
    legend = ax.legend(handles=handles, loc="upper left", frameon=False, fontsize=10,
                       handletextpad=0.6, borderpad=0, labelspacing=0.4)
    for text in legend.get_texts():
        text.set_color(theme["muted"])


# --------------------------------------------------------------------------------------
# panel 2 — what stopping early saves
# --------------------------------------------------------------------------------------

def setup_stopping(ax, rows: list[dict], theme: dict) -> None:
    rows = sorted(rows, key=lambda r: r["true_mean"])
    budget, threshold = rows[0]["budget"], rows[0]["threshold"]
    style_axes(ax, theme)
    ax.set_xticks(range(len(rows)))
    ax.set_xticklabels([f"{r['true_mean']:.2f}" for r in rows])
    for tick, row in zip(ax.get_xticklabels(), rows):
        if abs(row["true_mean"] - threshold) < 1e-9:
            tick.set_color(theme["text"])
            tick.set_fontweight("bold")
    ax.set_xlabel(f"the system's true Recall@5   (gate asks: is it above {threshold}?)",
                  fontsize=10, color=theme["muted"], labelpad=9)
    ax.set_ylabel("queries actually scored", fontsize=10, color=theme["muted"], labelpad=9)
    ax.set_ylim(0, budget * 1.42)
    ax.set_xlim(-0.95, len(rows) - 0.05)
    ax.set_yticks([0, 100, 200, 300])
    titled(ax, theme, "So the gate can stop as soon as it knows",
           "a clearly good or clearly broken system is cheap to judge")


def draw_stopping(ax, rows: list[dict], theme: dict) -> None:
    rows = sorted(rows, key=lambda r: r["true_mean"])
    budget = rows[0]["budget"]
    spent = [r["mean_stop"] for r in rows]
    rx, ry = px_to_data(ax, CORNER_PX)
    cap_x, _ = px_to_data(ax, BAR_CAP_PX)
    _, label_pad = px_to_data(ax, 9 * SCALE)
    width = min(0.62, cap_x)

    for i, value in enumerate(spent):
        rounded_bar(ax, i - width / 2, width, value, theme["series"][0], rx, ry)

    ax.axhline(budget, color=theme["muted"], linewidth=1.0, linestyle=(0, (4, 3)),
               zorder=2)
    # Below the line, so the budget label never reads as a value above it.
    ax.text(len(spent) - 0.55, budget - label_pad, f"{budget}-query budget", ha="right",
            va="top", fontsize=9, color=theme["muted"])

    # Direct-label only the story: the two cheap ends, and the expensive middle.
    peak = max(range(len(spent)), key=lambda i: spent[i])
    ax.text(-0.6, spent[0] + label_pad, f"{1 - spent[0] / budget:.0%}\nsaved", ha="left",
            va="bottom", fontsize=11, fontweight="bold", color=theme["text"],
            linespacing=1.3)
    ax.text(len(spent) - 0.45, spent[-1] + label_pad, f"{1 - spent[-1] / budget:.0%}\nsaved",
            ha="right", va="bottom", fontsize=11, fontweight="bold", color=theme["text"],
            linespacing=1.3)
    ax.annotate("at the line —\nnothing to save",
                xy=(peak, spent[peak]), xytext=(peak, budget * 1.19),
                ha="center", va="bottom", fontsize=9.5, color=theme["muted"],
                linespacing=1.35,
                arrowprops=dict(arrowstyle="-", color=theme["grid"], linewidth=1.0,
                                shrinkA=2, shrinkB=4))


def render(results: dict, mode: str, out: Path) -> None:
    theme = THEMES[mode]
    fig, axes = plt.subplots(1, 2, figsize=(FIGURE_WIDTH_IN, 5.4))
    fig.patch.set_facecolor(theme["surface"])

    setup_peeking(axes[0], results["peeking"], theme)
    setup_stopping(axes[1], results["gates"], theme)
    fig.tight_layout(rect=(0.004, 0.06, 0.996, 0.97), w_pad=4.5)

    # Marks after layout: the pixel-to-data conversion is only right once the axes
    # bounding boxes are final, and a rounded end that ignores it comes out lopsided.
    draw_peeking(axes[0], results["peeking"], theme)
    draw_stopping(axes[1], results["gates"], theme)

    fig.text(0.008, 0.018,
             f"{results['streams']:,} simulated evaluation streams per cell, seeded  ·  "
             f"reproduce: python eval/anytime_experiment.py  ·  ADR-0012",
             fontsize=8.5, color=theme["muted"])
    fig.savefig(out, dpi=DPI, facecolor=theme["surface"])
    plt.close(fig)
    print(f"wrote {out}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--results", default="../docs/anytime-results.json")
    parser.add_argument("--out-dir", default="../docs")
    args = parser.parse_args()

    results = json.loads(Path(args.results).read_text(encoding="utf-8"))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for mode in THEMES:
        render(results, mode, out_dir / f"anytime-{mode}.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
