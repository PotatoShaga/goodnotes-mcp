"""
GoodNotes PDF Viewer — Remote MCP Server
Connects to Dropbox, downloads GoodNotes PDF backups,
renders individual pages as images, and returns them to Claude.

Deploy on Render.com (free tier) and add as a custom connector in Claude.
"""

import os
import json
import base64
import io
from typing import Optional

import httpx
import fitz  # PyMuPDF
from PIL import Image as PILImage
from mcp.server.fastmcp import FastMCP, Image

# ── Configuration (set via Render environment variables) ──────────────────────

DROPBOX_REFRESH_TOKEN = os.environ["DROPBOX_REFRESH_TOKEN"]
DROPBOX_APP_KEY = os.environ["DROPBOX_APP_KEY"]
DROPBOX_APP_SECRET = os.environ["DROPBOX_APP_SECRET"]

# Root folder for GoodNotes backups in Dropbox.
# Discover with list_files(), then set here for convenience.
# Typical values: "" (search everything) or "/Apps/Goodnotes"
GOODNOTES_ROOT = os.environ.get("GOODNOTES_ROOT", "")

API_SECRET = os.environ.get("API_SECRET", "changeme")


# ── Dropbox Authentication ────────────────────────────────────────────────────

_access_token: Optional[str] = None


def _refresh_token():
    """Exchange refresh token for a new short-lived access token."""
    global _access_token
    r = httpx.post(
        "https://api.dropboxapi.com/oauth2/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": DROPBOX_REFRESH_TOKEN,
            "client_id": DROPBOX_APP_KEY,
            "client_secret": DROPBOX_APP_SECRET,
        },
        timeout=15,
    )
    r.raise_for_status()
    _access_token = r.json()["access_token"]


def _auth_headers():
    """Get Authorization header, refreshing token if needed."""
    if _access_token is None:
        _refresh_token()
    return {"Authorization": f"Bearer {_access_token}"}


# ── Dropbox API Helpers ───────────────────────────────────────────────────────

def _dbx_rpc(endpoint: str, body: dict) -> dict:
    """Call a Dropbox RPC endpoint with auto-retry on 401."""
    headers = {**_auth_headers(), "Content-Type": "application/json"}
    r = httpx.post(
        f"https://api.dropboxapi.com/2/{endpoint}",
        headers=headers, json=body, timeout=30,
    )
    if r.status_code == 401:
        _refresh_token()
        headers["Authorization"] = f"Bearer {_access_token}"
        r = httpx.post(
            f"https://api.dropboxapi.com/2/{endpoint}",
            headers=headers, json=body, timeout=30,
        )
    r.raise_for_status()
    return r.json()


def _dbx_download(path: str) -> bytes:
    """Download a file from Dropbox by path."""
    headers = {
        **_auth_headers(),
        "Dropbox-API-Arg": json.dumps({"path": path}),
    }
    r = httpx.post(
        "https://content.dropboxapi.com/2/files/download",
        headers=headers, timeout=120,
    )
    if r.status_code == 401:
        _refresh_token()
        headers["Authorization"] = f"Bearer {_access_token}"
        r = httpx.post(
            "https://content.dropboxapi.com/2/files/download",
            headers=headers, timeout=120,
        )
    r.raise_for_status()
    return r.content


# ── PDF Rendering ─────────────────────────────────────────────────────────────

