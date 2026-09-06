"""配置管理：读写 ~/.formulasnip/config.json。"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, fields
from pathlib import Path

APP_DIR = Path.home() / ".formulasnip"
CONFIG_PATH = APP_DIR / "config.json"
LOG_PATH = APP_DIR / "app.log"


def init_logging(level: int = logging.INFO) -> None:
    """初始化文件日志（~/.formulasnip/app.log）。GUI 后台启动时也一定有迹可查。"""
    APP_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        filename=str(LOG_PATH),
        encoding="utf-8",
        force=True,
    )


# 内置默认提示词：只描述识别行为，不涉及任何输出格式——
# 格式（包裹定界符 / 多行 / 是否携带原文）一律由设置注入，避免两处规则打架。
DEFAULT_PROMPT = """你是一个严谨的数学公式识别助手。请识别截图中的全部数学公式（印刷体或手写体均可），并输出 LaTeX 代码。

要求：
1. 若公式书写不规范（缺括号、连写等），按标准数学语义补全为规范 LaTeX；
2. 遇到模糊不清的内容宁可保守处理，不要臆造符号。
"""

# 内置多模态信息提取提示词：「提取图片信息」按钮专用——把图中全部信息转成
# 结构化文本，供无视觉能力的模型作为"视觉输入"使用。
DEFAULT_EXTRACT_PROMPT = """你是图片信息提取助手。请完整提取并结构化描述这张图片中的全部信息，输出为 Markdown 文本，供无视觉能力的模型作为视觉输入使用。

