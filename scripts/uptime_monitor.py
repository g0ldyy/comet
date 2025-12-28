import asyncio
import sys
from datetime import datetime, timezone

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
WEBHOOK_AVATAR = "https://i.ibb.co/LVGNJ0s/icon.jpg"

# ===========================================


C_RESET = "\033[0m"
C_DIM = "\033[2m"
C_BOLD = "\033[1m"
C_GREEN = "\033[38;5;114m"
C_YELLOW = "\033[38;5;221m"
C_RED = "\033[38;5;203m"
C_CYAN = "\033[38;5;117m"
C_GRAY = "\033[38;5;245m"


class InstanceStatus:
    def __init__(
        self,
        url: str,
        is_online: bool,
        manifest_ok: bool,
        search_ok: bool,
        response_time: float,
        error: str | None,
    ):
        self.url = url
        self.is_online = is_online
        self.manifest_ok = manifest_ok
        self.search_ok = search_ok
        self.response_time = response_time
        self.error = error


async def check_instance(session: aiohttp.ClientSession, url: str) -> InstanceStatus:
    start = asyncio.get_event_loop().time()
    manifest_ok = False
    search_ok = False
    error = None

    try:
        async with session.get(f"{url}/manifest.json") as resp:
            if resp.status == 200:
                data = await resp.json()
                manifest_ok = "id" in data and "resources" in data
    except Exception as e:
        error = str(e)[:60]

    if manifest_ok:
        try:
            async with session.get(f"{url}/e30=/stream/movie/{IMDB_ID}.json") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    streams = data.get("streams", [])
                    search_ok = len(streams) > 0 or resp.status == 200
        except Exception as e:
            if not error:
                error = str(e)[:60]

    response_time = asyncio.get_event_loop().time() - start

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


async def send_webhook(session: aiohttp.ClientSession, payload: dict) -> bool:
    try:
        async with session.post(WEBHOOK_URL, json=payload) as resp:
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


async def monitor_instance(session: aiohttp.ClientSession, url: str) -> None:
    while True:
        status = await check_instance(session, url)
        print_status(status)

        payload = build_embed(status)
        await send_webhook(session, payload)

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
    print_header()

    timeout = aiohttp.ClientTimeout(total=TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        tasks = [monitor_instance(session, url) for url in INSTANCES]
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n  {C_GREEN}✓{C_RESET} Stopped\n")
        sys.exit(0)
