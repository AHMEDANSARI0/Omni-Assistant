"""Built Command Center assets (0.82.0).

The React/TypeScript Command Center is compiled by Vite into this package
directory (``index.html`` + ``assets/*``) so that:

* a released install carries the UI without a Node toolchain;
* the local API can serve it from the same loopback origin as ``/v1`` - no CORS,
  no second server, no token in a URL;
* the release allow-list already covers ``src/**``, so the built assets ship
  inside the portable ZIP and the update bundles automatically.

Nothing here is generated at import time. :func:`smoke` returns the facts a test
or an operator needs: whether a build exists, its size, and the asset list.
"""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path
from typing import Any

__all__ = ["ASSET_ROOT", "index_path", "inline_script_hashes", "read_index", "smoke", "static_path"]

ASSET_ROOT = Path(__file__).resolve().parent
_TOKEN_PLACEHOLDER = "<!--PC_AGENT_TOKEN-->"


def index_path() -> Path:
    return ASSET_ROOT / "index.html"


def read_index(token: str = "") -> str:
    """The SPA shell, with the API token injected as a meta tag.

    The token is only ever rendered on loopback into an HTML ``meta`` element, so
    the browser can call ``/v1`` without the user pasting anything, and no script
    or URL carries it.
    """

    path = index_path()
    if not path.is_file():
        return ""
    html = path.read_text(encoding="utf-8", errors="replace")
    meta = (
        f'<meta name="pc-agent-token" content="{token}" />'
        if token
        else '<meta name="pc-agent-token" content="" />'
    )
    if _TOKEN_PLACEHOLDER in html:
        return html.replace(_TOKEN_PLACEHOLDER, meta, 1)
    return html.replace("</head>", f"  {meta}\n</head>", 1)


def static_path(relative: str) -> Path | None:
    """Resolve a request path inside the asset root, or ``None``.

    Traversal is refused by resolving the candidate and checking that it is still
    inside :data:`ASSET_ROOT`; a symlink that escapes is refused the same way.
    """

    cleaned = relative.split("?", 1)[0].split("#", 1)[0].lstrip("/")
    if not cleaned or "\x00" in cleaned:
        return None
    candidate = (ASSET_ROOT / cleaned).resolve()
    root = ASSET_ROOT.resolve()
    if candidate != root and root not in candidate.parents:
        return None
    if not candidate.is_file():
        return None
    return candidate


def smoke() -> dict[str, Any]:
    """Honest state of the built UI, for tests, the CLI and diagnostics."""

    index = index_path()
    if not index.is_file():
        return {
            "built": False,
            "detail": (
                "no Command Center build is present in this install; run "
                "`npm ci && npm run build` in frontend/ (or apply the patcher that "
                "ships the pre-built assets)"
            ),
            "assets": 0,
            "bytes": 0,
            "index_sha256": "",
        }
    assets_dir = ASSET_ROOT / "assets"
    files = sorted(p for p in assets_dir.glob("*") if p.is_file()) if assets_dir.is_dir() else []
    total = index.stat().st_size + sum(item.stat().st_size for item in files)
    return {
        "built": True,
        "detail": f"{len(files)} asset file(s)",
        "assets": len(files),
        "bytes": total,
        "index_sha256": hashlib.sha256(index.read_bytes()).hexdigest()[:16],
    }


_SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.DOTALL)


def inline_script_hashes(html: str) -> list[str]:
    """CSP hashes for the shell's inline scripts.

    The shell needs one inline script to apply the saved theme before the first
    paint (otherwise a light-mode user sees a dark flash). Rather than weaken the
    policy with ``'unsafe-inline'``, the exact script bodies are hashed here and
    the hashes are added to ``script-src`` for that response only.
    """

    hashes: list[str] = []
    for body in _SCRIPT_RE.findall(html):
        digest = hashlib.sha256(body.encode("utf-8")).digest()
        hashes.append("'sha256-" + base64.b64encode(digest).decode("ascii") + "'")
    return hashes
