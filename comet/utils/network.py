import ipaddress

from fastapi import Request

IP_REQUEST_HEADERS = [
    "X-Client-Ip",
    "Cf-Connecting-Ip",
    "Do-Connecting-Ip",
    "Fastly-Client-Ip",
    "True-Client-Ip",
    "X-Real-Ip",
    "X-Cluster-Client-Ip",
    "X-Forwarded",
    "X-Forwarded-For",
    "Forwarded-For",
    "Forwarded",
    "X-Appengine-User-Ip",
    "Cf-Pseudo-IPv4",
]


def is_public_ip(ip: str):
    try:
        parsed_ip = ipaddress.ip_address(ip)
        return not parsed_ip.is_private and not parsed_ip.is_loopback
    except ValueError:
        return False


def get_client_ip(request: Request):
    for header in IP_REQUEST_HEADERS:
        header_value = request.headers.get(header)
        if not header_value:
            continue

        if header == "X-Forwarded-For":
            for ip_part in header_value.split(","):
                ip_part = ip_part.strip()
                if is_public_ip(ip_part):
                    return ip_part
        else:
            if is_public_ip(header_value):
                return header_value

    if request.client and request.client.host and is_public_ip(request.client.host):
        return request.client.host

    return ""
