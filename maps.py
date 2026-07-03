import logging
import re

import aiohttp

from config import DGIS_API_KEY

GEOCODER_URL = "https://catalog.api.2gis.com/3.0/items/geocode" # — адрес -> координаты и обратно
ROUTING_URL = "https://routing.api.2gis.com/routing/7.0.0/global"# маршрут авто/пешком
PUBLIC_TRANSPORT_URL = "https://routing.api.2gis.com/public_transport/2.0"# маршрут на общественном транспорте
STATIC_MAP_URL = "https://static.maps.2gis.com/2.0"# картинка карты с точками/маршрутом

#для Routing API
TRANSPORT_MAP = {
    "🚗 На авто": "driving",
    "🚌 Общественный транспорт": "public_transport",
    "🚶 Пешком": "walking",
}
#для веб-ссылки 2ГИС
ROUTE_LINK_TYPE_MAP = {
    "🚗 На авто": "car",
    "🚌 Общественный транспорт": "bus",
    "🚶 Пешком": "pedestrian",
}

# Типы общественного транспорта
PUBLIC_TRANSPORT_TYPES = [
    "bus", "trolleybus", "tram", "shuttle_bus",
    "metro", "suburban_train", "monorail",
    "funicular_railway", "cablecar", "river_transport",
]

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=12)


async def geocode_address(address: str) -> tuple[float, float] | None:
    params = {
        "q": address,
        "fields": "items.point",
        "key": DGIS_API_KEY,
    }
    try:
        async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
            async with session.get(GEOCODER_URL, params=params) as resp:
                if resp.status != 200:
                    logging.error(f"2ГИС Geocoder вернул статус {resp.status}")
                    return None
                data = await resp.json()
    except Exception as e:
        logging.error(f"Ошибка запроса к 2ГИС Geocoder: {e}")
        return None

    items = (data.get("result") or {}).get("items") or []
    if not items:
        return None

    point = items[0].get("point")
    if not point:
        return None

    return point["lat"], point["lon"]


async def reverse_geocode(lat: float, lon: float) -> str | None:
    params = {
        "lat": lat,
        "lon": lon,
        "fields": "items.address_name,items.full_name",
        "key": DGIS_API_KEY,
    }
    try:
        async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
            async with session.get(GEOCODER_URL, params=params) as resp:
                if resp.status != 200:
                    logging.error(f"2ГИС Geocoder (реверс) вернул статус {resp.status}")
                    return None
                data = await resp.json()
    except Exception as e:
        logging.error(f"Ошибка обратного геокодирования через 2ГИС: {e}")
        return None

    items = (data.get("result") or {}).get("items") or []
    if not items:
        return None

    item = items[0]
    return item.get("full_name") or item.get("address_name")

_LINESTRING_RE = re.compile(r"LINESTRING\(([^)]+)\)")

# Максимум точек линии в ссылке на статическую карту (чтобы URL не разрастался бесконечно)
_MAX_LINE_POINTS = 150


def _parse_linestring_points(wkt: str) -> list[tuple[float, float]]:
    match = _LINESTRING_RE.search(wkt or "")
    if not match:
        return []

    points: list[tuple[float, float]] = []
    for pair in match.group(1).split(","):
        coords = pair.strip().split(" ")
        if len(coords) < 2:
            continue
        lon_str, lat_str = coords[0], coords[1]  # третье значение (если есть) — высота, игнорируем
        try:
            points.append((float(lat_str), float(lon_str)))
        except ValueError:
            continue
    return points


def _simplify_points(points: list[tuple[float, float]], max_points: int = _MAX_LINE_POINTS) -> list[tuple[float, float]]:
    if len(points) <= max_points:
        return points

    step = len(points) / max_points
    simplified = [points[round(i * step)] for i in range(max_points - 1)]
    simplified.append(points[-1])
    return simplified


async def get_route_geometry(
    lat_from: float, lon_from: float,
    lat_to: float, lon_to: float,
    transport_label: str,
) -> list[tuple[float, float]] | None:

    api_transport = TRANSPORT_MAP.get(transport_label, "driving")
    if api_transport == "public_transport":
        return None

    body = {
        "points": [
            {"type": "stop", "lon": lon_from, "lat": lat_from},
            {"type": "stop", "lon": lon_to, "lat": lat_to},
        ],
        "transport": api_transport,
        "route_mode": "fastest",
        "traffic_mode": "jam" if api_transport == "driving" else "statistics",
        "output": "detailed",
    }
    params = {"key": DGIS_API_KEY}

    try:
        async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
            async with session.post(ROUTING_URL, params=params, json=body) as resp:
                if resp.status != 200:
                    logging.error(f"2ГИС Routing (geometry) вернул статус {resp.status}: {await resp.text()}")
                    return None
                data = await resp.json()
    except Exception as e:
        logging.error(f"Ошибка запроса геометрии маршрута через 2ГИС: {e}")
        return None

    if isinstance(data, dict):
        routes = data.get("result") or []
    elif isinstance(data, list):
        routes = data
    else:
        routes = []

    if not routes:
        return None

    route = routes[0]
    points: list[tuple[float, float]] = []

    begin_path = ((route.get("begin_pedestrian_path") or {}).get("geometry") or {}).get("selection")
    if begin_path:
        points.extend(_parse_linestring_points(begin_path))

    for maneuver in route.get("maneuvers") or []:
        outcoming_path = maneuver.get("outcoming_path") or {}
        for geometry_item in outcoming_path.get("geometry") or []:
            points.extend(_parse_linestring_points(geometry_item.get("selection")))

    end_path = ((route.get("end_pedestrian_path") or {}).get("geometry") or {}).get("selection")
    if end_path:
        points.extend(_parse_linestring_points(end_path))

    if len(points) < 2:
        return None

    return _simplify_points(points)


