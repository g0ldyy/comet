# uv run python scripts/generate_status_videos.py \
#     --background surfer.mp4 \
#     --stremthru-root stremthru \
#     --output-dir comet/assets/status_videos \
#     --scope essential \
#     --overwrite \
#     --clean-output \
#     --code INVALID_ACCOUNT_OR_PASSWORD \
#     --code UNAUTHENTICATED \
#     --code PROXY_LIMIT_REACHED \
#     --code DEBRID_SYNC_TRIGGERED \
#     --code DEBRID_SYNC_ALREADY_RUNNING \
#     --code MEDIA_NOT_CACHED_YET \
#     --font-file /usr/share/fonts/noto/NotoSans-Black.ttf \
#     --width 1280 --height 720 \
#     --fps 18 \
#     --duration 8 \
#     --crf 24 \
#     --maxrate 1200k --bufsize 2400k \
#     --preset veryslow

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from textwrap import fill

try:
    from comet.utils.status_keys import \
        normalize_status_key as normalize_status_key_runtime
except ModuleNotFoundError:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from comet.utils.status_keys import \
        normalize_status_key as normalize_status_key_runtime

STATUS_DECLARATION_PATTERN = re.compile(
    r"\b[A-Za-z0-9_]+\s+ErrorCode\s*=\s*\"([^\"]+)\""
)

SCOPE_ALL = "all"
SCOPE_DEBRID = "debrid"
SCOPE_ESSENTIAL = "essential"

STORE_STATUS_FILE_PATTERN = re.compile(r"^store/[^/]+/error\.go$")
SERVER_STATUS_FILE_PATTERN = re.compile(r"^internal/server/error\.go$")
CAMEL_BOUNDARY_PATTERN = re.compile(r"([a-z0-9])([A-Z])")

ESSENTIAL_STORE_STATUS_KEYS = {
    "ACCOUNT_INVALID",
    "ACTIVE_LIMIT",
    "AUTH_BAD_APIKEY",
    "AUTH_BLOCKED",
    "AUTH_ERROR",
    "AUTH_MISSING_APIKEY",
    "AUTH_USER_BANNED",
    "BADTOKEN",
    "BAD_TOKEN",
    "COOLDOWN_LIMIT",
    "DOWNLOAD_SERVER_ERROR",
    "EXPIRED_TOKEN",
    "FREE_TRIAL_LIMIT_REACHED",
    "INVALID_ACCOUNT_OR_PASSWORD",
    "INVALID_CLIENT",
    "LINK_OFFLINE",
    "MAINTENANCE",
    "MAGNET_MUST_BE_PREMIUM",
    "MEDIA_NOT_CACHED_YET",
    "MONTHLY_LIMIT",
    "MUST_BE_PREMIUM",
    "NO_AUTH",
    "NO_SERVER",
    "NO_SERVERS_AVAILABLE_ERROR",
    "PLAN_RESTRICTED_FEATURE",
    "SERVER_ERROR",
    "UNAUTHENTICATED",
    "UNAUTHORIZED_CLIENT",
}

