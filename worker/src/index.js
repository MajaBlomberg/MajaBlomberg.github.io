/**
 * Wedding Uploads Worker
 *
 * POST /presign  { key, filename, contentType, fileSize }
 *   → { uploadUrl, objectKey }
 *
 * The browser then PUTs the file bytes directly to R2 via the presigned URL,
 * bypassing the 100 MB Worker body limit.
 *
 * IMPORTANT: Content-Type is intentionally NOT signed into the presigned URL.
 * Signing it would cause R2 to return SignatureDoesNotMatch when the browser
 * sends it as an unsigned header.
 */

import { AwsClient } from "aws4fetch";

// ── Constants ────────────────────────────────────────────────────────────────

const MAX_FILE_SIZE = 500 * 1024 * 1024; // 500 MB

const ALLOWED_CONTENT_TYPES = new Set([
  "image/jpeg",
  "image/png",
  "image/heic",
  "image/heif",
  "image/webp",
  "image/gif",
  "video/mp4",
  "video/quicktime",
  "video/x-m4v",
]);

// Presigned URL TTL (seconds)
const PRESIGN_EXPIRES = 3600;

// ── Helpers ───────────────────────────────────────────────────────────────────

/**
 * Sanitize a filename so it is safe to embed in an S3-style object key.
 * Strips everything that is not [a-zA-Z0-9._-].
 */
function sanitizeFilename(name) {
  return String(name).replace(/[^a-zA-Z0-9._-]/g, "_").slice(0, 200);
}

/**
 * Build the CORS headers object for a given request origin.
 * Returns the correct Access-Control-Allow-Origin for whitelisted origins,
 * or omits the header entirely for unlisted ones (browser will block — correct).
 */
function corsHeaders(origin, env) {
  const primaryOrigin = "https://mikaelmajabrollop2026.se";

  // Build the set of allowed origins at runtime
  const allowed = new Set([primaryOrigin]);
  const extra = (env.EXTRA_ALLOWED_ORIGINS || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  extra.forEach((o) => allowed.add(o));

  const allowedOrigin = allowed.has(origin) ? origin : primaryOrigin;

  return {
    "Access-Control-Allow-Origin": allowedOrigin,
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
    Vary: "Origin",
  };
}

/** Wrap a JSON response with CORS headers. */
function jsonResponse(body, status, cors) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json",
      ...cors,
    },
  });
}

// ── Main handler ──────────────────────────────────────────────────────────────

