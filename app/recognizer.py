"""视觉识别：图片 → 模型 API → 文本。

适配说明：支持三种 API 协议（可配置）：
- openai           ：OpenAI 兼容 /chat/completions（OpenAI、通义、智谱、硅基流动、
                     Ollama 本地、opencode zen 等几乎所有服务）
- openai-responses ：OpenAI 新版 /responses 协议（Responses API）
- anthropic        ：Anthropic /v1/messages（Claude 系列或兼容其协议的中转）

网络：可通过配置选择 跟随系统代理 / 强制直连 / 自定义代理地址。
模型必须支持图像输入。
"""
from __future__ import annotations

import base64
import re

import requests
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt, QThread, Signal
from PySide6.QtGui import QImage

from app.config import AppConfig

# 单张图片发送预算（字节）。多数服务的 body 上限在 4MB 左右，留些余量。
IMAGE_BUDGET = 3_500_000

_DIRECT = ("direct", "none", "off", "no", "直连")


def _build_session(cfg: AppConfig) -> requests.Session:
    """按配置构造会话：空=跟随系统；direct=强制直连；其他=指定代理。"""
    s = requests.Session()
    p = (cfg.proxy or "").strip()
    if not p:
        return s                       # 跟随系统环境变量（HTTP_PROXY 等）
    if p.lower() in _DIRECT:
        s.trust_env = False            # 忽略系统代理，强制直连
        return s
    s.proxies = {"http": p, "https": p}
    return s


def _api_style(cfg: AppConfig) -> str:
    """openai | openai-responses | anthropic（未知值回落 openai）。"""
    v = (cfg.api_style or "").strip().lower()
    if v in ("openai-responses", "responses"):
        return "openai-responses"
    if v == "anthropic":
        return "anthropic"
    return "openai"


def _apply_thinking(cfg: AppConfig, payload: dict, style: str) -> None:
    """深度思考开关：按协议注入对应参数（关闭时不发，保持服务默认）。"""
    if not getattr(cfg, "enable_thinking", False):
        return
    if style == "anthropic":
        # Anthropic：thinking 预算须小于 max_tokens，且 temperature 必须为 1
        budget = max(1024, int(cfg.max_tokens) // 2)
        payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
        payload["max_tokens"] = max(int(cfg.max_tokens), budget + 1024)
        payload["temperature"] = 1
    elif style == "openai-responses":
        payload["reasoning"] = {"effort": "medium"}
    else:
        # OpenAI 兼容：通义 / vLLM / 硅基流动等主流服务的开关字段
        payload["enable_thinking"] = True


def _endpoint_headers_payload(
    cfg: AppConfig, prompt: str, image: tuple[str, str] | None, timeout: float
):
    """按协议组装 (endpoint, headers, payload)。image 为 (mime, base64) 或 None。"""
    style = _api_style(cfg)
    if style == "anthropic":
        content: list = [{"type": "text", "text": prompt}]
        if image is not None:
            mime, b64 = image
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": mime, "data": b64},
            })
        payload = {
            "model": cfg.model,
            "max_tokens": cfg.max_tokens,
            "temperature": cfg.temperature,
            "messages": [{"role": "user", "content": content}],
        }
        _apply_thinking(cfg, payload, style)
        headers = {
            "x-api-key": cfg.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        return cfg.messages_endpoint(), headers, payload

    if style == "openai-responses":
        # OpenAI Responses API：input + input_text/input_image，max_output_tokens
        content = [{"type": "input_text", "text": prompt}]
        if image is not None:
            mime, b64 = image
            content.append({
                "type": "input_image",
                "image_url": f"data:{mime};base64,{b64}",
            })
        payload = {
            "model": cfg.model,
            "input": [{"role": "user", "content": content}],
            "temperature": cfg.temperature,
            "max_output_tokens": cfg.max_tokens,
        }
        _apply_thinking(cfg, payload, style)
        headers = {
            "Authorization": f"Bearer {cfg.api_key}",
            "Content-Type": "application/json",
        }
        return cfg.responses_endpoint(), headers, payload

    # 默认：OpenAI 兼容 Chat Completions
    content = [{"type": "text", "text": prompt}]
    if image is not None:
        mime, b64 = image
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
        })
    payload = {
        "model": cfg.model,
        "messages": [{"role": "user", "content": content}],
        "temperature": cfg.temperature,
        "max_tokens": cfg.max_tokens,
    }
    _apply_thinking(cfg, payload, style)
    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
    }
    return cfg.chat_endpoint(), headers, payload


