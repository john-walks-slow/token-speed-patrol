"""base_url 规范化。协议无关：仅省略协议头时补 https://，路径原样保留。"""

import re

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


def normalize_base_url(base_url: str) -> str:
    """省略协议头时补 https://；用户输入的路径（含 /v1）原样保留，不强制补 /v1。"""
    url = (base_url or "").strip().rstrip("/")
    if not _SCHEME_RE.match(url):
        url = "https://" + url
    return url