def _render_page(pdf_bytes: bytes, page_num: int, max_kb: int = 750) -> tuple[bytes, int]:
    """
    Render one page of a PDF as a JPEG image.

    Automatically adjusts zoom and JPEG quality to stay under max_kb.
    Returns (jpeg_bytes, total_page_count).
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    total = len(doc)

    if page_num < 0 or page_num >= total:
        raise ValueError(
            f"Page {page_num} out of range. "
            f"Valid range: 0–{total - 1} ({total} pages total)."
        )

    page = doc[page_num]
    page_h = page.rect.height

    # Adaptive zoom: tall scrolling pages get lower zoom to keep size down
    if page_h < 1500:
        zoom = 2.0
    elif page_h < 3000:
        zoom = 1.5
    else:
        zoom = max(0.8, 2000 / page_h * 1.5)

    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    img = PILImage.frombytes("RGB", (pix.width, pix.height), pix.samples)

    # Try decreasing JPEG quality until under budget
    for quality in [82, 68, 55, 40]:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        if buf.tell() <= max_kb * 1024:
            return buf.getvalue(), total

    # Still too large — halve resolution and retry
    img = img.resize((img.width // 2, img.height // 2), PILImage.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=55)
    return buf.getvalue(), total


# ── MCP Server ────────────────────────────────────────────────────────────────

mcp = FastMCP("goodnotes-viewer")


@mcp.tool()
def list_files(subfolder: str = "") -> str:
    """
    List PDF files in the GoodNotes Dropbox backup folder.

    Args:
        subfolder: Path relative to GOODNOTES_ROOT, e.g. "2A Tron/MTE 182"
    """
    if GOODNOTES_ROOT and subfolder:
        path = f"{GOODNOTES_ROOT}/{subfolder}"
    elif GOODNOTES_ROOT:
        path = GOODNOTES_ROOT
    elif subfolder:
        path = f"/{subfolder}"
    else:
        path = ""

    try:
        data = _dbx_rpc("files/list_folder", {
            "path": path,
            "recursive": True,
            "limit": 500,
        })
    except httpx.HTTPStatusError as e:
        return f"Error listing '{path}': {e.response.status_code} — {e.response.text[:200]}"

    entries = data.get("entries", [])
    pdfs = [
        e for e in entries
        if e.get(".tag") == "file" and e["name"].lower().endswith(".pdf")
    ]

    if not pdfs:
        return f"No PDF files found in '{path or '/'}'"

    lines = [f"Found {len(pdfs)} PDF(s) in {path or '/'}:", ""]
    for e in sorted(pdfs, key=lambda x: x.get("path_display", "")):
        kb = e.get("size", 0) / 1024
        lines.append(f"  {e['path_display']}  ({kb:.0f} KB)")

    return "\n".join(lines)


@mcp.tool()
def view_page(file_path: str, page: int = 0) -> list:
    """
    Render a page of a GoodNotes PDF backup as a viewable image.

    Args:
        file_path: Full Dropbox path, e.g. "/Apps/Goodnotes/GoodNotes/2A Tron/MTE 182/Finals/Practice Final.pdf"
        page: 0-indexed page number (default 0 = first page)
    """
    # Download PDF
    try:
        pdf_bytes = _dbx_download(file_path)
    except httpx.HTTPStatusError as e:
        return [f"Download failed for '{file_path}': HTTP {e.response.status_code}"]
    except Exception as e:
        return [f"Download error: {e}"]

    # Render requested page
    try:
        img_bytes, total_pages = _render_page(pdf_bytes, page)
    except ValueError as e:
        return [str(e)]
    except Exception as e:
        return [f"Render error: {e}"]

    # Return image + metadata
    return [
        Image(data=img_bytes, format="jpeg"),
        f"Page {page + 1}/{total_pages} of {file_path.split('/')[-1]}  ({len(img_bytes) // 1024} KB)",
    ]


@mcp.tool()
def search_files(query: str) -> str:
    """
    Search for PDF files in Dropbox by name.

    Args:
        query: Search term, e.g. "practice final" or "MTE 182"
    """
    try:
        data = _dbx_rpc("files/search_v2", {
            "query": query,
            "options": {
                "path": GOODNOTES_ROOT or "",
                "max_results": 20,
                "file_extensions": ["pdf"],
            },
        })
    except httpx.HTTPStatusError as e:
        return f"Search error: {e.response.status_code}"

    matches = data.get("matches", [])
    if not matches:
        return f"No PDFs matching '{query}'"

    lines = [f"Found {len(matches)} result(s) for '{query}':", ""]
    for m in matches:
        meta = m.get("metadata", {}).get("metadata", {})
        path = meta.get("path_display", "?")
        kb = meta.get("size", 0) / 1024
        lines.append(f"  {path}  ({kb:.0f} KB)")

    return "\n".join(lines)


# ── Entry Point ───────────────────────────────────────────────────────────────

API_SECRET = os.environ.get("API_SECRET", "changeme")

if __name__ == "__main__":
    import uvicorn
    from contextlib import asynccontextmanager
    from starlette.applications import Starlette
    from starlette.routing import Mount

    mcp_asgi = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        async with mcp.session_manager.run():
            yield

    app = Starlette(
        routes=[Mount(f"/{API_SECRET}", app=mcp_asgi)],
        lifespan=lifespan,
    )

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
