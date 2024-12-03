import base64
import orjson

from RTN import SettingsModel, BestRanking

from comet.utils.models import ConfigModel, default_config


def config_check(b64config: str):
    try:
        config = orjson.loads(base64.b64decode(b64config).decode())
        validated_config = ConfigModel(**config)
        validated_config = validated_config.model_dump()
        validated_config["rtnSettings"] = SettingsModel(
            **validated_config["rtnSettings"]
        )
        validated_config["rtnRanking"] = BestRanking(**validated_config["rtnRanking"])
        return validated_config
    except:
        return default_config  # if it doesn't pass, return default config


def bytes_to_size(bytes: int):
    sizes = ["Bytes", "KB", "MB", "GB", "TB"]
    if bytes == 0:
        return "0 Byte"

    i = 0
    while bytes >= 1024 and i < len(sizes) - 1:
        bytes /= 1024
        i += 1

    return f"{round(bytes, 2)} {sizes[i]}"


def size_to_bytes(size_str: str):
    sizes = ["kb", "mb", "gb", "tb"]

    value, unit = size_str.split()
    value = float(value)
    unit = unit.lower()

    if unit not in sizes:
        return None

    multiplier = 1024 ** sizes.index(unit)
    return int(value * multiplier)
