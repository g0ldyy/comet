import asyncio
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

import aiohttp

# ============== CONFIGURATION ==============

INSTANCES = [
    "https://your-instance-1.example.com",
    "https://your-instance-2.example.com",
]

IMDB_ID = "tt30472557"
CHECK_INTERVAL = 300
TIMEOUT = 30

WEBHOOK_URL = "https://discord.com/api/webhooks/YOUR_WEBHOOK_ID/YOUR_WEBHOOK_TOKEN"
WEBHOOK_USERNAME = "Comet Monitor"
WEBHOOK_AVATAR = "https://raw.githubusercontent.com/g0ldyy/comet/refs/heads/main/comet/assets/icon.png"

# ===========================================


C_RESET = "\033[0m"
C_DIM = "\033[2m"
C_BOLD = "\033[1m"
C_GREEN = "\033[38;5;114m"
C_YELLOW = "\033[38;5;221m"
C_RED = "\033[38;5;203m"
C_CYAN = "\033[38;5;117m"
C_GRAY = "\033[38;5;245m"


@dataclass(frozen=True, slots=True)
class InstanceStatus:
    url: str
    is_online: bool
    manifest_ok: bool
    search_ok: bool
    response_time: float
    error: str | None


def validate_http_url(value: str, label: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{label} must be a non-empty URL")
    normalized = value.strip().rstrip("/")
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{label} must use an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(
            f"{label} must not contain credentials, a query, or a fragment"
        )
    return normalized


def validate_configuration() -> tuple[tuple[str, ...], str]:
    if not INSTANCES:
        raise ValueError("INSTANCES must contain at least one URL")
    if type(CHECK_INTERVAL) is not int or CHECK_INTERVAL <= 0:
        raise ValueError("CHECK_INTERVAL must be a positive integer")
    if type(TIMEOUT) is not int or TIMEOUT <= 0:
        raise ValueError("TIMEOUT must be a positive integer")

    instances = tuple(validate_http_url(url, "instance URL") for url in INSTANCES)
    if len(set(instances)) != len(instances):
        raise ValueError("INSTANCES must not contain duplicate URLs")
    webhook_url = validate_http_url(WEBHOOK_URL, "WEBHOOK_URL")
    return instances, webhook_url


async def check_instance(session: aiohttp.ClientSession, url: str) -> InstanceStatus:
    start = asyncio.get_running_loop().time()
    manifest_ok = False
    search_ok = False
    error = None

    try:
        async with session.get(f"{url}/manifest.json") as resp:
            if resp.status == 200:
                data = await resp.json()
                manifest_ok = (
                    type(data) is dict
                    and type(data.get("id")) is str
                    and bool(data["id"].strip())
                    and type(data.get("resources")) is list
                )
                if not manifest_ok:
                    error = "invalid manifest response schema"
    except Exception as e:
        error = str(e)[:60]

    if manifest_ok:
        try:
            async with session.get(f"{url}/stream/movie/{IMDB_ID}.json") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    search_ok = (
                        type(data) is dict
                        and type(data.get("streams")) is list
                        and all(type(stream) is dict for stream in data["streams"])
                    )
                    if not search_ok:
                        error = "invalid stream response schema"
        except Exception as e:
            if not error:
                error = str(e)[:60]

    response_time = asyncio.get_running_loop().time() - start

    return InstanceStatus(
        url=url,
        is_online=manifest_ok,
        manifest_ok=manifest_ok,
        search_ok=search_ok,
        response_time=response_time,
        error=error,
    )


def build_embed(status: InstanceStatus) -> dict:
    instance_name = status.url.replace("https://", "").replace("http://", "")
    timestamp = datetime.now(timezone.utc).isoformat()

    if status.is_online and status.search_ok:
        color = 0x43B581
        description = "● Instance is healthy and responding"
    elif status.is_online:
        color = 0xFAA61A
        description = "◐ Instance is online but search may be slow"
    else:
        color = 0xF04747
        description = "○ Instance is unreachable"

    manifest_value = "✓ Valid" if status.manifest_ok else "✗ Failed"
    search_value = "✓ Working" if status.search_ok else "✗ Failed"

    fields = [
        {
            "name": "Response Time",
            "value": f"```\n{status.response_time:.2f}s\n```",
            "inline": True,
        },
        {
            "name": "Manifest",
            "value": f"```\n{manifest_value}\n```",
            "inline": True,
        },
        {
            "name": "Search API",
            "value": f"```\n{search_value}\n```",
            "inline": True,
        },
    ]

    if status.error:
        fields.append(
            {
                "name": "Error",
                "value": f"```\n{status.error}\n```",
                "inline": False,
            }
        )

    return {
        "embeds": [
            {
                "title": f"☄️ {instance_name}",
                "url": status.url,
                "description": description,
                "color": color,
                "fields": fields,
                "footer": {
                    "icon_url": WEBHOOK_AVATAR,
                },
                "timestamp": timestamp,
            }
        ],
        "username": WEBHOOK_USERNAME,
        "avatar_url": WEBHOOK_AVATAR,
    }


async def send_webhook(
    session: aiohttp.ClientSession, webhook_url: str, payload: dict
) -> bool:
    try:
        async with session.post(webhook_url, json=payload) as resp:
            return resp.status in (200, 204)
    except Exception:
        return False


def print_status(status: InstanceStatus) -> None:
    timestamp = datetime.now().strftime("%H:%M:%S")
    instance_name = status.url.replace("https://", "").replace("http://", "")

    if status.is_online and status.search_ok:
        indicator = f"{C_GREEN}●{C_RESET}"
        state = "operational"
        state_color = C_GREEN
    elif status.is_online:
        indicator = f"{C_YELLOW}◐{C_RESET}"
        state = "degraded"
        state_color = C_YELLOW
    else:
        indicator = f"{C_RED}○{C_RESET}"
        state = "offline"
        state_color = C_RED

    latency = f"{status.response_time:>6.2f}s"

    manifest_check = (
        f"{C_GREEN}✓{C_RESET}" if status.manifest_ok else f"{C_RED}✗{C_RESET}"
    )
    search_check = f"{C_GREEN}✓{C_RESET}" if status.search_ok else f"{C_RED}✗{C_RESET}"

    print(
        f"  {C_DIM}{timestamp}{C_RESET}  {indicator}  {C_BOLD}{instance_name:<40}{C_RESET}  "
        f"{state_color}{state:<11}{C_RESET}  {C_CYAN}{latency}{C_RESET}  {C_GRAY}[{manifest_check}{C_GRAY}/{search_check}{C_GRAY}]{C_RESET}"
    )

    if status.error:
        print(f"             {C_DIM}└─ {status.error}{C_RESET}")


async def monitor_instance(
    session: aiohttp.ClientSession, url: str, webhook_url: str
) -> None:
    while True:
        status = await check_instance(session, url)
        print_status(status)

        payload = build_embed(status)
        if not await send_webhook(session, webhook_url, payload):
            print(f"             {C_YELLOW}└─ webhook delivery failed{C_RESET}")

        await asyncio.sleep(CHECK_INTERVAL)


def print_header() -> None:
    print()
    print(f"  {C_BOLD}☄️  Comet Uptime Monitor{C_RESET}")
    print(f"  {C_DIM}{'─' * 50}{C_RESET}")
    print(
        f"  {C_GRAY}Instances: {C_RESET}{len(INSTANCES)}  {C_GRAY}│  Interval: {C_RESET}{CHECK_INTERVAL}s"
    )
    print()
    for inst in INSTANCES:
        name = inst.replace("https://", "").replace("http://", "")
        print(f"  {C_DIM}•{C_RESET} {name}")
    print()
    print(f"  {C_DIM}{'─' * 50}{C_RESET}")
    print()


async def main() -> None:
    instances, webhook_url = validate_configuration()
    print_header()

    timeout = aiohttp.ClientTimeout(total=TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        tasks = [monitor_instance(session, url, webhook_url) for url in instances]
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n  {C_GREEN}✓{C_RESET} Stopped\n")
        sys.exit(0)