def build_static_map_url(
    dest_lat: float, dest_lon: float,
    user_lat: float | None = None, user_lon: float | None = None,
    size: str = "650x450",
    route_points: list[tuple[float, float]] | None = None,
) -> str:
    params = [f"s={size}"]

    if user_lat is not None and user_lon is not None:
        params.append(f"pt={user_lat},{user_lon}~c:gn~n:1")
        params.append(f"pt={dest_lat},{dest_lon}~c:rd~n:2")

        if route_points and len(route_points) >= 2:
            line_coords = ",".join(f"{lat},{lon}" for lat, lon in route_points)
            params.append(f"ls={line_coords}~w:5~c:2b6fdb")
        # если геометрии маршрута нет — линию не рисуем, остаются только 2 точки
    else:
        params.append(f"pt={dest_lat},{dest_lon}~c:rd~s:l")
        params.append(f"c={dest_lat},{dest_lon}")
        params.append("z=15")

    params.append(f"key={DGIS_API_KEY}")
    return STATIC_MAP_URL + "?" + "&".join(params)


async def build_route_static_map_url(
    dest_lat: float, dest_lon: float,
    user_lat: float, user_lon: float,
    transport_label: str,
    size: str = "650x450",
) -> str:

    route_points = await get_route_geometry(user_lat, user_lon, dest_lat, dest_lon, transport_label)
    return build_static_map_url(
        dest_lat, dest_lon,
        user_lat=user_lat, user_lon=user_lon,
        size=size,
        route_points=route_points,
    )


def build_route_link(
    dest_lat: float, dest_lon: float,
    user_lat: float | None = None, user_lon: float | None = None,
    transport_label: str | None = None,
) -> str:
    route_type = ROUTE_LINK_TYPE_MAP.get(transport_label, "car")
    to_part = f"to/{dest_lon},{dest_lat}"
    if user_lat is not None and user_lon is not None:
        from_part = f"from/{user_lon},{user_lat}/"
    else:
        from_part = ""
    return f"https://2gis.ru/routeSearch/rsType/{route_type}/{from_part}{to_part}"


async def get_travel_time_minutes(
    lat_from: float, lon_from: float,
    lat_to: float, lon_to: float,
    transport_label: str,
) -> int | None:
    api_transport = TRANSPORT_MAP.get(transport_label, "driving")

    try:
        if api_transport == "public_transport":
            return await _get_public_transport_time(lat_from, lon_from, lat_to, lon_to)
        return await _get_routing_time(lat_from, lon_from, lat_to, lon_to, api_transport)
    except Exception as e:
        logging.error(f"Ошибка расчёта маршрута через 2ГИС: {e}")
        return None


async def _get_routing_time(lat_from, lon_from, lat_to, lon_to, transport: str) -> int | None:
    body = {
        "points": [
            {"type": "stop", "lon": lon_from, "lat": lat_from},
            {"type": "stop", "lon": lon_to, "lat": lat_to},
        ],
        "transport": transport,
        "route_mode": "fastest",
        "traffic_mode": "jam" if transport == "driving" else "statistics",
        "output": "summary",
    }
    params = {"key": DGIS_API_KEY}

    async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
        async with session.post(ROUTING_URL, params=params, json=body) as resp:
            if resp.status != 200:
                logging.error(f"2ГИС Routing вернул статус {resp.status}: {await resp.text()}")
                return None
            data = await resp.json()

    if isinstance(data, dict):
        routes = data.get("result") or []
    elif isinstance(data, list):
        routes = data
    else:
        routes = []

    if not routes:
        return None

    duration_seconds = routes[0].get("duration")
    if duration_seconds is None:
        return None

    return max(1, round(duration_seconds / 60))


async def _get_public_transport_time(lat_from, lon_from, lat_to, lon_to) -> int | None:
    body = {
        "source": {"point": {"lat": lat_from, "lon": lon_from}},
        "target": {"point": {"lat": lat_to, "lon": lon_to}},
        "transport": PUBLIC_TRANSPORT_TYPES,
        "locale": "ru",
    }
    params = {"key": DGIS_API_KEY}

    async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
        async with session.post(PUBLIC_TRANSPORT_URL, params=params, json=body) as resp:
            if resp.status != 200:
                logging.error(f"2ГИС Public Transport вернул статус {resp.status}: {await resp.text()}")
                return None
            data = await resp.json()

    if isinstance(data, list):
        routes = data
    elif isinstance(data, dict):
        routes = data.get("routes") or []
    else:
        routes = []

    candidates = [r for r in routes if isinstance(r.get("total_duration"), (int, float))]
    if not candidates:
        return None

    best = min(candidates, key=lambda r: r["total_duration"])
    return max(1, round(best["total_duration"] / 60))