DEFAULT_STATUS_MESSAGES = {
    "UNKNOWN": "Unexpected provider response. Please try again.",
    "BAD_GATEWAY": "Gateway issue while contacting the debrid provider.",
    "BAD_REQUEST": "Invalid request sent to the debrid provider.",
    "CONFLICT": "This request conflicts with the current provider state.",
    "FORBIDDEN": "This action is not allowed for this account.",
    "INTERNAL_SERVER_ERROR": "Internal provider issue. Please retry shortly.",
    "LOCKED": "This item is currently locked by the provider.",
    "METHOD_NOT_ALLOWED": "This action is not supported for this endpoint.",
    "NOT_FOUND": "Requested stream was not found.",
    "PAYMENT_REQUIRED": "A premium debrid subscription is required.",
    "SERVICE_UNAVAILABLE": "Debrid provider is temporarily unavailable.",
    "TOO_MANY_REQUESTS": "Too many requests right now. Please retry shortly.",
    "UNAUTHORIZED": "Your API key is invalid or expired.",
    "UNPROCESSABLE_ENTITY": "The provider could not process this request.",
    "UNSUPPORTED_MEDIA_TYPE": "Unsupported media type for this request.",
    "STORE_LIMIT_EXCEEDED": "Your account quota has been reached.",
    "STORE_MAGNET_INVALID": "This torrent cannot be processed by the provider.",
    "STORE_NAME_INVALID": "Invalid provider selected.",
    "STORE_SERVER_DOWN": "Debrid provider is currently unavailable.",
    "NO_AUTH": "No API key was provided for this provider.",
    "BAD_TOKEN": "Your API token is invalid or expired.",
    "AUTH_ERROR": "Provider authentication failed.",
    "EXPIRED_TOKEN": "Your API token has expired.",
    "AUTH_BAD_APIKEY": "Your API key is invalid.",
    "AUTH_MISSING_APIKEY": "No API key was provided.",
    "AUTH_BLOCKED": "This API key is blocked.",
    "AUTH_USER_BANNED": "This account is banned.",
    "ACCOUNT_INVALID": "This account is invalid.",
    "UNAUTHORIZED_CLIENT": "This client is not authorized.",
    "INVALID_CLIENT": "Invalid client credentials.",
    "MONTHLY_LIMIT": "Monthly quota reached.",
    "COOLDOWN_LIMIT": "Cooldown active. Please retry later.",
    "ACTIVE_LIMIT": "Too many active transfers on this account.",
    "LINK_OFFLINE": "The source link is offline.",
    "MUST_BE_PREMIUM": "A premium debrid subscription is required.",
    "MAGNET_MUST_BE_PREMIUM": "A premium debrid subscription is required.",
    "MEDIA_NOT_CACHED_YET": "The media you tried to play is not cached yet on this debrid service. Please try again soon.",
    "DEBRID_SYNC_TRIGGERED": "Debrid account sync started. New results will appear shortly.",
    "DEBRID_SYNC_ALREADY_RUNNING": "A debrid account sync is already running. Please try again in a few minutes.",
    "PROXY_LIMIT_REACHED": "Too many simultaneous streams on this proxy. Close an active stream and try again.",
}


def normalize_status_key(status_key: str) -> str:
    return normalize_status_key_runtime(status_key) or "UNKNOWN"


def split_identifier_words(value: str) -> list[str]:
    value = CAMEL_BOUNDARY_PATTERN.sub(r"\1 \2", value)
    value = value.replace("_", " ").replace("-", " ")
    return [part for part in value.split() if part]


def build_fallback_message(raw_key: str, normalized_key: str) -> str:
    upper = normalized_key
    if any(token in upper for token in ("AUTH", "TOKEN", "UNAUTHORIZED")):
        return "Authentication failed. Check your API key."
    if any(token in upper for token in ("LIMIT", "MAX", "QUOTA", "COOLDOWN")):
        return "Usage limit reached. Please retry later."
    if any(
        token in upper for token in ("SERVER", "UNAVAILABLE", "OFFLINE", "MAINTENANCE")
    ):
        return "Provider is currently unavailable."
    if any(token in upper for token in ("PREMIUM", "PAYMENT")):
        return "A premium subscription is required."
    if any(token in upper for token in ("INVALID", "BAD", "NOT_FOUND", "NOTFOUND")):
        return "This request is invalid or unavailable."

    words = split_identifier_words(raw_key)
    if words:
        readable = " ".join(words).strip()
        return readable[0].upper() + readable[1:] + "."
    return "Unexpected provider response."


def is_relevant_status_file(rel_path: str, scope: str) -> bool:
    if scope == SCOPE_ALL:
        return rel_path.endswith(".go")
    if SERVER_STATUS_FILE_PATTERN.fullmatch(rel_path):
        return True
    if STORE_STATUS_FILE_PATTERN.fullmatch(rel_path):
        return True
    return False


