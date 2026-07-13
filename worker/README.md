# Wedding Uploads Worker

Cloudflare Worker that issues presigned R2 PUT URLs so guests can upload photos and videos directly to R2 — no proxy through the Worker body, so large video files (up to 500 MB) work fine.

---

## Setup steps

### 1. Create a Cloudflare account

Go to https://dash.cloudflare.com and sign up (free tier is fine).

### 2. Create the R2 bucket

In the Cloudflare dashboard → **R2** → **Create bucket**  
Name it exactly: `wedding-uploads-2026`

### 3. Create a scoped R2 API token

Dashboard → **R2** → **Manage R2 API tokens** → **Create API token**

- Permissions: **Object Read & Write**
- Specify bucket: `wedding-uploads-2026`
- Copy the **Access Key ID** and **Secret Access Key** — you won't see them again.

### 4. Set the bucket CORS policy

In the bucket settings → **CORS Policy** → paste and save:

```json
[
  {
    "AllowedOrigins": ["https://mikaelmajabrollop2026.se"],
    "AllowedMethods": ["PUT", "GET", "HEAD"],
    "AllowedHeaders": ["Content-Type", "Content-Length"],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 3600
  }
]
```

If you want to test from localhost, temporarily add `"http://localhost:8000"` to `AllowedOrigins` and also add it to `EXTRA_ALLOWED_ORIGINS` in wrangler.toml.

### 5. Install dependencies

```bash
cd worker
npm install
```

### 6. Log in to Wrangler

```bash
npx wrangler login
```

### 7. Set secrets (never commit these)

```bash
npx wrangler secret put R2_ACCESS_KEY_ID
# paste the Access Key ID from step 3

npx wrangler secret put R2_SECRET_ACCESS_KEY
# paste the Secret Access Key from step 3

npx wrangler secret put UPLOAD_KEY
# a random string, e.g. output of: openssl rand -hex 8
# This repo is public, so the key lives only here and in the QR URL (?k=<value>)
```

### 8. Edit wrangler.toml vars

Open `wrangler.toml` and replace the placeholders:

| Var | What to put |
|-----|-------------|
| `ACCOUNT_ID` | Your Cloudflare Account ID (dash.cloudflare.com → top-right or Workers & Pages → Overview) |
| `UPLOAD_OPEN_UTC` | When uploads open, in UTC, e.g. `"2026-08-08T06:00:00Z"` |
| `UPLOAD_CLOSE_UTC` | End of upload window, e.g. `"2026-08-23T23:59:59Z"` — keep it open a couple of weeks so guests can upload videos from home |
| `MAX_TOTAL_GB` | Hard cap on total bucket volume (default `"20"`). Once reached, new uploads are refused. Concurrent in-flight uploads can overshoot by a few files at most. Raise it and redeploy if the wedding fills it up. |
| `EXTRA_ALLOWED_ORIGINS` | `"http://localhost:8000"` for local dev, empty string for production |

### 9. Deploy

```bash
npx wrangler deploy
```

Wrangler prints the Worker URL, e.g. `https://wedding-uploads-worker.<your-subdomain>.workers.dev`

Copy it. Open `upload.html` in the repo root and replace:

```js
var WORKER_URL = "https://REPLACE-AFTER-DEPLOY.workers.dev";
```

with the real URL.

### 10. Generate the QR code

The QR should point to:
```
https://mikaelmajabrollop2026.se/upload.html?k=<YOUR_UPLOAD_KEY>
```

Using npx:
```bash
npx qrcode "https://mikaelmajabrollop2026.se/upload.html?k=YOUR_KEY_HERE"
```

Or with the `qrencode` CLI (install with `brew install qrencode`):
```bash
qrencode -o wedding-upload-qr.png -s 8 \
  "https://mikaelmajabrollop2026.se/upload.html?k=YOUR_KEY_HERE"
```

Print the QR and place it on tables. The `?k=` secret is the only access control — keep the key out of any public channels.

---

## After the wedding — downloading everything

### 1. Install rclone

```bash
brew install rclone
```

### 2. Configure rclone for R2

```bash
rclone config
```

Choose: **n** (new remote) → name it `r2wedding` → type `s3` → provider `Cloudflare` → enter Access Key ID and Secret Access Key from step 3 → endpoint `https://<ACCOUNT_ID>.r2.cloudflarestorage.com` → region `auto` → leave ACL blank → save.

### 3. Download all uploads

```bash
rclone copy r2wedding:wedding-uploads-2026/uploads ./wedding-photos --progress
```

### 4. Verify the download

```bash
ls -lh ./wedding-photos | wc -l
rclone check r2wedding:wedding-uploads-2026/uploads ./wedding-photos
```

### 5. Clean up

Once you've confirmed everything is safely on disk:

```bash
# Delete all objects in the bucket
rclone purge r2wedding:wedding-uploads-2026

# Delete the R2 API token in the Cloudflare dashboard
# R2 → Manage R2 API tokens → revoke the token from step 3

# Make the QR codes stop working: rotate the secret
npx wrangler secret put UPLOAD_KEY   # enter any new random string
# — or simply delete the Worker entirely:
npx wrangler delete
```

---

## Local development

```bash
cd worker
npm install
npx wrangler dev
```

The Worker runs on `http://localhost:8787`. Set `EXTRA_ALLOWED_ORIGINS = "http://localhost:8000"` in wrangler.toml and serve `upload.html` with:

```bash
python3 -m http.server 8000
```

Note: `wrangler dev` uses a local rate-limiter stub (always succeeds), so rate limiting only takes effect after deploy.
