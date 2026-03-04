#!/usr/bin/env python3
import json
import math
import os
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8790"))
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "8"))

PROVIDER_PRIORITY = [p.strip().lower() for p in os.getenv("PROVIDER_PRIORITY", "scraperapi,browserless,rapidapi,serpapi").split(",") if p.strip()]
MIN_RESULTS = int(os.getenv("MIN_RESULTS", "7"))

SERPAPI_KEY = os.getenv("SERPAPI_KEY", "").strip()
SERPAPI_BASE = "https://serpapi.com/search.json"
SCRAPERAPI_KEY = os.getenv("SCRAPERAPI_KEY", "").strip()
SCRAPERAPI_PREMIUM = os.getenv("SCRAPERAPI_PREMIUM", "1").strip().lower() in ("1", "true", "yes", "y")
SCRAPERAPI_ULTRA_PREMIUM = os.getenv("SCRAPERAPI_ULTRA_PREMIUM", "0").strip().lower() in ("1", "true", "yes", "y")
SCRAPERAPI_TIMEOUT_SEC = int(os.getenv("SCRAPERAPI_TIMEOUT_SEC", "20"))
BROWSERLESS_TOKEN = os.getenv("BROWSERLESS_TOKEN", "").strip()
BROWSERLESS_BASE = os.getenv("BROWSERLESS_BASE", "https://chrome.browserless.io").strip()

AMADEUS_HOST = os.getenv("AMADEUS_HOST", "test.api.amadeus.com").strip()
AMADEUS_CLIENT_ID = os.getenv("AMADEUS_CLIENT_ID", "").strip()
AMADEUS_CLIENT_SECRET = os.getenv("AMADEUS_CLIENT_SECRET", "").strip()
AMADEUS_MAX_RESULTS = int(os.getenv("AMADEUS_MAX_RESULTS", "30"))

RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "").strip()
RAPIDAPI_HOST = os.getenv("RAPIDAPI_HOST", "").strip()
RAPIDAPI_SEARCH_PATH = os.getenv("RAPIDAPI_SEARCH_PATH", "").strip()
RAPIDAPI_MAX_PAGES = int(os.getenv("RAPIDAPI_MAX_PAGES", "8"))

CACHE_DB_PATH = os.getenv("CACHE_DB_PATH", "./stay_cache.sqlite3")
CACHE_TTL_SEC = int(os.getenv("CACHE_TTL_SEC", "1800"))
CACHE_KEY_VERSION = "v2"
RAPIDAPI_RETRY_ATTEMPTS = int(os.getenv("RAPIDAPI_RETRY_ATTEMPTS", "3"))
RAPIDAPI_BACKOFF_BASE_SEC = float(os.getenv("RAPIDAPI_BACKOFF_BASE_SEC", "1.0"))
PROVIDER_RETRY_ATTEMPTS = int(os.getenv("PROVIDER_RETRY_ATTEMPTS", "2"))
PROVIDER_BACKOFF_BASE_SEC = float(os.getenv("PROVIDER_BACKOFF_BASE_SEC", "1.0"))

CACHE_LOCK = Lock()
TOKEN_LOCK = Lock()
AMADEUS_TOKEN = ""
AMADEUS_TOKEN_EXP = 0


def now_ts():
    return int(time.time())


