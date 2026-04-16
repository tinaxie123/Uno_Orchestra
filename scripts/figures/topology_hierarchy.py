"""Generate the topology hierarchy figure for the paper (Section 3, Data Selection)."""

import os
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

fig, axes = plt.subplots(1, 4, figsize=(18, 5.5))
fig.subplots_adjust(wspace=0.08, bottom=0.28, top=0.75, left=0.03, right=0.97)

LEVELS = [
    {
        "name": "Atomic",
        "constraint": "|V| = 1",
        "learns": "When not to\ndecompose",
        "sources": "GSM8K",
        "quota": "500",
        "nodes": [(0.5, 0.5)],
        "edges": [],
    },
    {
        "name": "Path",
        "constraint": "Linear chain, deg ≤ 1",
        "learns": "Sequential\ndelegation",
        "sources": "NuminaMath, HotpotQA",
        "quota": "1.5k each",
        "nodes": [(0.5, 0.85), (0.5, 0.5), (0.5, 0.15)],
        "edges": [(0, 1), (1, 2)],
    },
    {
        "name": "Tree",
        "constraint": "Allow branching (out-deg > 1)",
        "learns": "Explore alternative\ndecomposition paths",
        "sources": "MuSiQue",
        "quota": "1.5k",
        "nodes": [(0.5, 0.85), (0.25, 0.45), (0.75, 0.45), (0.12, 0.1), (0.38, 0.1)],
        "edges": [(0, 1), (0, 2), (1, 3), (1, 4)],
    },
    {
        "name": "DAG",
        "constraint": "Allow fan-in (in-deg > 1)",
        "learns": "Parallel sub-tasks +\ntool chaining",
        "sources": "DROP, TACO, ToolACE",
        "quota": "1.5k + 1.75k each",
        "nodes": [(0.5, 0.9), (0.2, 0.55), (0.8, 0.55), (0.5, 0.55), (0.5, 0.15)],
        "edges": [(0, 1), (0, 2), (0, 3), (1, 4), (2, 4), (3, 4)],
    },
]

NODE_COLOR = "#4A90D9"
EDGE_COLOR = "#555555"
BG_COLORS = ["#f0f7ff", "#e8f4e8", "#fff8e0", "#fce8e8"]
BORDER_COLORS = ["#4A90D9", "#5cb85c", "#f0ad4e", "#d9534f"]

for ax, level, bg, bc in zip(axes, LEVELS, BG_COLORS, BORDER_COLORS):
    ax.set_xlim(-0.15, 1.15)
    ax.set_ylim(-0.15, 1.15)
    ax.set_aspect("equal")
    ax.axis("off")

    # Background box
    rect = mpatches.FancyBboxPatch(
        (-0.1, -0.1), 1.2, 1.2,
        boxstyle="round,pad=0.05", facecolor=bg, edgecolor=bc, linewidth=2.5
    )
    ax.add_patch(rect)

    # Draw edges
    for i, j in level["edges"]:
        x0, y0 = level["nodes"][i]
        x1, y1 = level["nodes"][j]
        ax.annotate(
            "", xy=(x1, y1), xytext=(x0, y0),
            arrowprops=dict(
                arrowstyle="-|>", color=EDGE_COLOR, lw=1.8,
                shrinkA=10, shrinkB=10,
            ),
        )

    # Draw nodes
    for x, y in level["nodes"]:
        circle = plt.Circle((x, y), 0.08, color=NODE_COLOR, ec="white", lw=1.5, zorder=5)
        ax.add_patch(circle)

    # Topology name — above the box
    ax.text(0.5, 1.22, level["name"], ha="center", va="bottom",
            fontsize=17, fontweight="bold", color=bc, transform=ax.transAxes)

    # Constraint — smaller, below the name
    ax.text(0.5, 1.15, level["constraint"], ha="center", va="bottom",
            fontsize=9, color="#777777", style="italic", transform=ax.transAxes)

    # "Router learns" — below the box
    ax.text(0.5, -0.12, level["learns"], ha="center", va="top",
            fontsize=9.5, color="#333333", transform=ax.transAxes)

    # Sources — bold, colored
    ax.text(0.5, -0.32, level["sources"], ha="center", va="top",
            fontsize=10, fontweight="bold", color=bc, transform=ax.transAxes)

    # Quota — gray
    ax.text(0.5, -0.42, f"n = {level['quota']}", ha="center", va="top",
            fontsize=9, color="#999999", transform=ax.transAxes)

# Bottom arrow
fig.patches.append(mpatches.FancyArrowPatch(
    (0.06, 0.10), (0.96, 0.10),
    arrowstyle="-|>", mutation_scale=20,
    color="#aaaaaa", lw=1.5,
    transform=fig.transFigure, clip_on=False,
))
fig.text(0.51, 0.06, "Increasing topological complexity  →", ha="center", va="center",
         fontsize=11, color="#aaaaaa", style="italic")

# Suptitle
fig.suptitle(
    "Training data selection by reasoning topology (Besta et al., 2024)",
    fontsize=14, y=0.98, fontweight="bold", color="#333333",
)

out_path = "docs/figures/topology_hierarchy.pdf"
os.makedirs(os.path.dirname(out_path), exist_ok=True)
fig.savefig(out_path, dpi=300, bbox_inches="tight")
fig.savefig(out_path.replace(".pdf", ".png"), dpi=300, bbox_inches="tight")
print(f"Saved: {out_path} and .png")
plt.close()