def is_essential_store_status_key(raw_key: str) -> bool:
    normalized = normalize_status_key(raw_key)
    return (
        normalized in DEFAULT_STATUS_MESSAGES
        or normalized in ESSENTIAL_STORE_STATUS_KEYS
    )


def collect_status_keys(stremthru_root: Path, scope: str) -> dict[str, set[str]]:
    keys_by_normalized: dict[str, set[str]] = {}

    for go_file in stremthru_root.rglob("*.go"):
        rel_path = go_file.relative_to(stremthru_root).as_posix()
        if not is_relevant_status_file(rel_path, scope):
            continue

        source = go_file.read_text(encoding="utf-8", errors="ignore")
        matches = STATUS_DECLARATION_PATTERN.findall(source)
        if not matches:
            continue

        for raw_key in matches:
            if scope == SCOPE_ESSENTIAL and STORE_STATUS_FILE_PATTERN.fullmatch(
                rel_path
            ):
                if not is_essential_store_status_key(raw_key):
                    continue
            normalized = normalize_status_key(raw_key)
            keys_by_normalized.setdefault(normalized, set()).add(raw_key)

    if "UNKNOWN" not in keys_by_normalized:
        keys_by_normalized["UNKNOWN"] = {"UNKNOWN"}

    return keys_by_normalized


def escape_filter_value(value: str) -> str:
    return (
        value.replace("\\", r"\\")
        .replace(":", r"\:")
        .replace("'", r"\'")
        .replace("%", r"\%")
    )


def escape_drawtext_text(value: str) -> str:
    return (
        value.replace("\\", r"\\")
        .replace(":", r"\:")
        .replace("'", r"\'")
        .replace("%", r"\%")
        .replace(",", r"\,")
    )


def layout_message(message: str, width: int, height: int) -> tuple[list[str], int]:
    clean_message = " ".join(message.strip().split())
    if not clean_message:
        clean_message = "Unexpected provider response."

    font_size = max(34, min(70, int(height * 0.08)))
    min_font_size = max(20, int(height * 0.035))

    while font_size >= min_font_size:
        approx_chars_per_line = max(14, int((width * 0.86) / (font_size * 0.56)))
        lines = fill(clean_message, width=approx_chars_per_line).splitlines()
        max_lines = max(2, int((height * 0.72) / (font_size * 1.25)))

        if len(lines) <= max_lines:
            return lines, font_size

        font_size -= 2

    approx_chars_per_line = max(14, int((width * 0.86) / (min_font_size * 0.56)))
    lines = fill(clean_message, width=approx_chars_per_line).splitlines()
    max_lines = max(2, int((height * 0.72) / (min_font_size * 1.25)))
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        if len(lines[-1]) > 3:
            lines[-1] = lines[-1][:-3].rstrip() + "..."
    return lines, min_font_size


def resolve_font_file(font_file: str | None) -> str | None:
    if not font_file:
        return None

    candidate = Path(font_file)
    if not candidate.is_file():
        raise FileNotFoundError(f"Font file not found: {font_file}")
    return str(candidate)


def build_filter(
    lines: list[str], width: int, height: int, font_file: str | None, fontsize: int
) -> str:
    font_part = (
        f"fontfile={escape_filter_value(font_file)}"
        if font_file
        else "font='Sans Bold'"
    )

    filters = [
        f"scale=w={width}:h={height}:force_original_aspect_ratio=increase",
        f"crop={width}:{height}",
        "format=yuv420p",
    ]

    line_height = int(fontsize * 1.25)
    block_height = (len(lines) - 1) * line_height
    first_y = int((height - block_height) / 2)
    outline_width = max(4, round(fontsize * 0.14))
    shadow_offset = max(2, round(fontsize * 0.06))

    for index, line in enumerate(lines):
        y = first_y + (index * line_height)
        filters.append(
            (
                f"drawtext={font_part}:text='{escape_drawtext_text(line)}'"
                f":x=(w-text_w)/2:y={y}:fontsize={fontsize}"
                f":fontcolor=white:borderw={outline_width}:bordercolor=black@0.92"
                f":shadowcolor=black@0.85:shadowx={shadow_offset}:shadowy={shadow_offset}:fix_bounds=1"
            )
        )

    return ",".join(filters)


