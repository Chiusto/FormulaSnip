"""可选的 LaTeX 渲染预览：用 matplotlib mathtext 把公式渲染成位图。

matplotlib 未安装时 render_latex 返回 None，UI 自动隐藏预览按钮。
mathtext 只支持常用宏（\\frac \\sqrt \\sum \\int 等），复杂环境
（如 \\begin{aligned}）渲染失败时返回 None，主交付仍是 LaTeX 文本。
"""
from __future__ import annotations

import io


def render_latex(latex: str, dpi: int = 170):
    """渲染 LaTeX 公式为 QImage；任何失败返回 None。"""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        from matplotlib import rcParams

        rcParams["mathtext.fontset"] = "stix"
        rcParams["font.family"] = "STIXGeneral"
    except Exception:  # noqa: BLE001
        return None

    s = (latex or "").strip()
    if not s:
        return None
    if s.startswith("$") and s.endswith("$") and len(s) > 2:
        s = s[1:-1]
    expr = f"${s}$"

    try:
        fig = plt.figure(figsize=(10, 1.4), dpi=dpi)
        fig.patch.set_facecolor("white")
        fig.text(0.5, 0.5, expr, ha="center", va="center",
                 fontsize=22, color="black")
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight",
                    pad_inches=0.12, facecolor="white")
        plt.close(fig)
        from PySide6.QtGui import QImage

        img = QImage.fromData(bytes(buf.getvalue()), "PNG")
        return img if not img.isNull() else None
    except Exception:  # noqa: BLE001
        return None


def preview_available() -> bool:
    try:
        import matplotlib  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False
