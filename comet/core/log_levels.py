STANDARD_LOG_LEVELS = {
    "DEBUG": {"color": "#DC5F00", "icon": "🕸️", "loguru_color": "<fg #DC5F00>"},
    "INFO": {"color": "#FC5F39", "icon": "📰", "loguru_color": "<fg #FC5F39>"},
    "WARNING": {"color": "#DC5F00", "icon": "⚠️", "loguru_color": "<fg #DC5F00>"},
    "ERROR": {"color": "#ff0000", "icon": "❌", "loguru_color": "<fg #ff0000>"},
    "CRITICAL": {"color": "#ff0000", "icon": "💀", "loguru_color": "<fg #ff0000>"},
}

CUSTOM_LOG_LEVELS = {
    "COMET": {
        "color": "#7871d6",
        "icon": "🌠",
        "loguru_color": "<fg #7871d6>",
        "no": 50,
    },
    "API": {"color": "#006989", "icon": "👾", "loguru_color": "<fg #006989>", "no": 45},
    "SCRAPER": {
        "color": "#d6bb71",
        "icon": "👻",
        "loguru_color": "<fg #d6bb71>",
        "no": 40,
    },
    "STREAM": {
        "color": "#d171d6",
        "icon": "🎬",
        "loguru_color": "<fg #d171d6>",
        "no": 35,
    },
    "DATABASE": {
        "color": "#5aa5d9",
        "icon": "💾",
        "loguru_color": "<fg #5aa5d9>",
        "no": 32,
    },
    "LOCK": {
        "color": "#71d6d6",
        "icon": "🔒",
        "loguru_color": "<fg #71d6d6>",
        "no": 30,
    },
    "SESSION": {
        "color": "#71d6d6",
        "icon": "🔒",
        "loguru_color": "<fg #71d6d6>",
        "no": 30,
    },
    "BACKGROUND_SCRAPER": {
        "color": "#5fba64",
        "icon": "🏭",
        "loguru_color": "<fg #5fba64>",
        "no": 25,
    },
    "DB_EXPORT": {
        "color": "#4A90E2",
        "icon": "📤",
        "loguru_color": "<fg #4A90E2>",
        "no": 20,
    },
    "DB_IMPORT": {
        "color": "#E24A90",
        "icon": "📥",
        "loguru_color": "<fg #E24A90>",
        "no": 20,
    },
}

ALL_LOG_LEVELS = {**STANDARD_LOG_LEVELS, **CUSTOM_LOG_LEVELS}


def get_level_info(level_name: str):
    return ALL_LOG_LEVELS.get(level_name, {"color": "#ffffff", "icon": "📝"})


def get_level_color(level_name: str):
    return get_level_info(level_name)["color"]


def get_level_icon(level_name: str):
    return get_level_info(level_name)["icon"]