def encode_status_video(
    ffmpeg_bin: str,
    background: Path,
    output: Path,
    message: str,
    *,
    width: int,
    height: int,
    duration: int,
    fps: int,
    crf: int,
    maxrate: str,
    bufsize: str,
    preset: str,
    font_file: str | None,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    lines, fontsize = layout_message(message, width, height)

    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-stream_loop",
        "-1",
        "-i",
        str(background),
        "-vf",
        build_filter(lines, width, height, font_file, fontsize),
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-profile:v",
        "high",
        "-level:v",
        "4.1",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-crf",
        str(crf),
        "-maxrate",
        maxrate,
        "-bufsize",
        bufsize,
        "-r",
        str(fps),
        "-g",
        str(fps * 2),
        "-keyint_min",
        str(fps),
        "-sc_threshold",
        "0",
        "-t",
        str(duration),
        "-y",
        str(output),
    ]
    subprocess.run(cmd, check=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate user-friendly status videos from a background video."
    )
    parser.add_argument(
        "--background",
        required=True,
        help="Path to the background video (for example: surfer.mp4).",
    )
    parser.add_argument(
        "--stremthru-root",
        default="stremthru",
        help="Path to the StremThru source tree (required for scope-derived keys).",
    )
    parser.add_argument(
        "--output-dir",
        default="comet/assets/status_videos",
        help="Directory where status video assets are written.",
    )
    parser.add_argument("--width", type=int, default=1280, help="Output width.")
    parser.add_argument("--height", type=int, default=720, help="Output height.")
    parser.add_argument(
        "--duration",
        type=int,
        default=10,
        help="Duration in seconds for each output video.",
    )
    parser.add_argument("--fps", type=int, default=24, help="Output frame rate.")
    parser.add_argument(
        "--crf", type=int, default=21, help="H.264 constant quality factor."
    )
    parser.add_argument(
        "--maxrate",
        default="3M",
        help="Encoder max bitrate for streaming-friendly ABR behavior.",
    )
    parser.add_argument(
        "--bufsize",
        default="6M",
        help="Encoder VBV buffer size.",
    )
    parser.add_argument(
        "--preset",
        default="slow",
        help="x264 preset (ultrafast..veryslow).",
    )
    parser.add_argument(
        "--font-file",
        default=None,
        help="Optional font file path for drawtext.",
    )
    parser.add_argument(
        "--ffmpeg-bin",
        default="ffmpeg",
        help="ffmpeg binary name or absolute path.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing files if present.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Generate only the first N keys in batch mode (0 = all).",
    )
    parser.add_argument(
        "--code",
        action="append",
        default=[],
        help="Generate specific status key(s) in addition to scope-derived keys.",
    )
    parser.add_argument(
        "--scope",
        choices=[SCOPE_ESSENTIAL, SCOPE_DEBRID, SCOPE_ALL],
        default=SCOPE_ESSENTIAL,
        help=(
            "Status key scope in batch mode: "
            "'essential' (default), 'debrid' (all debrid files), 'all' (all declarations)."
        ),
    )
    parser.add_argument(
        "--messages-file",
        default=None,
        help='Optional JSON map {"STATUS_KEY": "Custom message"} to override batch messages.',
    )
    parser.add_argument(
        "--clean-output",
        action="store_true",
        help="Delete stale .mp4 files in output directory that are not part of this run.",
    )
    parser.add_argument(
        "--single-file",
        default=None,
        help="Single mode: output file name (or path) for one custom video.",
    )
    parser.add_argument(
        "--single-message",
        default=None,
        help="Single mode: custom message displayed in the video.",
    )
    return parser.parse_args()


def load_message_overrides(messages_file: str | None) -> dict[str, str]:
    if not messages_file:
        return {}

    path = Path(messages_file)
    if not path.is_file():
        raise FileNotFoundError(f"Messages file not found: {messages_file}")

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("Messages file must be a JSON object.")

    normalized_map = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        normalized_map[normalize_status_key(key)] = value.strip()
    return normalized_map


