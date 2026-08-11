#!/usr/bin/env python3
"""
Build the wedding gallery derivatives + manifest.

Takes a folder of curated originals (photos and videos, straight off the camera
or downloaded from R2) and produces web-sized copies plus a gallery.json
manifest that index.html reads.

    python3 tools/build-gallery.py --src ~/wedding-photos/curated

Outputs, by default, into gallery-build/ :

    gallery-build/photos/<slug>-full.webp     long edge 2000px
    gallery-build/photos/<slug>-thumb.webp    square 500px, cropped
    gallery-build/videos/<slug>.mp4           H.264, max 1080p, faststart
    gallery-build/videos/<slug>-poster.webp   first usable frame
    gallery-build/videos/<slug>-thumb.webp    square 500px, cropped
    gallery.json                              manifest (written to repo root)

Then upload with tools/upload-gallery.sh.

Requirements
    Pillow          pip3 install Pillow
    pillow-heif     pip3 install pillow-heif     (only if you have .HEIC files)
    ffmpeg          brew install ffmpeg          (only if you have videos)

Notes
    - All EXIF is stripped from the output, including GPS. Guest phone photos
      routinely carry the location of the venue and of wherever they were
      standing; none of that should end up on a public bucket.
    - Rotation is applied before stripping, so portrait photos stay portrait.
    - Items are sorted by capture time (EXIF DateTimeOriginal for photos,
      creation_time for videos, file mtime as a fallback).
    - Re-runs skip work whose output is already newer than its source, so you
      can add a few photos and rebuild cheaply. Use --force to redo everything.
    - Slugs come from the original filename, so URLs stay stable when you add
      more media later.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PHOTO_EXT = {'.jpg', '.jpeg', '.png', '.heic', '.heif', '.webp', '.tif', '.tiff'}
VIDEO_EXT = {'.mp4', '.mov', '.m4v', '.avi', '.mkv', '.webm'}

PLACEHOLDER_MEDIA_BASE = 'REPLACE-ME'


# ── helpers ──────────────────────────────────────────────────────────────────

def die(msg):
    print('error: ' + msg, file=sys.stderr)
    sys.exit(1)


def slugify(stem):
    s = stem.lower()
    for a, b in (('å', 'a'), ('ä', 'a'), ('ö', 'o'), ('é', 'e'), ('ü', 'u')):
        s = s.replace(a, b)
    s = re.sub(r'[^a-z0-9]+', '-', s).strip('-')
    return s or 'item'


def is_fresh(src, *outputs):
    """True if every output exists and is at least as new as the source."""
    try:
        src_m = src.stat().st_mtime
    except OSError:
        return False
    for out in outputs:
        if not out.exists() or out.stat().st_mtime < src_m:
            return False
    return True


def iso(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


# ── photos ───────────────────────────────────────────────────────────────────

def load_pillow(need_heif):
    try:
        from PIL import Image, ImageOps
    except ImportError:
        die('Pillow is not installed. Run:  pip3 install Pillow')
    if need_heif:
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except ImportError:
            die('You have .heic/.heif files but pillow-heif is not installed.\n'
                '       Run:  pip3 install pillow-heif')
    return Image, ImageOps


def photo_taken_at(img, path):
    """EXIF DateTimeOriginal, falling back to file mtime."""
    try:
        exif = img.getexif()
        # 36867 = DateTimeOriginal, 306 = DateTime
        for tag in (36867, 306):
            raw = exif.get(tag)
            if raw:
                return datetime.strptime(str(raw).strip(), '%Y:%m:%d %H:%M:%S')
        ifd = exif.get_ifd(0x8769)  # ExifIFD
        raw = ifd.get(36867)
        if raw:
            return datetime.strptime(str(raw).strip(), '%Y:%m:%d %H:%M:%S')
    except Exception:
        pass
    return datetime.fromtimestamp(path.stat().st_mtime)


def square_crop(ImageOps, img, size):
    """Centre-cropped square thumbnail."""
    return ImageOps.fit(img, (size, size), method=1, centering=(0.5, 0.5))


def build_photo(Image, ImageOps, src, out_dir, slug, args):
    full_p = out_dir / (slug + '-full.webp')
    thumb_p = out_dir / (slug + '-thumb.webp')

    with Image.open(src) as img:
        taken = photo_taken_at(img, src)

        if not args.force and is_fresh(src, full_p, thumb_p):
            with Image.open(full_p) as f:
                w, h = f.size
            print('  skip   ' + slug + ' (up to date)')
            return {'w': w, 'h': h, 'taken': taken}

        img = ImageOps.exif_transpose(img)
        if img.mode not in ('RGB', 'RGBA'):
            img = img.convert('RGB')

        # Full size: long edge capped, never upscaled.
        full = img.copy()
        full.thumbnail((args.full_size, args.full_size), Image.LANCZOS)
        # No exif= argument, so all metadata (incl. GPS) is dropped.
        full.save(full_p, 'WEBP', quality=args.full_quality, method=5)

        thumb = square_crop(ImageOps, img, args.thumb_size)
        thumb.save(thumb_p, 'WEBP', quality=args.thumb_quality, method=5)

        w, h = full.size

    print('  photo  ' + slug + '  ' + str(w) + 'x' + str(h))
    return {'w': w, 'h': h, 'taken': taken}


# ── videos ───────────────────────────────────────────────────────────────────

def ffprobe_json(src):
    out = subprocess.run(
        ['ffprobe', '-v', 'error', '-print_format', 'json',
         '-show_format', '-show_streams', str(src)],
        capture_output=True, text=True)
    if out.returncode != 0:
        return {}
    try:
        return json.loads(out.stdout)
    except ValueError:
        return {}


def video_meta(src):
    """(width, height, duration_seconds, taken_at) with sane fallbacks."""
    probe = ffprobe_json(src)
    w = h = 0
    for stream in probe.get('streams', []):
        if stream.get('codec_type') == 'video':
            w = int(stream.get('width') or 0)
            h = int(stream.get('height') or 0)
            # Phone videos are stored landscape with a rotation side-car.
            rot = 0
            try:
                rot = abs(int(stream.get('tags', {}).get('rotate', 0)))
            except (TypeError, ValueError):
                pass
            for sd in stream.get('side_data_list', []) or []:
                if 'rotation' in sd:
                    try:
                        rot = abs(int(sd['rotation']))
                    except (TypeError, ValueError):
                        pass
            if rot in (90, 270):
                w, h = h, w
            break

    fmt = probe.get('format', {})
    try:
        dur = float(fmt.get('duration') or 0)
    except ValueError:
        dur = 0.0

    taken = None
    created = (fmt.get('tags', {}) or {}).get('creation_time')
    if created:
        try:
            taken = datetime.strptime(created[:19], '%Y-%m-%dT%H:%M:%S')
        except ValueError:
            taken = None
    if taken is None:
        taken = datetime.fromtimestamp(src.stat().st_mtime)

    return w, h, dur, taken


def build_video(Image, ImageOps, src, out_dir, slug, args):
    mp4_p = out_dir / (slug + '.mp4')
    poster_p = out_dir / (slug + '-poster.webp')
    thumb_p = out_dir / (slug + '-thumb.webp')

    w, h, dur, taken = video_meta(src)

    if not args.force and is_fresh(src, mp4_p, poster_p, thumb_p):
        print('  skip   ' + slug + ' (up to date)')
        ow, oh, _, _ = video_meta(mp4_p)
        return {'w': ow or w, 'h': oh or h, 'dur': round(dur), 'taken': taken}

    # Cap the LONG edge, not the height. Capping height would shrink a portrait
    # 1080x1920 phone clip to 608x1080 — a big quality loss on exactly the
    # videos most likely to be watched on a phone. This keeps portrait at
    # 1080x1920 and landscape at 1920x1080. force_divisible_by=2 satisfies
    # libx264's even-dimension requirement in both orientations.
    edge = str(args.video_max_edge)
    vf = ('scale=min(' + edge + r'\,iw):min(' + edge + r'\,ih)'
          ':force_original_aspect_ratio=decrease:force_divisible_by=2')
    cmd = [
        'ffmpeg', '-y', '-loglevel', 'error', '-stats',
        '-i', str(src),
        '-vf', vf,
        '-c:v', 'libx264', '-crf', str(args.video_crf), '-preset', args.video_preset,
        '-profile:v', 'high', '-level', '4.0', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-b:a', '128k', '-ac', '2',
        '-map_metadata', '-1',          # strip GPS / device metadata
        '-movflags', '+faststart',      # header first, so it streams
        str(mp4_p),
    ]
    print('  video  ' + slug + '  transcoding...')
    if subprocess.run(cmd).returncode != 0:
        print('  FAILED ' + slug + ' — ffmpeg returned an error', file=sys.stderr)
        return None

    # Poster: 10% in, so we skip the black frame most clips open on.
    seek = max(0.5, dur * 0.1) if dur else 0.5
    tmp_png = out_dir / (slug + '-poster.tmp.png')
    poster_cmd = [
        'ffmpeg', '-y', '-loglevel', 'error',
        '-ss', '{:.2f}'.format(seek), '-i', str(mp4_p),
        '-frames:v', '1', str(tmp_png),
    ]
    if subprocess.run(poster_cmd).returncode != 0 or not tmp_png.exists():
        print('  warn   ' + slug + ' — could not extract a poster frame', file=sys.stderr)
        return None

    with Image.open(tmp_png) as frame:
        frame = frame.convert('RGB')
        poster = frame.copy()
        poster.thumbnail((args.full_size, args.full_size), Image.LANCZOS)
        poster.save(poster_p, 'WEBP', quality=args.full_quality, method=5)
        square_crop(ImageOps, frame, args.thumb_size).save(
            thumb_p, 'WEBP', quality=args.thumb_quality, method=5)
    tmp_png.unlink()

    ow, oh, _, _ = video_meta(mp4_p)
    print('  video  ' + slug + '  ' + str(ow) + 'x' + str(oh) +
          '  ' + str(round(dur)) + 's')
    return {'w': ow or w, 'h': oh or h, 'dur': round(dur), 'taken': taken}


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description='Build wedding gallery derivatives and manifest.')
    ap.add_argument('--src', required=True,
                    help='folder of curated originals (searched recursively)')
    ap.add_argument('--out', default='gallery-build',
                    help='output folder for derivatives (default: gallery-build)')
    ap.add_argument('--manifest', default='gallery.json',
                    help='manifest path (default: gallery.json in the repo root)')
    ap.add_argument('--media-base', default=None,
                    help='public URL prefix the files are served from, e.g. '
                         'https://pub-<hash>.r2.dev/gallery . Only needed the '
                         'first time — after that it is carried over from the '
                         'existing manifest.')
    ap.add_argument('--full-size', type=int, default=2000)
    ap.add_argument('--thumb-size', type=int, default=500)
    ap.add_argument('--full-quality', type=int, default=82)
    ap.add_argument('--thumb-quality', type=int, default=72)
    ap.add_argument('--video-max-edge', type=int, default=1920,
                    help='cap the long edge of videos (default 1920, so '
                         'portrait clips stay 1080x1920 and landscape '
                         '1920x1080)')
    ap.add_argument('--video-crf', type=int, default=23)
    ap.add_argument('--video-preset', default='slow')
    ap.add_argument('--force', action='store_true',
                    help='rebuild everything, ignoring up-to-date output')
    args = ap.parse_args()

    src_root = Path(args.src).expanduser()
    if not src_root.is_dir():
        die('--src is not a folder: ' + str(src_root))

    # Carry the media base over from the previous run so it only has to be
    # typed once. Cloudflare's r2.dev hostname contains a random hash, and
    # getting it wrong silently produces a gallery of broken images.
    # Check the manifest being written first, then the repo's own gallery.json,
    # so scratch builds to a throwaway manifest still find the URL.
    if not args.media_base:
        for candidate in (Path(args.manifest), Path(__file__).parent.parent / 'gallery.json'):
            try:
                prev = json.loads(candidate.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            found = prev.get('mediaBase')
            if found and found != PLACEHOLDER_MEDIA_BASE:
                args.media_base = found
                break
    if not args.media_base or args.media_base == PLACEHOLDER_MEDIA_BASE:
        die('no --media-base set, and none found in ' + args.manifest + '.\n'
            '       This is the public URL your bucket is served from, plus\n'
            '       the /gallery prefix. Enable the Public Development URL on\n'
            '       the bucket (R2 -> Settings -> Public access) and pass e.g.\n'
            '         --media-base https://pub-<hash>.r2.dev/gallery\n'
            '       See GALLERY.md step 2.')

    files = sorted(p for p in src_root.rglob('*')
                   if p.is_file() and not p.name.startswith('.'))
    photos = [p for p in files if p.suffix.lower() in PHOTO_EXT]
    videos = [p for p in files if p.suffix.lower() in VIDEO_EXT]
    ignored = [p for p in files if p not in photos and p not in videos]

    if not photos and not videos:
        die('no photos or videos found under ' + str(src_root))

    if videos and not shutil.which('ffmpeg'):
        die('you have ' + str(len(videos)) + ' video(s) but ffmpeg is not '
            'installed.\n       Run:  brew install ffmpeg')

    need_heif = any(p.suffix.lower() in ('.heic', '.heif') for p in photos)
    Image, ImageOps = load_pillow(need_heif)

    out_root = Path(args.out)
    photo_dir = out_root / 'photos'
    video_dir = out_root / 'videos'
    photo_dir.mkdir(parents=True, exist_ok=True)
    if videos:
        video_dir.mkdir(parents=True, exist_ok=True)

    print(str(len(photos)) + ' photo(s), ' + str(len(videos)) + ' video(s)')
    if ignored:
        print(str(len(ignored)) + ' file(s) ignored (unsupported type)')
    print('')

    used_slugs = set()

    def unique_slug(path):
        base = slugify(path.stem)
        slug = base
        n = 2
        while slug in used_slugs:
            slug = base + '-' + str(n)
            n += 1
        used_slugs.add(slug)
        return slug

    items = []

    for p in photos:
        slug = unique_slug(p)
        try:
            info = build_photo(Image, ImageOps, p, photo_dir, slug, args)
        except Exception as e:
            print('  FAILED ' + slug + ' — ' + str(e), file=sys.stderr)
            continue
        items.append({
            'type': 'photo',
            'thumb': 'photos/' + slug + '-thumb.webp',
            'full': 'photos/' + slug + '-full.webp',
            'w': info['w'],
            'h': info['h'],
            '_taken': info['taken'],
        })

    for p in videos:
        slug = unique_slug(p)
        try:
            info = build_video(Image, ImageOps, p, video_dir, slug, args)
        except Exception as e:
            print('  FAILED ' + slug + ' — ' + str(e), file=sys.stderr)
            continue
        if info is None:
            continue
        items.append({
            'type': 'video',
            'thumb': 'videos/' + slug + '-thumb.webp',
            'poster': 'videos/' + slug + '-poster.webp',
            'src': 'videos/' + slug + '.mp4',
            'w': info['w'],
            'h': info['h'],
            'dur': info['dur'],
            '_taken': info['taken'],
        })

    items.sort(key=lambda it: it['_taken'])
    for it in items:
        del it['_taken']

    manifest = {
        'mediaBase': args.media_base.rstrip('/'),
        'thumbSize': args.thumb_size,
        'count': len(items),
        'items': items,
    }
    manifest_p = Path(args.manifest)
    manifest_p.write_text(
        json.dumps(manifest, indent=1, ensure_ascii=False) + '\n',
        encoding='utf-8')

    total = sum(f.stat().st_size for f in out_root.rglob('*') if f.is_file())
    thumbs = sum(f.stat().st_size for f in out_root.rglob('*-thumb.webp'))

    print('')
    print('wrote ' + str(manifest_p) + '  (' + str(len(items)) + ' items)')
    print('output   ' + '{:.1f}'.format(total / 1e6) + ' MB total')
    print('thumbs   ' + '{:.1f}'.format(thumbs / 1e6) + ' MB  '
          '(what the grid downloads on first view)')
    print('')
    print('next:  ./tools/upload-gallery.sh')


if __name__ == '__main__':
    main()