def parse_json_body(handler):
    length = int(handler.headers.get("Content-Length", "0") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


def write_json(handler, status, payload):
    raw = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.end_headers()
    handler.wfile.write(raw)


def init_cache():
    with CACHE_LOCK:
        conn = sqlite3.connect(CACHE_DB_PATH)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS search_cache (
                  cache_key TEXT PRIMARY KEY,
                  created_at INTEGER NOT NULL,
                  payload TEXT NOT NULL
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_cache_created_at ON search_cache(created_at)")
            conn.commit()
        finally:
            conn.close()


def cache_get(cache_key):
    with CACHE_LOCK:
        conn = sqlite3.connect(CACHE_DB_PATH)
        try:
            cur = conn.cursor()
            cur.execute("SELECT created_at, payload FROM search_cache WHERE cache_key=?", (cache_key,))
            row = cur.fetchone()
            if not row:
                return None
            created_at, payload = row
            if now_ts() - int(created_at) > CACHE_TTL_SEC:
                cur.execute("DELETE FROM search_cache WHERE cache_key=?", (cache_key,))
                conn.commit()
                return None
            return json.loads(payload)
        finally:
            conn.close()


def cache_set(cache_key, payload):
    with CACHE_LOCK:
        conn = sqlite3.connect(CACHE_DB_PATH)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO search_cache(cache_key, created_at, payload)
                VALUES (?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET created_at=excluded.created_at, payload=excluded.payload
                """,
                (cache_key, now_ts(), json.dumps(payload)),
            )
            conn.commit()
        finally:
            conn.close()


def http_get_json(url, headers=None, timeout=18):
    req = Request(url, headers=headers or {}, method="GET")
    try:
        with urlopen(req, timeout=timeout) as res:
            return json.loads(res.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="ignore").strip()
        except Exception:
            body = ""
        if body:
            raise HTTPError(exc.url, exc.code, f"{exc.reason} | {body[:220]}", exc.hdrs, exc.fp)
        raise


def http_post_form(url, form_data, headers=None, timeout=18):
    body = urlencode(form_data).encode("utf-8")
    merged = {"Content-Type": "application/x-www-form-urlencoded"}
    if headers:
        merged.update(headers)
    req = Request(url, data=body, headers=merged, method="POST")
    with urlopen(req, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8"))


def http_post_json(url, payload, headers=None, timeout=18):
    body = json.dumps(payload).encode("utf-8")
    merged = {"Content-Type": "application/json"}
    if headers:
        merged.update(headers)
    req = Request(url, data=body, headers=merged, method="POST")
    try:
        with urlopen(req, timeout=timeout) as res:
            return json.loads(res.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="ignore").strip()
        except Exception:
            body = ""
        if body:
            raise HTTPError(exc.url, exc.code, f"{exc.reason} | {body[:220]}", exc.hdrs, exc.fp)
        raise


def parse_float(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = re.sub(r"[^0-9.\-]", "", str(value))
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def extract_number(value):
    val = parse_float(value)
    if val is None:
        return None
    return round(val, 2)


def normalize_bool(value):
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in ("1", "true", "yes", "y", "free cancellation", "refundable")


def haversine_miles(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2):
        return None
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def geocode_city(city):
    params = urlencode({"q": city, "format": "json", "limit": 1})
    url = f"https://nominatim.openstreetmap.org/search?{params}"
    data = http_get_json(url, headers={"User-Agent": "stay-scanner/1.0"})
    if not data:
        return None, None
    return parse_float(data[0].get("lat")), parse_float(data[0].get("lon"))


def likely_family_friendly(name, amenities, property_type, bedrooms, rooms, beds, travelers):
    text = " ".join([
        str(name or ""),
        str(property_type or ""),
        " ".join([str(x) for x in (amenities or [])]),
    ]).lower()
    keywords = ("family", "kitchen", "suite", "play", "pool", "kids", "crib", "washer")
    if any(k in text for k in keywords):
        return True
    if (beds or 0) >= 2:
        return True
    if (bedrooms or 0) >= 2:
        return True
    if (rooms or 0) >= 2:
        return True
    if property_type in ("apartment", "vacation_home", "holiday_home", "house") and travelers >= 2:
        return True
    return False


def likely_safe_area(review_score, review_count, text_blob):
    text = str(text_blob or "").lower()
    if "unsafe" in text:
        return False
    # Booking-style vacation rentals can have low review counts even when rated
    # highly. Treat high-score reviewed properties as reasonably safe.
    if review_score is not None and review_score >= 8.0 and (review_count or 0) >= 1:
        return True
    if review_score is not None and review_score >= 8.8:
        return True
    if "safe" in text or "secure" in text:
        return True
    return False


def normalize_property(raw, source, city_lat, city_lon, travelers, rooms):
    name = raw.get("name") or raw.get("title") or raw.get("hotel_name")
    lat = parse_float(raw.get("latitude") or raw.get("lat") or ((raw.get("gps_coordinates") or {}).get("latitude") if isinstance(raw.get("gps_coordinates"), dict) else None))
    lng = parse_float(raw.get("longitude") or raw.get("lng") or ((raw.get("gps_coordinates") or {}).get("longitude") if isinstance(raw.get("gps_coordinates"), dict) else None))

    review_score = extract_number(raw.get("rating") or raw.get("overall_rating") or raw.get("review_score") or (raw.get("overall_rating") if isinstance(raw.get("overall_rating"), (int, float, str)) else None))
    review_count = int(parse_float(raw.get("reviews") or raw.get("reviews_count") or raw.get("review_count") or 0) or 0)

    price_per_night = extract_number(
        raw.get("price_per_night")
        or raw.get("nightly_rate")
        or raw.get("rate_per_night")
        or (raw.get("total_rate", {}) or {}).get("lowest")
        or (raw.get("rate_per_night", {}) or {}).get("lowest")
        or raw.get("price")
    )
    total_price = extract_number(
        raw.get("total_price")
        or raw.get("total_rate")
        or (raw.get("total_rate", {}) or {}).get("lowest")
        or raw.get("price_total")
    )
    if total_price is None and price_per_night is not None:
        total_price = price_per_night

    currency = raw.get("currency") or "USD"
    free_cancellation = normalize_bool(raw.get("free_cancellation") or raw.get("is_free_cancellation") or raw.get("refundable"))

    amenities = raw.get("amenities") if isinstance(raw.get("amenities"), list) else []
    property_type = str(raw.get("property_type") or raw.get("type") or raw.get("kind") or "hotel").lower()
    address = raw.get("address") or raw.get("location") or raw.get("full_address") or ""
    url = raw.get("link") or raw.get("url") or raw.get("property_token") or ""

    distance = haversine_miles(city_lat, city_lon, lat, lng)
    family_friendly = likely_family_friendly(
        name,
        amenities,
        property_type,
        int(parse_float(raw.get("bedrooms") or 0) or 0),
        rooms,
        int(parse_float(raw.get("beds") or 0) or 0),
        travelers,
    )
    safe_area = likely_safe_area(review_score, review_count, f"{name} {address} {' '.join([str(x) for x in amenities])}")

    return {
        "id": f"{source}:{raw.get('id') or name or url or str(hash(json.dumps(raw, sort_keys=True)))}",
        "name": name or "Unknown property",
        "source": source,
        "property_type": property_type,
        "price_per_night": price_per_night,
        "total_price": total_price,
        "currency": currency,
        "review_score": review_score,
        "review_count": review_count,
        "free_cancellation": free_cancellation,
        "family_friendly": family_friendly,
        "safe_area": safe_area,
        "distance_miles": distance,
        "lat": lat,
        "lng": lng,
        "address": address,
        "url": url,
        "amenities": amenities,
    }


def configured_providers():
    available = []
    for provider in PROVIDER_PRIORITY:
        if provider == "scraperapi" and SCRAPERAPI_KEY and SERPAPI_KEY:
            available.append("scraperapi")
        elif provider == "browserless" and BROWSERLESS_TOKEN and SERPAPI_KEY:
            available.append("browserless")
        elif provider == "rapidapi" and RAPIDAPI_KEY and RAPIDAPI_HOST:
            available.append("rapidapi")
        elif provider == "amadeus" and AMADEUS_CLIENT_ID and AMADEUS_CLIENT_SECRET:
            available.append("amadeus")
        elif provider == "serpapi" and SERPAPI_KEY:
            available.append("serpapi")
    if not available and SERPAPI_KEY:
        available = ["serpapi"]
    return available


def get_amadeus_token():
    global AMADEUS_TOKEN, AMADEUS_TOKEN_EXP
    with TOKEN_LOCK:
        if AMADEUS_TOKEN and now_ts() < AMADEUS_TOKEN_EXP - 60:
            return AMADEUS_TOKEN
        url = f"https://{AMADEUS_HOST}/v1/security/oauth2/token"
        data = http_post_form(url, {
            "grant_type": "client_credentials",
            "client_id": AMADEUS_CLIENT_ID,
            "client_secret": AMADEUS_CLIENT_SECRET,
        })
        token = data.get("access_token", "")
        expires_in = int(data.get("expires_in", 1799) or 1799)
        if not token:
            raise RuntimeError("Amadeus auth failed")
        AMADEUS_TOKEN = token
        AMADEUS_TOKEN_EXP = now_ts() + expires_in
        return token


def amadeus_search(city_lat, city_lon, check_in, check_out, travelers, rooms):
    if not (AMADEUS_CLIENT_ID and AMADEUS_CLIENT_SECRET):
        return []
    token = get_amadeus_token()

    by_geo_url = (
        f"https://{AMADEUS_HOST}/v1/reference-data/locations/hotels/by-geocode?"
        + urlencode({
            "latitude": city_lat,
            "longitude": city_lon,
            "radius": 40,
            "radiusUnit": "KM",
            "hotelSource": "ALL",
        })
    )
    hotels_json = http_get_json(by_geo_url, headers={"Authorization": f"Bearer {token}"})
    hotel_ids = [h.get("hotelId") for h in (hotels_json.get("data") or []) if h.get("hotelId")]
    hotel_ids = hotel_ids[:50]
    if not hotel_ids:
        return []

    offers_url = (
        f"https://{AMADEUS_HOST}/v3/shopping/hotel-offers?"
        + urlencode({
            "hotelIds": ",".join(hotel_ids),
            "checkInDate": check_in,
            "checkOutDate": check_out,
            "adults": travelers,
            "roomQuantity": rooms,
            "bestRateOnly": "true",
            "currency": "USD",
        })
    )
    offers_json = http_get_json(offers_url, headers={"Authorization": f"Bearer {token}"})
    rows = []
    for item in (offers_json.get("data") or [])[:AMADEUS_MAX_RESULTS]:
        hotel = item.get("hotel") or {}
        offer = (item.get("offers") or [{}])[0]
        price = offer.get("price") or {}
        policies = offer.get("policies") or {}
        cancel = policies.get("cancellation") or {}
        raw = {
            "id": hotel.get("hotelId"),
            "name": hotel.get("name"),
            "latitude": hotel.get("latitude"),
            "longitude": hotel.get("longitude"),
            "rating": hotel.get("rating"),
            "review_count": 30,
            "price_per_night": (price.get("variations") or {}).get("average", {}).get("base") if isinstance(price.get("variations"), dict) else None,
            "total_price": price.get("total"),
            "currency": price.get("currency"),
            "free_cancellation": str(cancel).lower().find("free") >= 0 or str(cancel).lower().find("refundable") >= 0,
            "property_type": "hotel",
            "address": ", ".join(hotel.get("address", {}).get("lines") or []),
            "url": offer.get("self"),
            "amenities": hotel.get("amenities") or [],
        }
        rows.append(raw)
    return rows


def serpapi_search(city, check_in, check_out, travelers, rooms):
    if not SERPAPI_KEY:
        return []

    queries = [
        {
            "engine": "google_hotels",
            "q": f"{city} hotels",
            "check_in_date": check_in,
            "check_out_date": check_out,
            "adults": travelers,
            "currency": "USD",
            "gl": "us",
            "hl": "en",
            "api_key": SERPAPI_KEY,
        },
        {
            "engine": "google_vacation_rentals",
            "q": f"{city} vacation rentals",
            "check_in_date": check_in,
            "check_out_date": check_out,
            "adults": travelers,
            "api_key": SERPAPI_KEY,
            "currency": "USD",
            "hl": "en",
            "gl": "us",
        },
    ]

    rows = []
    for q in queries:
        url = f"{SERPAPI_BASE}?{urlencode(q)}"
        data = http_get_json(url)
        for key in ("properties", "vacation_rentals", "organic_results"):
            if isinstance(data.get(key), list):
                rows.extend(data.get(key))
                break
    return rows


def scraperapi_search(city, check_in, check_out, travelers, rooms):
    if not (SCRAPERAPI_KEY and SERPAPI_KEY):
        return []
    rows = []
    queries = [
        {
            "engine": "google_hotels",
            "q": f"{city} hotels",
            "check_in_date": check_in,
            "check_out_date": check_out,
            "adults": travelers,
            "currency": "USD",
            "gl": "us",
            "hl": "en",
            "api_key": SERPAPI_KEY,
        },
        {
            "engine": "google_vacation_rentals",
            "q": f"{city} vacation rentals",
            "check_in_date": check_in,
            "check_out_date": check_out,
            "adults": travelers,
            "currency": "USD",
            "gl": "us",
            "hl": "en",
            "api_key": SERPAPI_KEY,
        },
    ]
    for q in queries:
        serp_url = f"{SERPAPI_BASE}?{urlencode(q)}"
        params = {
            "api_key": SCRAPERAPI_KEY,
            "url": serp_url,
        }
        if SCRAPERAPI_PREMIUM:
            params["premium"] = "true"
        if SCRAPERAPI_ULTRA_PREMIUM:
            params["ultra_premium"] = "true"
        proxy_url = "http://api.scraperapi.com/?" + urlencode(params)
        data = http_get_json(proxy_url, timeout=max(5, SCRAPERAPI_TIMEOUT_SEC))
        for key in ("properties", "vacation_rentals", "organic_results"):
            if isinstance(data.get(key), list):
                rows.extend(data.get(key))
                break
    return rows


def browserless_search(city, check_in, check_out, travelers, rooms):
    if not (BROWSERLESS_TOKEN and SERPAPI_KEY):
        return []
    rows = []
    queries = [
        {
            "engine": "google_hotels",
            "q": f"{city} hotels",
            "check_in_date": check_in,
            "check_out_date": check_out,
            "adults": travelers,
            "currency": "USD",
            "gl": "us",
            "hl": "en",
            "api_key": SERPAPI_KEY,
        },
        {
            "engine": "google_vacation_rentals",
            "q": f"{city} vacation rentals",
            "check_in_date": check_in,
            "check_out_date": check_out,
            "adults": travelers,
            "currency": "USD",
            "gl": "us",
            "hl": "en",
            "api_key": SERPAPI_KEY,
        },
    ]
    for q in queries:
        serp_url = f"{SERPAPI_BASE}?{urlencode(q)}"
        base_candidates = [BROWSERLESS_BASE, "https://production-sfo.browserless.io", "https://chrome.browserless.io"]
        data = None
        last_exc = None
        for base in base_candidates:
            content_url_qs = f"{base.rstrip('/')}/content?token={BROWSERLESS_TOKEN}"
            try:
                data = http_post_json(content_url_qs, {"url": serp_url}, timeout=45)
                break
            except HTTPError as exc:
                last_exc = exc
                # Retry with bearer token auth variant.
                try:
                    content_url_hdr = f"{base.rstrip('/')}/content"
                    data = http_post_json(
                        content_url_hdr,
                        {"url": serp_url},
                        headers={"Authorization": f"Bearer {BROWSERLESS_TOKEN}"},
                        timeout=45,
                    )
                    break
                except Exception as inner_exc:
                    last_exc = inner_exc
                    continue
            except Exception as exc:
                last_exc = exc
                continue
        if data is None:
            raise last_exc or RuntimeError("Browserless request failed")
        for key in ("properties", "vacation_rentals", "organic_results"):
            if isinstance(data.get(key), list):
                rows.extend(data.get(key))
                break
    return rows


def rapidapi_search(city, check_in, check_out, travelers, rooms):
    if not (RAPIDAPI_KEY and RAPIDAPI_HOST):
        return []

    headers = {
        "X-RapidAPI-Key": RAPIDAPI_KEY,
        "X-RapidAPI-Host": RAPIDAPI_HOST,
    }

    # Booking-com15 flow: city lookup -> paginated hotel search.
    if RAPIDAPI_HOST == "booking-com15.p.rapidapi.com":
        dest_url = (
            f"https://{RAPIDAPI_HOST}/api/v1/hotels/searchDestination?"
            + urlencode({"query": city})
        )
        dest_data = http_get_json(dest_url, headers=headers)
        options = dest_data.get("data") if isinstance(dest_data.get("data"), list) else []
        if not options:
            return []
        city_option = next((x for x in options if x.get("dest_type") == "city"), options[0])
        dest_id = city_option.get("dest_id")
        search_type = city_option.get("search_type")
        if not dest_id or not search_type:
            return []

        rows = []
        nights = max((datetime.strptime(check_out, "%Y-%m-%d") - datetime.strptime(check_in, "%Y-%m-%d")).days, 1)
        max_pages = max(1, RAPIDAPI_MAX_PAGES)

        for page_number in range(1, max_pages + 1):
            params = {
                "dest_id": dest_id,
                "search_type": search_type,
                "arrival_date": check_in,
                "departure_date": check_out,
                "adults": travelers,
                "room_qty": rooms,
                "page_number": page_number,
                "units": "metric",
                "temperature_unit": "c",
                "languagecode": "en-us",
                "currency_code": "USD",
            }
            hotels_url = f"https://{RAPIDAPI_HOST}/api/v1/hotels/searchHotels?" + urlencode(params)
            hotels_data = http_get_json(hotels_url, headers=headers)
            hotels = (
                hotels_data.get("data", {}).get("hotels")
                if isinstance(hotels_data.get("data"), dict)
                else []
            )
            if not hotels:
                break

            for item in hotels:
                prop = item.get("property") if isinstance(item.get("property"), dict) else {}
                price_breakdown = prop.get("priceBreakdown") if isinstance(prop.get("priceBreakdown"), dict) else {}
                gross = price_breakdown.get("grossPrice") if isinstance(price_breakdown.get("grossPrice"), dict) else {}
                total_price = extract_number(gross.get("value"))
                price_per_night = round(total_price / nights, 2) if total_price is not None else None

                label = str(item.get("accessibilityLabel") or "")
                free_cancellation = "free cancellation" in label.lower()
                bedrooms_match = re.search(r"(\\d+)\\s+bedrooms?", label, flags=re.IGNORECASE)
                beds_match = re.search(r"(\\d+)\\s+beds?", label, flags=re.IGNORECASE)
                ptype = "hotel"
                lower_label = label.lower()
                if "vacation home" in lower_label:
                    ptype = "vacation_home"
                elif "apartment" in lower_label:
                    ptype = "apartment"
                elif "private room" in lower_label:
                    ptype = "private_room"

                rows.append(
                    {
                        "id": item.get("hotel_id") or prop.get("id"),
                        "name": prop.get("name"),
                        "latitude": prop.get("latitude"),
                        "longitude": prop.get("longitude"),
                        "review_score": prop.get("reviewScore"),
                        "review_count": prop.get("reviewCount"),
                        "price_per_night": price_per_night,
                        "total_price": total_price,
                        "currency": prop.get("currency") or "USD",
                        "free_cancellation": free_cancellation,
                        "property_type": ptype,
                        "address": f"{city_option.get('label') or city}",
                        "url": "",
                        "amenities": [label],
                        "bedrooms": int(bedrooms_match.group(1)) if bedrooms_match else 0,
                        "beds": int(beds_match.group(1)) if beds_match else 0,
                    }
                )
        return rows

    # Generic RapidAPI fallback path for other providers.
    if not RAPIDAPI_SEARCH_PATH:
        return []
    params = urlencode({
        "city": city,
        "checkin": check_in,
        "checkout": check_out,
        "adults": travelers,
        "rooms": rooms,
        "currency": "USD",
    })
    sep = "&" if "?" in RAPIDAPI_SEARCH_PATH else "?"
    url = f"https://{RAPIDAPI_HOST}{RAPIDAPI_SEARCH_PATH}{sep}{params}"
    data = http_get_json(url, headers=headers)
    for key in ("results", "properties", "data", "items"):
        if isinstance(data.get(key), list):
            return data.get(key)
    return []


def filter_and_sort(rows, city_lat, city_lon):
    filtered = []
    seen = set()
    for row in rows:
        row_id = row.get("id")
        if row_id in seen:
            continue
        seen.add(row_id)

        distance = row.get("distance_miles")
        if distance is not None and distance > 25:
            continue
        if row.get("review_score") is None or row.get("review_score") < 7:
            continue
        if not row.get("family_friendly"):
            continue
        if not row.get("safe_area"):
            continue
        if row.get("price_per_night") is None:
            continue
        filtered.append(row)

    filtered.sort(key=lambda x: (0 if x.get("free_cancellation") else 1, float(x.get("price_per_night") or 1e12)))
    return filtered


def run_provider_once(provider, city, city_lat, city_lon, check_in, check_out, travelers, rooms):
    if provider == "rapidapi":
        return rapidapi_search(city, check_in, check_out, travelers, rooms)
    if provider == "amadeus":
        return amadeus_search(city_lat, city_lon, check_in, check_out, travelers, rooms)
    if provider == "scraperapi":
        return scraperapi_search(city, check_in, check_out, travelers, rooms)
    if provider == "browserless":
        return browserless_search(city, check_in, check_out, travelers, rooms)
    if provider == "serpapi":
        return serpapi_search(city, check_in, check_out, travelers, rooms)
    return []


def provider_retry_policy(provider):
    if provider == "rapidapi":
        return max(1, RAPIDAPI_RETRY_ATTEMPTS), max(0.0, RAPIDAPI_BACKOFF_BASE_SEC)
    return max(1, PROVIDER_RETRY_ATTEMPTS), max(0.0, PROVIDER_BACKOFF_BASE_SEC)


def search(payload):
    city = str(payload.get("city") or "").strip()
    check_in = str(payload.get("check_in") or "").strip()
    check_out = str(payload.get("check_out") or "").strip()
    travelers = int(payload.get("travelers") or 1)
    rooms = int(payload.get("rooms") or 1)

    if not city or not check_in or not check_out:
        raise ValueError("city, check_in, check_out are required")

    datetime.strptime(check_in, "%Y-%m-%d")
    datetime.strptime(check_out, "%Y-%m-%d")

    cache_key = json.dumps({
        "cache_key_version": CACHE_KEY_VERSION,
        "city": city.lower(),
        "check_in": check_in,
        "check_out": check_out,
        "travelers": travelers,
        "rooms": rooms,
    }, sort_keys=True)

    cached = cache_get(cache_key)
    if cached:
        cached["cached"] = True
        return cached

    city_lat, city_lon = geocode_city(city)
    if city_lat is None or city_lon is None:
        raise RuntimeError("Could not geocode city")

    providers = configured_providers()
    if not providers:
        raise RuntimeError("No configured providers. Add keys in api.txt")

    all_rows = []
    used = []
    provider_errors = []
    execution_status = []

    for provider in providers:
        provider_rows = []
        start_ts = time.time()
        provider_meta = {
            "provider": provider,
            "attempts": 0,
            "outcome": "not_run",
            "message": "",
            "duration_ms": 0,
            "result_rows": 0,
            "retries": [],
        }
        try:
            max_tries, backoff_base = provider_retry_policy(provider)
            last_exc = None
            for attempt in range(1, max_tries + 1):
                provider_meta["attempts"] = attempt
                try:
                    provider_rows = run_provider_once(
                        provider,
                        city,
                        city_lat,
                        city_lon,
                        check_in,
                        check_out,
                        travelers,
                        rooms,
                    )
                    provider_meta["outcome"] = "success"
                    provider_meta["message"] = f"{provider} success on attempt {attempt}"
                    break
                except (HTTPError, URLError, TimeoutError, ValueError) as exc:
                    last_exc = exc
                    if attempt < max_tries:
                        if isinstance(exc, HTTPError) and int(getattr(exc, "code", 0) or 0) == 429:
                            reason = "HTTP 429 Too Many Requests"
                        else:
                            reason = str(exc)
                        backoff_sec = backoff_base * (2 ** (attempt - 1))
                        provider_meta["retries"].append(
                            {
                                "attempt": attempt,
                                "reason": reason,
                                "backoff_sec": round(backoff_sec, 2),
                            }
                        )
                        if backoff_sec > 0:
                            time.sleep(backoff_sec)
                        continue
                    raise
            if last_exc and not provider_rows:
                raise last_exc
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            provider_rows = []
            err_msg = f"{provider}: {exc}"
            provider_errors.append(err_msg)
            print(f"[warn] provider failed: {err_msg}")
            provider_meta["outcome"] = "failed"
            provider_meta["message"] = str(exc)
        finally:
            provider_meta["duration_ms"] = int((time.time() - start_ts) * 1000)
            provider_meta["result_rows"] = len(provider_rows)
            execution_status.append(provider_meta)

        if provider_rows:
            used.append(provider)
            for raw in provider_rows:
                all_rows.append(normalize_property(raw, provider, city_lat, city_lon, travelers, rooms))

        filtered_now = filter_and_sort(all_rows, city_lat, city_lon)
        if len(filtered_now) >= MIN_RESULTS:
            break

    results = filter_and_sort(all_rows, city_lat, city_lon)

    payload_out = {
        "city": city,
        "center": {"lat": city_lat, "lng": city_lon},
        "providers_used": used,
        "provider_errors": provider_errors,
        "execution_status": execution_status,
        "raw_result_count": len(all_rows),
        "result_count": len(results),
        "results": results,
        "cached": False,
        "no_results_hint": (
            (
                "All providers failed. Check provider_errors/execution status and try again later."
                if (not all_rows and provider_errors)
                else "No properties matched all filters for this city/date. Try nearby dates or another city."
            )
            if len(results) == 0
            else ""
        ),
        "applied_rules": {
            "radius_miles_max": 25,
            "review_score_min": 7,
            "family_friendly": True,
            "safe_area": True,
            "sort": ["free_cancellation desc", "price_per_night asc"],
        },
    }

    cache_set(cache_key, payload_out)
    return payload_out


class Handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        write_json(self, 200, {"ok": True})

    def do_GET(self):
        if self.path.startswith("/health"):
            write_json(self, 200, {
                "status": "ok",
                "providers_configured": configured_providers(),
                "cache_ttl_sec": CACHE_TTL_SEC,
            })
            return
        write_json(self, 404, {"error": "Not found"})

    def do_POST(self):
        if self.path.startswith("/search"):
            try:
                payload = parse_json_body(self)
                result = search(payload)
                write_json(self, 200, result)
            except Exception as exc:
                write_json(self, 400, {"error": str(exc)})
            return
        write_json(self, 404, {"error": "Not found"})


def main():
    init_cache()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[stay-scanner] listening on http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