def resolve_batch_message(
    normalized_key: str,
    raw_keys: set[str],
    overrides: dict[str, str],
) -> str:
    if normalized_key in overrides:
        return overrides[normalized_key]
    if normalized_key in DEFAULT_STATUS_MESSAGES:
        return DEFAULT_STATUS_MESSAGES[normalized_key]

    preferred_raw = sorted(raw_keys, key=lambda item: (len(item), item))[0]
    return build_fallback_message(preferred_raw, normalized_key)


def resolve_single_output_path(output_dir: Path, single_file: str) -> Path:
    candidate = Path(single_file)
    if not candidate.suffix:
        candidate = candidate.with_suffix(".mp4")
    if candidate.is_absolute():
        return candidate
    return output_dir / candidate


def main() -> int:
    args = parse_args()

    background = Path(args.background).resolve()
    output_dir = Path(args.output_dir).resolve()
    font_file = resolve_font_file(args.font_file)

    if not background.is_file():
        raise FileNotFoundError(f"Background video not found: {background}")

    subprocess.run(
        [args.ffmpeg_bin, "-hide_banner", "-version"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    output_dir.mkdir(parents=True, exist_ok=True)

    single_mode = bool(args.single_file or args.single_message)
    if single_mode:
        if not args.single_file or not args.single_message:
            raise ValueError(
                "--single-file and --single-message must be used together."
            )

        output_path = resolve_single_output_path(output_dir, args.single_file)
        encode_status_video(
            args.ffmpeg_bin,
            background,
            output_path,
            args.single_message,
            width=args.width,
            height=args.height,
            duration=args.duration,
            fps=args.fps,
            crf=args.crf,
            maxrate=args.maxrate,
            bufsize=args.bufsize,
            preset=args.preset,
            font_file=font_file,
        )
        print(f"generated {output_path}")
        return 0

    overrides = load_message_overrides(args.messages_file)
    requested = {normalize_status_key(code) for code in args.code}
    stremthru_root = Path(args.stremthru_root).resolve()
    has_stremthru_root = stremthru_root.is_dir()

    keys_by_normalized: dict[str, set[str]] = {}
    keys: list[str] = []

    if has_stremthru_root:
        keys_by_normalized = collect_status_keys(stremthru_root, args.scope)
        keys = sorted(keys_by_normalized.keys())
    elif not requested:
        raise FileNotFoundError(
            f"StremThru source directory not found: {stremthru_root}. "
            "Provide --stremthru-root for scope-derived keys or pass --code values."
        )
    else:
        print(
            f"warning: StremThru source directory not found at {stremthru_root}; "
            "generating only explicitly requested --code keys."
        )

    if requested:
        for key in requested:
            if key not in keys_by_normalized:
                keys_by_normalized[key] = {key}
        keys = sorted(set(keys) | requested)

    if args.limit > 0:
        keys = keys[: args.limit]

    if not keys:
        raise RuntimeError("No status declarations found for the current selection.")

    if args.clean_output:
        keep_files = {f"{key}.mp4" for key in keys}
        for stale_file in output_dir.glob("*.mp4"):
            if stale_file.name not in keep_files:
                stale_file.unlink()
                print(f"removed {stale_file}")

    generated = []

    for key in keys:
        output = output_dir / f"{key}.mp4"
        if output.exists() and not args.overwrite:
            generated.append(key)
            continue

        message = resolve_batch_message(key, keys_by_normalized[key], overrides)
        encode_status_video(
            args.ffmpeg_bin,
            background,
            output,
            message,
            width=args.width,
            height=args.height,
            duration=args.duration,
            fps=args.fps,
            crf=args.crf,
            maxrate=args.maxrate,
            bufsize=args.bufsize,
            preset=args.preset,
            font_file=font_file,
        )
        generated.append(key)
        print(f"generated {output}")

    print(f"ready: {len(generated)} assets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
