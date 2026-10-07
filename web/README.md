# RedactEz — web interface

A thin Flask front-end over the `redactez` package. Same engine, same defaults
as the CLI; the CLI is unaffected and remains fully usable on its own.

## Run with Docker (recommended)

```bash
cd web
docker compose up -d          # -> http://localhost:8020
docker compose logs -f        # follow
docker compose down           # stop
```

The image bundles Tesseract with French and English language data, so scanned
documents and images work out of the box. To require a password, add
`REDACTEZ_AUTH=user:password` to the `environment` list in
`docker-compose.yml`. Uploaded files only ever live on a RAM-backed
`/tmp` (tmpfs), and the container runs read-only as an unprivileged user.

## Run without Docker (dev)

```bash
pip install -r ../requirements.txt -r requirements.txt
python3 webapp.py             # -> http://localhost:8020
```

Scanned documents need Tesseract on the host (`python3 -m redactez --check-ocr`).

## How it works

The page follows the CLI's phases:

1. **Documents** — drag & drop one or several files: PDF, Word (`.docx`) or
   images (PNG, JPEG, TIFF, BMP). *Detect personal data* sends each file to
   `POST /analyze`, which returns the candidates (type, confidence, context,
   and the position of every occurrence).
2. **What to redact** — one entry per value, grouped by type. High and medium
   confidence values are ticked, low-confidence ones are not. Tick or untick
   values one at a time or a whole type at once, and type extra terms by hand
   (one per line). Clicking a value jumps to it in the preview.
   - **Other forms of a name** (`NAME_VARIANT`: `DOE`, `J. DOE`…) follow the
     name they come from until you decide them yourself. Names found in one
     file are looked for in the files analysed after it.
   - **Keyboard**: <kbd>↑</kbd>/<kbd>↓</kbd> to move, <kbd>Space</kbd> to
     tick or untick, <kbd>y</kbd>/<kbd>n</kbd> to redact or keep and move on,
     <kbd>t</kbd>/<kbd>i</kbd> for the whole type.
   - **Decision file**: *Import* applies a `pii.json` written by the CLI's
     `--policy` (or by *Export*); values it marks to redact that the detection
     did not propose are added to the manual list. *Export* saves the current
     decisions in the same format. Both happen in the browser only.
3. **Output** — rendering (stars, black box, label), watermark, and every
   advanced CLI option (whole word, case, accents, pixel erasure, flattening,
   OCR visual check, metadata). *Redact* sends each file with the chosen terms
   to `POST /redact`. One file comes back as a PDF and several as a ZIP. The
   ZIP is built in the browser.

Each produced file is listed with its number of occurrences and its
verification status. **A file whose final check still finds a term is withheld
(HTTP 422) instead of delivered.**

Other features:

- **Live preview of the real page** with pdf.js, which is vendored in
  `static/vendor/` (no CDN). Ticked values are drawn the way they will be
  rendered (stars, box or label), unticked ones are outlined with a dashed
  line, and the watermark is shown as well. Terms typed by hand, and values
  found in other files, appear once the detection has run. Images are
  previewed too. Word documents have no fixed pages, so they have no preview.
  Use the arrows to change pages and click a file to preview it.
- **Progress bar with ETA** and a **Cancel** button, with a status per file
  (queued, processing, done, failed).
- **EN/FR**, with server errors translated, and **three themes**: Black (OLED),
  which is the default, grey and white.
- **The options are remembered** in the browser. **Terms and detected values
  never are**: they are personal data.

## Privacy

- Every request runs in its own temporary directory, which is wiped before the
  response is sent. Nothing is kept server-side. The file is uploaded twice,
  once to analyse and once to redact, rather than kept between the two steps.
- Backend instances are not shared between requests, so no extraction cache
  holds a previous visitor's text in memory.
- Logs only record the exception *type*, never a message that could quote the
  document. The `X-RedactEz-Report` header carries counts, never a value.
- `Cache-Control: no-store`, `Referrer-Policy: no-referrer`, and a strict CSP
  (no `unsafe-eval`, no third-party origin).

## Limits & hardening

The web is the untrusted surface. The CLI stays unbounded.

| Limit | Value |
|---|---|
| Upload size | 100 MB per file (one file per request) |
| Decompressed DOCX | 512 MB (zip bomb guard) |
| Pages per file | 200 (OCR costs about 1 s per page) |
| Terms per request | 2,000, each 2 to 500 characters |
| OCR / flatten resolution | 400 dpi max |

- Validation errors return JSON `{error, hint}`. Unexpected errors return a
  generic message.
- The server temp path is never echoed back to the client.
- **Authentication** is off by default. Set `REDACTEZ_AUTH=user:password` to
  require HTTP Basic authentication on every page except `/health`. Basic auth
  sends the password with each request, so use it over HTTPS only.
- **Rate limiting**: 120 processing requests per minute and per client address
  by default (`REDACTEZ_RATE_LIMIT`, `0` disables it). Beyond that the server
  answers 429 with `Retry-After`. The counter lives in memory, per worker
  process.

## Port

The default port is **8020**, so it does not collide with filigranez on 8010.
Override it with the `PORT` environment variable, and match the new value in
the reverse-proxy `proxy_pass` and in the healthcheck.

## Behind a reverse proxy (sub-path)

The app honours `X-Forwarded-Prefix` and `X-Forwarded-Proto` through
werkzeug's ProxyFix:

```nginx
location = /redactez { return 301 /redactez/; }

location /redactez/ {
    proxy_pass http://127.0.0.1:8020/;      # trailing slash strips the prefix
    proxy_set_header Host               $host;
    proxy_set_header X-Forwarded-For    $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto  $scheme;
    proxy_set_header X-Forwarded-Prefix /redactez;

    client_max_body_size 100m;              # match the app's upload limit
    proxy_read_timeout   600s;              # OCR on long scans
    proxy_request_buffering off;
    access_log off;                         # or at least no request bodies
}
```

## Tests

```bash
python3 -m unittest discover -s tests -p "test_web.py" -v   # skipped without Flask
```

pdf.js © Mozilla Foundation, Apache License 2.0.