export default {
  async fetch(request, env) {
    const origin = request.headers.get("Origin") || "";
    const cors = corsHeaders(origin, env);

    // ── OPTIONS preflight ─────────────────────────────────────────────────────
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: cors });
    }

    // Only POST /presign (any path works — the worker has no router)
    if (request.method !== "POST") {
      return jsonResponse({ error: "Method not allowed" }, 405, cors);
    }

    // ── 1. Validate UPLOAD_KEY ─────────────────────────────────────────────────
    let body;
    try {
      body = await request.json();
    } catch {
      return jsonResponse({ error: "Invalid JSON body" }, 400, cors);
    }

    const { key, filename, contentType, fileSize } = body;

    if (!key || key !== env.UPLOAD_KEY) {
      return jsonResponse({ error: "Unauthorized" }, 401, cors);
    }

    // ── 2. Upload window check ─────────────────────────────────────────────────
    const now = Date.now();
    const openMs = Date.parse(env.UPLOAD_OPEN_UTC);
    const closeMs = Date.parse(env.UPLOAD_CLOSE_UTC);

    if (isNaN(openMs) || isNaN(closeMs)) {
      return jsonResponse(
        { error: "Server misconfiguration: invalid upload window" },
        500,
        cors
      );
    }

    if (now < openMs) {
      return jsonResponse(
        { error: "Uploads are not open yet. See you at the wedding!" },
        403,
        cors
      );
    }
    if (now > closeMs) {
      return jsonResponse(
        {
          error:
            "The upload window has closed. Contact the couple to share your photos.",
        },
        403,
        cors
      );
    }

    // ── 3. Content-type allowlist ──────────────────────────────────────────────
    if (!contentType || !ALLOWED_CONTENT_TYPES.has(contentType.toLowerCase())) {
      return jsonResponse(
        {
          error: `File type not allowed: ${contentType}. Allowed: images (JPEG, PNG, HEIC, WebP, GIF) and videos (MP4, MOV, M4V).`,
        },
        415,
        cors
      );
    }

    // ── 4. File size check ─────────────────────────────────────────────────────
    const size = Number(fileSize);
    if (!Number.isFinite(size) || size <= 0) {
      return jsonResponse({ error: "Invalid fileSize" }, 400, cors);
    }
    if (size > MAX_FILE_SIZE) {
      return jsonResponse(
        {
          error: `File too large: ${Math.round(
            size / 1024 / 1024
          )} MB. Maximum is 500 MB.`,
        },
        413,
        cors
      );
    }

    // ── 5. Rate limiting (per IP) ──────────────────────────────────────────────
    const ip =
      request.headers.get("CF-Connecting-IP") ||
      request.headers.get("X-Forwarded-For") ||
      "unknown";

    // Fail open if the binding is missing/misconfigured — a broken rate
    // limiter must never block wedding-day uploads.
    let withinLimit = true;
    try {
      if (env.UPLOAD_RATE_LIMITER) {
        ({ success: withinLimit } = await env.UPLOAD_RATE_LIMITER.limit({
          key: ip,
        }));
      }
    } catch (e) {
      console.error("Rate limiter error (failing open):", e);
    }

    if (!withinLimit) {
      return jsonResponse(
        { error: "Too many requests. Please wait a minute and try again." },
        429,
        cors
      );
    }

    // ── 6. Total storage cap ───────────────────────────────────────────────────
    // Sum the bucket's current size via the binding; refuse the presign if this
    // file would push it past MAX_TOTAL_GB. Runs after the rate limiter so an
    // abuser can't make us list the bucket more than 10×/min per IP.
    const maxTotalBytes =
      Number(env.MAX_TOTAL_GB || "20") * 1024 * 1024 * 1024;
    let usedBytes = 0;
    let cursor;
    do {
      const page = await env.BUCKET.list({ cursor, limit: 1000 });
      for (const obj of page.objects) usedBytes += obj.size;
      cursor = page.truncated ? page.cursor : undefined;
    } while (cursor);

    if (usedBytes + size > maxTotalBytes) {
      return jsonResponse(
        {
          error:
            "Lagringen är full — tack för alla bilder! / Storage is full — thank you for all the photos!",
        },
        507,
        cors
      );
    }

    // ── 7. Generate object key ─────────────────────────────────────────────────
    const safeFilename = sanitizeFilename(filename || "upload");
    const objectKey = `uploads/${Date.now()}-${crypto.randomUUID()}-${safeFilename}`;

    // ── 8. Presign the PUT URL via aws4fetch ───────────────────────────────────
    //
    // CRITICAL: do NOT include Content-Type in the signed headers.
    // R2 requires the browser to send Content-Type unsigned on the PUT.
    // Signing it here causes SignatureDoesNotMatch on the R2 side.
    //
    const r2Endpoint = `https://${env.ACCOUNT_ID}.r2.cloudflarestorage.com`;
    const putUrl = new URL(`${r2Endpoint}/${env.BUCKET_NAME}/${objectKey}`);
    // aws4fetch has no expiresIn option — expiry is controlled by the
    // X-Amz-Expires query param (defaults to 86400 if absent).
    putUrl.searchParams.set("X-Amz-Expires", String(PRESIGN_EXPIRES));

    const aws = new AwsClient({
      accessKeyId: env.R2_ACCESS_KEY_ID,
      secretAccessKey: env.R2_SECRET_ACCESS_KEY,
      service: "s3",
      region: "auto",
    });

    const signed = await aws.sign(
      new Request(putUrl, { method: "PUT" }),
      {
        aws: {
          signQuery: true,
          // Do NOT sign Content-Type — the browser sends it unsigned
        },
      }
    );

    return jsonResponse(
      { uploadUrl: signed.url, objectKey },
      200,
      cors
    );
  },
};