def _parse_text(cfg: AppConfig, data: dict) -> str:
    style = _api_style(cfg)
    if style == "anthropic":
        parts = [b.get("text", "") for b in data.get("content", [])
                 if isinstance(b, dict) and b.get("type") == "text"]
        if not parts:
            raise RuntimeError(f"响应格式异常：{str(data)[:400]}")
        return "\n".join(parts)
    if style == "openai-responses":
        # 优先取 SDK 风格的便捷字段；否则遍历 output 数组提取 output_text
        if isinstance(data.get("output_text"), str) and data["output_text"]:
            return data["output_text"]
        parts = []
        for item in data.get("output", []):
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for b in item.get("content", []):
                if isinstance(b, dict) and b.get("type") == "output_text":
                    parts.append(b.get("text", ""))
        if not parts:
            raise RuntimeError(f"响应格式异常：{str(data)[:400]}")
        return "\n".join(parts)
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"响应格式异常：{str(data)[:400]}")


def _post_chat(cfg: AppConfig, prompt: str,
               image: tuple[str, str] | None, timeout: float) -> str:
    """发一次请求并返回模型文本。抛异常表示失败。

    超时拆分：连接超时固定 20 秒（快速发现地址/代理问题），
    读超时 = 配置的 timeout（大图 + 推理模型响应慢，需要留足等待）。
    """
    url, headers, payload = _endpoint_headers_payload(cfg, prompt, image, timeout)
    sess = _build_session(cfg)
    resp = sess.post(url, json=payload, headers=headers,
                     timeout=(min(20.0, timeout), timeout))
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}：{resp.text[:400]}")
    return _parse_text(cfg, resp.json())


def _save_to_bytes(img: QImage, fmt: str, quality: int = -1) -> bytes:
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    ok = img.save(buf, fmt, quality) if quality > 0 else img.save(buf, fmt)
    buf.close()
    return bytes(ba) if ok else b""


