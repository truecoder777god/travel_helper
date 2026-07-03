"""
Интеграция с картографическим сервисом 2ГИС.

Используются следующие публичные API 2ГИС:
- Geocoder API   (catalog.api.2gis.com/3.0/items/geocode)  — адрес -> координаты и обратно
- Routing API    (routing.api.2gis.com/routing/7.0.0/global)      — маршрут авто/пешком
- Routing API    (routing.api.2gis.com/public_transport/2.0)      — маршрут на общественном транспорте
- Static API     (static.maps.2gis.com/2.0)                       — картинка карты с точками/маршрутом
- Веб-ссылка на построение маршрута в 2ГИС (2gis.ru/routeSearch/...)

Документация: https://docs.2gis.com/en/api/navigation/routing/overview
"""

import logging

import aiohttp

from config import DGIS_API_KEY

GEOCODER_URL = "https://catalog.api.2gis.com/3.0/items/geocode"
ROUTING_URL = "https://routing.api.2gis.com/routing/7.0.0/global"
PUBLIC_TRANSPORT_URL = "https://routing.api.2gis.com/public_transport/2.0"
STATIC_MAP_URL = "https://static.maps.2gis.com/2.0"

# Сопоставление кнопок выбора транспорта в боте -> тип для Routing API
TRANSPORT_MAP = {
    "🚗 На авто": "driving",
    "🚌 Общественный транспорт": "public_transport",
    "🚶 Пешком": "walking",
}

# Сопоставление кнопок выбора транспорта в боте -> тип маршрута для веб-ссылки 2ГИС
ROUTE_LINK_TYPE_MAP = {
    "🚗 На авто": "car",
    "🚌 Общественный транспорт": "bus",
    "🚶 Пешком": "pedestrian",
}

# Типы общественного транспорта, которые запрашиваем у 2ГИС
PUBLIC_TRANSPORT_TYPES = [
    "bus", "trolleybus", "tram", "shuttle_bus",
    "metro", "suburban_train", "monorail",
    "funicular_railway", "cablecar", "river_transport",
]

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=12)


async def geocode_address(address: str) -> tuple[float, float] | None:
    """
    Превращает текстовый адрес в координаты (lat, lon) через Geocoder API 2ГИС.
    Возвращает None, если адрес не найден или произошла ошибка сети.
    """
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
    """
    Превращает координаты (например, из точки, отправленной на карте в Telegram)
    в читаемый адрес через Geocoder API 2ГИС (обратное геокодирование).
    Возвращает None, если ничего не нашлось или произошла ошибка сети.
    """
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


def build_static_map_url(
    dest_lat: float, dest_lon: float,
    user_lat: float | None = None, user_lon: float | None = None,
    size: str = "650x450",
) -> str:
    """
    Собирает ссылку на картинку карты (Static API 2ГИС) с меткой места назначения
    и, если известна геолокация пользователя, — линией маршрута между двумя точками.
    """
    params = [f"s={size}"]

    if user_lat is not None and user_lon is not None:
        # Точка пользователя (зелёная, №1) и точка назначения (красная, №2) + линия между ними
        params.append(f"pt={user_lat},{user_lon}~c:gn~n:1")
        params.append(f"pt={dest_lat},{dest_lon}~c:rd~n:2")
        params.append(f"ls={user_lat},{user_lon},{dest_lat},{dest_lon}~w:5~c:2b6fdb")
    else:
        # Известна только точка назначения — просто центрируем карту на ней
        params.append(f"pt={dest_lat},{dest_lon}~c:rd~s:l")
        params.append(f"c={dest_lat},{dest_lon}")
        params.append("z=15")

    params.append(f"key={DGIS_API_KEY}")
    return STATIC_MAP_URL + "?" + "&".join(params)


def build_route_link(
    dest_lat: float, dest_lon: float,
    user_lat: float | None = None, user_lon: float | None = None,
    transport_label: str | None = None,
) -> str:
    """
    Собирает ссылку на 2gis.ru, которая открывает построенный маршрут
    (в приложении 2ГИС, если оно установлено, либо в браузере).
    Если геолокация пользователя неизвестна, точка отправления не указывается —
    2ГИС в таком случае подставит текущее местоположение самостоятельно.
    """
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
    """
    Возвращает предполагаемое время в пути в минутах с учётом текущей
    ситуации на дороге, либо None при ошибке запроса к 2ГИС.
    """
    api_transport = TRANSPORT_MAP.get(transport_label, "driving")

    try:
        if api_transport == "public_transport":
            return await _get_public_transport_time(lat_from, lon_from, lat_to, lon_to)
        return await _get_routing_time(lat_from, lon_from, lat_to, lon_to, api_transport)
    except Exception as e:
        logging.error(f"Ошибка расчёта маршрута через 2ГИС: {e}")
        return None


async def _get_routing_time(lat_from, lon_from, lat_to, lon_to, transport: str) -> int | None:
    """Маршрут для авто ('driving') или пешком ('walking') через Routing API."""
    body = {
        "points": [
            {"type": "stop", "lon": lon_from, "lat": lat_from},
            {"type": "stop", "lon": lon_to, "lat": lat_to},
        ],
        "transport": transport,
        "route_mode": "fastest",
        # Учитываем пробки в реальном времени только для авто
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

    # Ответ 2ГИС Routing API — объект с ключом "result" (список маршрутов внутри),
    # а не голый список, как ожидалось раньше.
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
    """Маршрут на общественном транспорте через /public_transport/2.0."""
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

    # API может вернуть либо список маршрутов, либо объект с ключом "routes" —
    # обрабатываем оба варианта на случай изменений в ответе.
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