要求：
1. 文字内容：逐字转录图中全部文字，保持原有结构与阅读顺序，不要遗漏和改写；
2. 数学公式：用 LaTeX 表示，行内公式用 \\(...\\) 包裹，行间公式用 \\[...\\] 包裹；
3. 表格：转成 Markdown 表格；
4. 图表：说明图表类型、坐标轴、图例与单位，并尽量读出关键数据点；
5. 界面/图形：描述布局、控件、颜色等有意义的视觉要素；
6. 只输出提取结果本身，不要添加任何与图片无关的评论。
"""

# LaTeX 包裹格式 → 注入提示词的指令
LATEX_DELIM_INSTR = {
    "codeblock": "把每一段 LaTeX 公式放入 ```latex 围栏代码块中，代码块之外不要输出任何文字。",
    "dollar":    "用 $$...$$ 包裹每一个行间公式，公式之外不要输出任何文字。",
    "bracket":   r"用 \[...\] 包裹每一个行间公式，公式之外不要输出任何文字。",
    "paren":     r"用 \(...\) 包裹公式，每个公式独立一行，公式之外不要输出任何文字。",
    "inline":    r"用 $...$ 包裹公式，每个公式独立一行，公式之外不要输出任何文字。",
}

# 携带原文模式（latex_with_text=True）→ 公式嵌入原文原位置，文字公式混排
LATEX_DELIM_INSTR_WITH_TEXT = {
    "codeblock": "输出截图中的完整原文：普通文字按原样输出，每个数学公式放入 ```latex 围栏代码块并嵌在原位置，保持截图中的阅读顺序与分段。",
    "dollar":    r"输出截图中的完整原文：普通文字按原样输出，数学公式用 $$...$$ 包裹并嵌在原位置，保持截图中的阅读顺序与分段。",
    "bracket":   r"输出截图中的完整原文：普通文字按原样输出，数学公式用 \[...\] 包裹并嵌在原位置，保持截图中的阅读顺序与分段。",
    "paren":     r"输出截图中的完整原文：普通文字按原样输出，数学公式用 \(...\) 包裹并嵌在原位置，保持截图中的阅读顺序与分段。",
    "inline":    r"输出截图中的完整原文：普通文字按原样输出，数学公式用 $...$ 包裹并嵌在原位置，保持截图中的阅读顺序与分段。",
}

# 多行公式处理 → 注入提示词的指令
LATEX_MULTILINE_INSTR = {
    "lines": "若截图包含多个或跨行公式，请逐行分别输出，保持截图中的先后顺序。",
    "align": r"若截图包含多行公式，请把它们合并进一个 align 环境：用 & 对齐、\\ 换行，不要拆成多段。",
}

_LATEX_DELIM_LABELS = [
    ("```latex 代码块（Typora/Overleaf 通用）", "codeblock"),
    ("$$...$$ 美元双符号", "dollar"),
    (r"\[...\] 方括号（标准 LaTeX）", "bracket"),
    (r"\(...\) 圆括号（行内 LaTeX）", "paren"),
    (r"$...$ 单美元（行内风格）", "inline"),
]

_LATEX_MULTILINE_LABELS = [
    ("逐行分段输出", "lines"),
    ("align 对齐环境（合并多行）", "align"),
]


@dataclass
class AppConfig:
    """应用配置。base_url 填 OpenAI 兼容服务的根地址，如 https://api.openai.com/v1。"""

    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o-mini"
    api_style: str = "openai"   # openai | openai-responses | anthropic
    proxy: str = ""             # ""=跟随系统环境变量；direct=强制直连；其他=代理地址
    latex_delim: str = "codeblock"    # 公式包裹格式（见 LATEX_DELIM_INSTR）
    latex_multiline: str = "lines"    # 多行公式处理（见 LATEX_MULTILINE_INSTR）
    latex_keep_delim: bool = True     # 复制时保留定界符（如 \(...\)、$$...$$）
    latex_with_text: bool = False     # 携带原文：文字公式混排，公式嵌在原位置
    enable_thinking: bool = False     # 深度思考（推理模式，按协议发送对应参数）
    prompt: str = DEFAULT_PROMPT
    max_tokens: int = 2048
    temperature: float = 0.1
    timeout: int = 120          # 单次请求超时（秒）
    max_image_side: int = 1600  # 发送前图片最长边像素上限

    def is_ready(self) -> bool:
        return bool(self.base_url.strip() and self.api_key.strip() and self.model.strip())

    def chat_endpoint(self) -> str:
        base = self.base_url.strip().rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"

    def messages_endpoint(self) -> str:
        """Anthropic 协议端点：兼容 base_url 带/不带 /v1 的写法。"""
        base = self.base_url.strip().rstrip("/")
        if base.endswith("/messages"):
            return base
        if base.endswith("/v1"):
            return base + "/messages"
        return base + "/v1/messages"

    def responses_endpoint(self) -> str:
        """OpenAI Responses 协议端点：兼容 base_url 带/不带 /v1 的写法。"""
        base = self.base_url.strip().rstrip("/")
        if base.endswith("/responses"):
            return base
        return base + "/responses"

    def effective_prompt(self) -> str:
        """用户提示词 + 按格式设置注入的输出要求（格式设置优先级最高）。"""
        base = self.prompt.strip() or DEFAULT_PROMPT
        delim = self.latex_delim if self.latex_delim in LATEX_DELIM_INSTR else "codeblock"
        multi = self.latex_multiline if self.latex_multiline in LATEX_MULTILINE_INSTR else "lines"
        if getattr(self, "latex_with_text", False):
            # 携带原文模式：文字公式混排，align 合并指令与"公式外无文字"指令不再适用
            return "\n".join([
                base,
                "",
                "【输出格式（以下要求优先于上文中的任何输出格式描述）】",
                "- " + LATEX_DELIM_INSTR_WITH_TEXT[delim],
            ])
        return "\n".join([
            base,
            "",
            "【输出格式（以下要求优先于上文中的任何输出格式描述）】",
            "- " + LATEX_DELIM_INSTR[delim],
            "- " + LATEX_MULTILINE_INSTR[multi],
        ])

    @classmethod
    def from_dict(cls, d: dict) -> "AppConfig":
        known = {f.name for f in fields(cls)}
        cfg = cls()
        for k, v in (d or {}).items():
            if k in known:
                setattr(cfg, k, v)
        return cfg

    def to_dict(self) -> dict:
        return asdict(self)


class ConfigStore:
    """负责配置的加载与保存。API Key 以明文保存在用户目录，注意保管。"""

    def __init__(self, path: Path = CONFIG_PATH):
        self.path = path

    def load(self) -> AppConfig:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return AppConfig.from_dict(data)
        except (OSError, ValueError):
            return AppConfig()

    def save(self, cfg: AppConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