def encode_image(image: QImage, max_side: int) -> tuple[str, str]:
    """返回 (mime, base64)。PNG 优先，超预算自动转 JPEG / 缩小。"""
    img = image
    if max(img.width(), img.height()) > max_side:
        img = img.scaled(max_side, max_side,
                         Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    png = _save_to_bytes(img, "PNG")
    if len(png) <= IMAGE_BUDGET:
        return "image/png", base64.b64encode(png).decode("ascii")
    jpg = _save_to_bytes(img, "JPG", 88)
    if len(jpg) <= IMAGE_BUDGET:
        return "image/jpeg", base64.b64encode(jpg).decode("ascii")
    small = img.scaled(1024, 1024,
                       Qt.AspectRatioMode.KeepAspectRatio,
                       Qt.TransformationMode.SmoothTransformation)
    jpg2 = _save_to_bytes(small, "JPG", 85)
    return "image/jpeg", base64.b64encode(jpg2).decode("ascii")


def recognize(cfg: AppConfig, image: QImage, prompt: str) -> str:
    """阻塞式识别，返回模型文本。抛异常表示失败。

    大图容错：全尺寸请求若**读超时**（大图 + 推理模型响应慢，最常见）或服务端
    报 413（图片过大），自动把图片最长边压缩一半后重试一次——压缩可显著降低
    编码与推理耗时。连接被拒 / 鉴权失败等错误不重试，直接抛出以便快速诊断。
    """
    mime, b64 = encode_image(image, cfg.max_image_side)
    try:
        return _post_chat(cfg, prompt, (mime, b64), timeout=float(cfg.timeout))
    except requests.exceptions.ReadTimeout:
        smaller = max(640, int(cfg.max_image_side) // 2)
        mime, b64 = encode_image(image, smaller)
        try:
            return _post_chat(cfg, prompt, (mime, b64), timeout=float(cfg.timeout))
        except Exception:  # noqa: BLE001  重试仍失败：报原始超时错误更可诊断
            raise
    except RuntimeError as exc:
        if "HTTP 413" not in str(exc):
            raise
        smaller = max(640, int(cfg.max_image_side) // 2)
        mime, b64 = encode_image(image, smaller)
        return _post_chat(cfg, prompt, (mime, b64), timeout=float(cfg.timeout))


def ping_model(cfg: AppConfig) -> tuple[bool, str]:
    """用一条纯文本消息测试服务连通性。"""
    try:
        content = _post_chat(
            cfg, "你好，请回复 OK", None,
            timeout=min(float(cfg.timeout), 30.0),
        )
        return True, f"连接成功：{content[:60]}"
    except requests.exceptions.RequestException as exc:
        return False, f"网络错误：{exc}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _models_endpoint(cfg: AppConfig) -> str:
    base = cfg.base_url.strip().rstrip("/")
    if base.endswith("/models"):
        return base
    return base + "/models"


def list_models(cfg: AppConfig) -> list[str]:
    """查询服务可用模型列表（GET {base_url}/models，OpenAI/Anthropic 均兼容）。"""
    sess = _build_session(cfg)
    if _api_style(cfg) == "anthropic":
        headers = {"x-api-key": cfg.api_key, "anthropic-version": "2023-06-01"}
    else:
        headers = {"Authorization": f"Bearer {cfg.api_key}"}
    resp = sess.get(_models_endpoint(cfg), headers=headers,
                    timeout=min(float(cfg.timeout), 30.0))
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}：{resp.text[:400]}")
    data = resp.json()
    items = data.get("data", data) if isinstance(data, dict) else data
    ids = []
    for m in items if isinstance(items, list) else []:
        mid = m.get("id") if isinstance(m, dict) else m
        if isinstance(mid, str) and mid:
            ids.append(mid)
    if not ids:
        raise RuntimeError(f"响应格式异常：{str(data)[:300]}")
    return sorted(set(ids))


# ---------------------------------------------------------------- LaTeX 提取
_FENCE = re.compile(r"```(?:latex|tex|math|latexmath)?\s*\n([\s\S]*?)```", re.I)
_DOUBLE_DOLLAR = re.compile(r"\$\$([\s\S]+?)\$\$")
_BRACKET = re.compile(r"\\\[([\s\S]+?)\\\]")
_PAREN = re.compile(r"\\\(([\s\S]+?)\\\)")
_INLINE = re.compile(r"(?<!\\)\$([^$\n]+?)\$")


def extract_latex(text: str, keep_delims: bool = False) -> str | None:
    r"""从模型输出里提取 LaTeX。

    优先级：代码块 > $$..$$ > \[..\] > \(..\) > $..$。
    keep_delims=False：返回裸 LaTeX（剥掉定界符）。
    keep_delims=True ：保留 \(..\)/\[..\]/$$..$$/$..$ 定界符原样输出，
                       多个公式逐行拼接；``` 代码块始终返回裸 LaTeX（剥围栏）。
    """
    if not text:
        return None
    fence = _FENCE.search(text)
    if fence and fence.group(1).strip():
        return fence.group(1).strip()
    for pat in (_DOUBLE_DOLLAR, _BRACKET, _PAREN, _INLINE):
        matches = [m for m in pat.finditer(text) if m.group(1).strip()]
        if matches:
            if keep_delims:
                return "\n".join(m.group(0).strip() for m in matches)
            return "\n".join(m.group(1).strip() for m in matches)
    return None


def strip_latex_delims(text: str) -> str:
    r"""剥掉 \(..\)/\[..\]/$$..$$/$..$/``` 围栏等定界符，返回裸 LaTeX（用于渲染预览）。"""
    for pat in (_FENCE, _DOUBLE_DOLLAR, _BRACKET, _PAREN, _INLINE):
        text = pat.sub(lambda m: (m.group(1) or "").strip(), text)
    return text.strip()


# ---------------------------------------------- 自动复制决策（迷你窗流程共用）
def plan_auto_copy(cfg: AppConfig, mode: str, text: str) -> tuple[str | None, str]:
    """识别完成后决定自动复制的内容与提示文案，返回 (应复制文本, 状态说明)。

    约定：识别产物要“直接可用”——公式模式自动复制提取出的 LaTeX（找不到
    代码块时退回复制全文）；携带原文 / 提取信息模式复制整段文本。
    返回 None 表示结果为空、无可复制内容。
    """
    if mode == "extract":
        full = text.strip()
        if full:
            return full, ("图片信息已提取并自动复制到剪贴板，可直接粘贴给"
                          "无视觉能力的模型（可编辑后再复制）")
        return None, "提取完成，但结果为空"
    if cfg.latex_with_text:
        full = text.strip()
        if full:
            return full, ("识别完成（含原文），已自动复制到剪贴板（可直接 "
                          "Ctrl+V；编辑后可再点“复制 LaTeX”）")
        return None, "识别完成，但结果为空"
    latex = extract_latex(text, keep_delims=cfg.latex_keep_delim)
    if latex:
        return latex, ("识别完成，LaTeX 已自动复制到剪贴板（可直接 Ctrl+V；"
                       "编辑后可再点“复制 LaTeX”）")
    if text.strip():
        return text, "未找到 ```latex 代码块，已将全文复制到剪贴板（可编辑后再复制）"
    return None, "识别完成，但结果为空"


# ---------------------------------------------------------------- 后台任务
# 运行中的任务由全局集合托管，避免“窗口关闭时任务还在跑”导致 QThread 被提前
# 销毁而崩溃；任务结束（finished）后自动从集合移除。
_ACTIVE_TASKS: set = set()


def track_task(task: "RecognizeTask") -> None:
    _ACTIVE_TASKS.add(task)
    task.finished.connect(lambda t=task: _ACTIVE_TASKS.discard(t))


def wait_all_tasks(timeout_ms: int | None = None) -> None:
    """等待所有后台识别任务结束（退出前调用）。"""
    for t in list(_ACTIVE_TASKS):
        if t.isRunning():
            t.wait(timeout_ms)


class RecognizeTask(QThread):
    """后台识别任务，避免阻塞 UI。独立于任何窗口生命周期。"""

    succeeded = Signal(str)
    failed = Signal(str)

    def __init__(self, cfg: AppConfig, image: QImage, prompt: str):
        super().__init__()
        self._cfg = cfg
        self._image = image.copy()
        self._prompt = prompt

    def run(self):  # noqa: D102
        try:
            text = recognize(self._cfg, self._image, self._prompt)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
        else:
            self.succeeded.emit(text)
