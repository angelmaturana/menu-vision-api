# Menu Vision API

A small FastAPI service for uploading menu images and serving them through a public menu page. The application includes a landing page, an upload form, an in-memory image store, and a bundled default menu image.

## Features

- FastAPI application with automatic OpenAPI documentation.
- Upload menu images as base64-encoded JPEG, PNG, or WebP files.
- Maximum image size of 5 MB.
- Public HTML pages for the venue and each stored menu image.
- Bundled fallback menu and logo loaded from `img.txt` and `logo.txt`.
- Health check and optional keep-alive support for Render deployments.

## Requirements

- Python 3.9 or newer
- The packages listed in `requirements.txt`

## Local setup

Create and activate a virtual environment, then install the dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Start the server:

```bash
python3 main.py
```

The application listens on `http://localhost:8000`.

For development with automatic reload:

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

## Web pages

| URL | Description |
| --- | --- |
| `/` | Public landing page |
| `/upload` | Menu image upload form |
| `/upload.html` | Alias for the upload form |
| `/menu/default` | Bundled or default menu image |
| `/menu/{image_id}` | A specific uploaded menu image |
| `/docs` | Swagger UI API documentation |
| `/redoc` | ReDoc API documentation |

## API endpoints

### Health check

```bash
curl http://localhost:8000/health
```

### Upload a menu image

Send a JSON body containing a base64 data URL. Set `use_default` to `true` to replace the default menu for the lifetime of the process, or to `false` to receive a generated image ID.

```bash
curl -X POST http://localhost:8000/api/v1/parse-menu \
  -H "Content-Type: application/json" \
  -d '{
    "image_b64": "data:image/jpeg;base64,<BASE64_IMAGE_DATA>",
    "use_default": true
  }'
```

The response includes `image_id`, `view_url`, the detected format, and the image size in bytes.

### Keep-alive endpoint

```bash
curl http://localhost:8000/keepalive
```

This returns `OK` and can be used by an external monitoring service.

## Storage notes

Uploaded images are stored in memory and are lost when the process restarts. The bundled image in `img.txt` is used as the fallback for `/menu/default`. The legacy `/uploads/{filename}` endpoint serves files from `/tmp/uploads` when they exist.

## Render deployment

The application binds to `0.0.0.0` on port `8000` when started directly. Configure the deployment platform to run:

```bash
python3 main.py
```

When `RENDER_EXTERNAL_URL` is set, a background thread periodically calls `/keepalive`. The interval defaults to 270 seconds and can be changed with `KEEPALIVE_INTERVAL`.

## Project structure

```text
main.py             FastAPI application and routes
requirements.txt    Python dependencies
templates/          Jinja2 HTML templates
img.txt             Bundled default menu image data
logo.txt            Bundled logo image data
```
