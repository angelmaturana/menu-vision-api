"""
FastAPI application for Menu Vision API.

Implements in-memory image storage with a bundled default image fallback
for deployment on Render (ephemeral storage).

Includes a Keep-Alive Engine to prevent the service from sleeping
on Render Free Tier.

Endpoints:
  - GET  /health                    Health check
    - GET  /upload                    Upload page
  - POST /api/v1/parse-menu         Parse and store an image from base64
  - GET  /uploads/{filename}        Serve a previously uploaded file (legacy)
  - GET  /menu/{image_id}           Render HTML menu page with embedded image
  - GET  /keepalive                 Internal keep-alive ping endpoint
"""

import io
import os
import sys
import time
import uuid
import base64
import threading
import urllib.request
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware

from PIL import Image
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Directory for legacy static-file serving (used by /uploads/{filename}).
# Files placed here are NOT used by the in-memory storage path.
UPLOAD_DIR = Path("/tmp/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Maximum allowed image size: 5 MB
MAX_IMAGE_BYTES = 5 * 1024 * 1024

# In-memory image store: {image_id: image_bytes}
# This dictionary is shared across requests within a single container
# lifetime.  On restart (e.g. Render free-tier dyno restart) the store
# is re-initialised empty.
image_store: dict[str, bytes] = {}

# Bundled default image. The file contains an <img> element with a data URI.
DEFAULT_IMAGE_FILE = Path(__file__).with_name("img.txt")
LOGO_FILE = Path(__file__).with_name("logo.txt")

# The default image_id used when serving the bundled fallback image.
DEFAULT_IMAGE_ID = "default"


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class ParseMenuRequest(BaseModel):
    """Request body model for POST /api/v1/parse-menu."""
    image_b64: str = Field(..., description="Base64-encoded image string, "
                                          "optionally with data-URL prefix.")
    use_default: bool = Field(
        True,
        description="Store the image under the default ID when enabled.",
    )


# ---------------------------------------------------------------------------
# OpenAPI app & middleware
# ---------------------------------------------------------------------------

app = FastAPI(title="Menu Vision API", version="1.0.0")

# Allow CORS from any origin (sufficient for a public API deployed on Render).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def decode_base64_image(raw: str) -> bytes:
    """
    Decode a base64 string that may or may not contain the
    ``data:image/...;base64,`` prefix.
    """
    # Strip the data-URL prefix if present
    if raw.startswith("data:image"):
        raw = raw.split(",", 1)[-1]
    return base64.b64decode(raw)


def load_default_image() -> bytes:
    """Load the bundled default image from img.txt."""
    raw = DEFAULT_IMAGE_FILE.read_text(encoding="utf-8").strip()
    if "base64," in raw:
        raw = raw.split("base64,", 1)[1].split('"', 1)[0]
    return decode_base64_image(raw)


def load_logo_base64() -> str:
    """Load the bundled logo as a base64 string for template embedding."""
    raw = LOGO_FILE.read_text(encoding="utf-8").strip()
    if "base64," in raw:
        raw = raw.split("base64,", 1)[1].split('"', 1)[0]
    return raw


DEFAULT_IMAGE_BYTES = load_default_image()
LOGO_BASE64 = load_logo_base64()


def validate_image(image_bytes: bytes) -> str:
    """
    Validate that *image_bytes* represents a permitted image format
    (JPEG, PNG, WebP).  Returns the detected format or raises
    ``ValueError``.
    """
    # Check size limit
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise ValueError(
            f"Image exceeds maximum allowed size of {MAX_IMAGE_BYTES} bytes"
        )

    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.verify()  # ensures the file is not corrupt
    except Exception as exc:
        raise ValueError(f"Invalid or corrupt image: {exc}") from exc

    # Re-open to read format (verify() closes the image)
    try:
        img = Image.open(io.BytesIO(image_bytes))
        fmt = img.format.upper()
    except Exception as exc:
        raise ValueError(f"Unable to determine image format: {exc}") from exc

    if fmt not in ("JPEG", "PNG", "WEBP"):
        raise ValueError(
            f"Unsupported image format '{fmt}'. Allowed: JPEG, PNG, WebP"
        )
    return fmt


def retrieve_image(image_id: str) -> Optional[bytes]:
    """
    Retrieve an image by its ID.

    Search order:
      1. In-memory store  (``image_store``)
            2. Bundled default image (only when image_id == DEFAULT_IMAGE_ID)

    Returns ``bytes`` on success or ``None`` on failure.
    """
    # 1 - In-memory lookup
    if image_id in image_store:
        return image_store[image_id]

    # 2 - Bundled fallback (only for the default ID)
    if image_id == DEFAULT_IMAGE_ID:
        return DEFAULT_IMAGE_BYTES

    return None


def build_url(request: Request, path: str) -> str:
    """
    Build a fully-qualified URL from the request context.

    Detects the correct scheme (http/https) and host header.
    """
    scheme = "https" if request.headers.get("x-forwarded-proto", "http") == "https" else "http"
    host = request.headers.get("host", "localhost:8000")
    return f"{scheme}://{host}{path}"


# ---------------------------------------------------------------------------
# Keep-Alive Engine (Anti-Sleep for Render Free Tier)
# ---------------------------------------------------------------------------
# This mechanism mirrors the pattern proven in production: the service pings
# its own public URL (RENDER_EXTERNAL_URL) which is detected as traffic by
# Render's routing layer, preventing the service from being marked idle
# and put to sleep.

def keep_alive_ping():
    """
    Background daemon thread that periodically pings the service's external
    /health endpoint to keep the Render Free Tier service awake.

    Key design decisions (matching the proven pattern from production):
      * 30-second initial delay so the service fully starts before pinging.
      * Pings RENDER_EXTERNAL_URL (the public URL Render assigns, e.g.
        https://menu-vision-api.onrender.com).  If that variable is not
        set the thread exits gracefully (no-op in local development).
      * 270-second interval (4.5 min) — well under Render's ~5-15 min
        sleep threshold.
    """
    # Give the server time to boot before attempting to ping
    time.sleep(30)

    external_url = os.environ.get("RENDER_EXTERNAL_URL")
    if not external_url:
        print("[Keep-Alive] RENDER_EXTERNAL_URL not set — skipping keep-alive (local mode)")
        return

    ping_url = f"{external_url}/keepalive"
    interval = int(os.environ.get("KEEPALIVE_INTERVAL", "270"))  # 4.5 minutes

    print(f"[Keep-Alive] Engine started — pinging {ping_url} every {interval}s")

    while True:
        try:
            req = urllib.request.Request(ping_url, method="GET")
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    print(f"[Keep-Alive] Pinged {ping_url} → {resp.status}")
        except Exception as exc:
            print(f"[Keep-Alive] Ping failed: {exc}")

        time.sleep(interval)


# Start keep-alive thread on module load.
# The thread will self-terminate if RENDER_EXTERNAL_URL is not set.
threading.Thread(target=keep_alive_ping, daemon=True).start()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health_check() -> dict:
    """Simple health-check endpoint for monitoring and keep-alive pings."""
    return {
        "status": "healthy",
        "message": "Menu Vision API is running",
        "store_size": len(image_store),
    }


@app.post("/api/v1/parse-menu")
async def parse_menu(request: Request, payload: ParseMenuRequest):
    """
    **POST /api/v1/parse-menu**

    Accepts a JSON payload with a base64-encoded image, decodes it,
    validates it, and stores it in memory.

    Payload format::

        {
          "image_b64": "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQ..."
        }

    Returns the ``image_id`` and a public ``view_url``.
    """
    image_b64 = payload.image_b64
    if not image_b64.strip():
        raise HTTPException(status_code=400, detail="Missing 'image_b64' field")

    # Decode base64
    try:
        image_bytes = decode_base64_image(image_b64)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid base64 data: {exc}")

    # Validate image format and size
    try:
        fmt = validate_image(image_bytes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # The default image lives only in memory after an upload. On restart,
    # retrieve_image() falls back to the bundled image in img.txt.
    image_id = DEFAULT_IMAGE_ID if payload.use_default else str(uuid.uuid4())

    # Store in memory (NOT on disk)
    image_store[image_id] = image_bytes

    # Build public URL
    view_url = build_url(request, f"/menu/{image_id}")

    return {
        "status": "success",
        "image_id": image_id,
        "view_url": view_url,
        "format": fmt,
        "size_bytes": len(image_bytes),
    }


@app.get("/uploads/{filename}")
async def serve_upload(filename: str):
    """
    Serve a previously saved file from the legacy upload directory.

    Because the primary storage is in-memory, this endpoint is a
    fallback for files that were explicitly written to disk (e.g.
    during initial seeding via a separate script).
    """
    # Prevent path traversal attacks
    safe_name = Path(filename).name

    filepath = UPLOAD_DIR / safe_name
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="File not found")

    # Detect media type based on extension
    import mimetypes
    media_type, _ = mimetypes.guess_type(str(filepath))
    if not media_type:
        media_type = "application/octet-stream"

    return FileResponse(
        path=filepath,
        filename=safe_name,
        media_type=media_type,
    )


# Initialise Jinja2 template renderer
templates = Jinja2Templates(directory="templates")


@app.get("/upload")
@app.get("/upload.html")
async def upload_page(request: Request):
    """Render the dedicated image upload page."""
    return templates.TemplateResponse(
        request=request,
        name="upload.html",
        context={
            "default_image_id": DEFAULT_IMAGE_ID,
            "logo_base64": LOGO_BASE64,
        },
    )


@app.get("/menu/{image_id}")
async def view_menu(request: Request, image_id: str):
    """
    Render the ``Bar Sinabril`` menu page for *image_id*.

    The image is retrieved from the in-memory store (or the bundled
    default image as a fallback) and embedded
    directly into the HTML as a base64 data-URI.
    """
    image_bytes = retrieve_image(image_id)
    if image_bytes is None:
        raise HTTPException(
            status_code=404,
            detail=f"Image with id '{image_id}' not found",
        )

    # Encode image to base64 for data-URI embedding
    try:
        image_base64 = base64.b64encode(image_bytes).decode("utf-8")
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to encode image: {exc}",
        )

    # Detect image format for proper data-URI scheme
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img_format = img.format.lower()
    except Exception:
        img_format = "jpeg"

    return templates.TemplateResponse(
        request=request,
        name="menu.html",
        context={
            "image_id": image_id,
            "image_base64": image_base64,
            "image_format": img_format,
            "title": "Bar Sinabril",
            "image_title": "Carta del Bar",
            "logo_base64": LOGO_BASE64,
        },
    )


@app.get("/keepalive")
async def keepalive():
    """
    Simple endpoint for external keep-alive services (e.g. UptimeRobot).
    Returns a small response to keep the service active.
    """
    return PlainTextResponse("OK", status_code=200)


if __name__ == "__main__":
    import uvicorn

    print("Starting Menu Vision API (in-memory storage)…")
    print(f"Max image size: {MAX_IMAGE_BYTES} bytes")
    uvicorn.run(app, host="0.0.0.0", port=8000)
