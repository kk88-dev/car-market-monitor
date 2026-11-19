#!/usr/bin/env python3
"""
Мониторинг объявлений AUTO.RIA.

Каждый запуск:
  1. раз в сутки ищет все объявления по фильтру из config.json;
  2. для новых объявлений получает карточку (цена, пробег, VIN ...);
  3. для пропавших из выдачи проверяет карточку: продано / снято / вышло из фильтра;
  4. по кругу обновляет цены активных объявлений, пока позволяет лимит API;
  5. пишет JSON для дашборда в docs/data/.

Лимит бесплатного ключа: 1000 запросов в месяц и 30 в час. Скрипт сам
распределяет месячный бюджет по дням, поэтому его можно запускать хоть каждый час.

Только стандартная библиотека Python 3.9+.
"""
import argparse
import calendar
import datetime as dt
import hashlib
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.json")
DATA_DIR = os.path.join(ROOT, "docs", "data")
API = "https://developers.ria.com/auto"
TZ = ZoneInfo("Europe/Kyiv")

# ---------------------------------------------------------------- utilities


def now():
    return dt.datetime.now(TZ)


def today_str(clock=None):
    return (clock or now()).strftime("%Y-%m-%d")


def days_between(a, b):
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days


def load_json(name, default, d=None):
    path = os.path.join(d or DATA_DIR, name)
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(name, obj, d=None):
    d = d or DATA_DIR
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ------------------------------------------------ site URL -> API parameters

# Сайт auto.ria.com и API называют фильтры по-разному. Сюда сведено то,
# что известно; всё, что не распознано, выводится предупреждением.
SITE_TO_API = [
    (r"categories\.main\.id", "category_id"),
    (r"brand\.id\[(\d+)\]", "marka_id[{0}]"),
    (r"model\.id\[(\d+)\]", "model_id[{0}]"),
    (r"year\[(\d+)\]\.gte", "s_yers[{0}]"),
    (r"year\[(\d+)\]\.lte", "po_yers[{0}]"),
    (r"fuel\.id\[(\d+)\]", "type[{0}]"),
    (r"gearbox\.id\[(\d+)\]", "gearbox[{0}]"),
    (r"drive\.id\[(\d+)\]", "drive_id[{0}]"),
    (r"body\.id\[(\d+)\]", "bodystyle[{0}]"),
    (r"region\.id\[(\d+)\]", "state[{0}]"),
    (r"city\.id\[(\d+)\]", "city[{0}]"),
    (r"engine\.gte", "engineVolumeFrom"),
    (r"engine\.lte", "engineVolumeTo"),
    (r"mileage\.gte", "raceFrom"),
    (r"mileage\.lte", "raceTo"),
    (r"power\.hp\.gte", "powerFrom"),
    (r"power\.hp\.lte", "powerTo"),
    (r"price\.USD\.gte", "price_ot"),
    (r"price\.USD\.lte", "price_do"),
]
# Параметры сайта, которые относятся только к отображению выдачи.
SITE_IGNORE = {"indexName", "page", "size", "sort[0].order", "price.currency", "lang"}
# Имена, которые уже являются параметрами API (старый формат ссылок).
API_NAMES = re.compile(
    r"^(category_id|marka_id|model_id|s_yers|po_yers|type|gearbox|drive_id|bodystyle|"
    r"state|city|engineVolumeFrom|engineVolumeTo|raceFrom|raceTo|powerFrom|powerTo|"
    r"power_name|price_ot|price_do|currency|custom|abroad|damage|auto_options|"
    r"brandOrigin|with_photo|top)(\[\d+\])?$"
)


# Новый формат ссылок сайта (2026): all[i].any[j].<поле>[k]=...
# Списки значений: поле сайта -> параметр API.
NEW_LIST = {"fuel": "type", "gearbox": "gearbox", "drive": "drive_id", "body": "bodystyle",
            "region": "state", "state": "state", "city": "city"}
# Диапазоны: поле[0] = от, поле[1] = до.
NEW_RANGE = {"engine": ("engineVolumeFrom", "engineVolumeTo"),
             "race": ("raceFrom", "raceTo"), "mileage": ("raceFrom", "raceTo"),
             "power": ("powerFrom", "powerTo")}
NEW_TOP = {
    "category": lambda v: {"category_id": v},
    "abroad": lambda v: {"abroad": "2"} if v == "0" else {},          # «Авто в Україні»
    "customs_cleared": lambda v: {"custom": "1"} if v == "1" else {},  # «Розмитнені»
    "search_type": lambda v: {},
}


def new_url_to_params(pairs):
    groups = {}   # префикс группы марки -> {"brand", "models", "years"}
    lists, params, unknown = {}, {}, []
    for key, value in pairs:
        if key in NEW_TOP:
            params.update(NEW_TOP[key](value))
            continue
        head, _, last = key.rpartition(".")
        m = re.fullmatch(r"([a-z_]+)(?:\[(\d+)\])?", last)
        if not m or not head.startswith("all["):
            unknown.append(f"{key}={value}")
            continue
        name, idx = m.group(1), int(m.group(2) or 0)
        if name == "brand":
            groups.setdefault(head, {"models": [], "years": {}})["brand"] = value
        elif name == "model":
            parent = re.sub(r"\.any\[\d+\]$", "", head)
            groups.setdefault(parent, {"models": [], "years": {}})["models"].append(value)
        elif name == "year":
            groups.setdefault(head, {"models": [], "years": {}})["years"][idx] = value
        elif name in NEW_LIST:
            lists.setdefault(NEW_LIST[name], []).append(value)
        elif name in NEW_RANGE:
            params[NEW_RANGE[name][min(idx, 1)]] = value
            if name == "power":
                params["power_name"] = "1"
        else:
            unknown.append(f"{key}={value}")
    n = 0
    for g in groups.values():
        for model in g["models"] or ["0"]:
            params[f"marka_id[{n}]"] = g.get("brand", "0")
            params[f"model_id[{n}]"] = model
            if 0 in g["years"]:
                params[f"s_yers[{n}]"] = g["years"][0]
            if 1 in g["years"]:
                params[f"po_yers[{n}]"] = g["years"][1]
            n += 1
    for api_name, values in lists.items():
        for i, v in enumerate(values):
            params[f"{api_name}[{i}]"] = v
    params.setdefault("category_id", "1")
    return params, unknown


def url_to_params(url):
    """Превращает ссылку с фильтрами auto.ria.com в параметры API /auto/search."""
    query = urllib.parse.urlsplit(url).query
    pairs = urllib.parse.parse_qsl(query, keep_blank_values=False)
    if any(k.startswith("all[") for k, _ in pairs):
        return new_url_to_params(pairs)
    params, unknown = {}, []
    for key, value in pairs:
        if key in SITE_IGNORE:
            continue
        for pattern, target in SITE_TO_API:
            m = re.fullmatch(pattern, key)
            if m:
                params[target.format(*m.groups())] = value
                if target.startswith("power"):
                    params["power_name"] = "1"
                if target.startswith("price"):
                    params["currency"] = "1"
                break
        else:
            if API_NAMES.match(key):
                params[key] = value
            else:
                unknown.append(f"{key}={value}")
    params.setdefault("category_id", "1")
    return params, unknown


def build_search_params(search):
    params, unknown = url_to_params(search.get("search_url", ""))
    params.update({k: str(v) for k, v in (search.get("api_params") or {}).items()})
    return params, unknown


def search_id(url):
    return "s" + hashlib.sha1(url.strip().encode()).hexdigest()[:7]


def year_range(params):
    lo = [int(v) for k, v in params.items() if k.startswith("s_yers")]
    hi = [int(v) for k, v in params.items() if k.startswith("po_yers")]
    if lo and hi:
        return f"{min(lo)}–{max(hi)}" if min(lo) != max(hi) else str(min(lo))
    return f"от {min(lo)}" if lo else (f"до {max(hi)}" if hi else "")


# ---------------------------------------------------------------- API client


class BudgetExhausted(Exception):
    pass


class RateLimited(Exception):
    pass


class Api:
    def __init__(self, key, meta, cfg, clock=None, transport=None):
        self.key = key
        self.meta = meta
        self.clock = clock or now
        self.transport = transport or self._http
        self.monthly = int(cfg.get("monthly_request_limit", 1000))
        self.hourly = int(cfg.get("hourly_request_limit", 30))
        self.reserve = float(cfg.get("budget_reserve", 0.05))
        self.run_used = 0
        self._roll()

    # -- budget -----------------------------------------------------------
    def _roll(self):
        t = self.clock()
        month, day = t.strftime("%Y-%m"), t.strftime("%Y-%m-%d")
        b = self.meta.setdefault("budget", {})
        if b.get("month") != month:
            b.update(month=month, used_month=0)
        if b.get("day") != day:
            b.update(day=day, used_day=0, day_allowance=None)
        if b.get("day_allowance") is None:
            last_day = calendar.monthrange(t.year, t.month)[1]
            days_left = last_day - t.day + 1
            usable = self.monthly * (1 - self.reserve) - b["used_month"]
            b["day_allowance"] = max(0, int(usable // days_left))
        # Скользящее окно в один час.
        cutoff = t.timestamp() - 3600
        b["recent"] = [s for s in b.get("recent", []) if s > cutoff]

    def left_today(self):
        b = self.meta["budget"]
        return max(0, min(b["day_allowance"] - b["used_day"],
                          self.hourly - 2 - len(b["recent"])))

    def _spend(self):
        b = self.meta["budget"]
        b["used_month"] += 1
        b["used_day"] += 1
        b["recent"].append(self.clock().timestamp())
        self.run_used += 1

    # -- transport --------------------------------------------------------
    def _http(self, path, params):
        q = urllib.parse.urlencode({**params, "api_key": self.key})
        base = "https://developers.ria.com" if path.startswith(("/generations", "/modifications")) else API
        req = urllib.request.Request(f"{base}{path}?{q}", headers={"User-Agent": "car-market-monitor/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(body)
            except ValueError:
                return e.code, {"error": body[:300]}

    def get(self, path, params):
        if self.left_today() <= 0:
            raise BudgetExhausted()
        self._spend()
        status, data = self.transport(path, params)
        if status == 429:
            raise RateLimited()
        if status in (401, 403):
            raise SystemExit(f"API отклонил ключ (HTTP {status}): {data}")
        time.sleep(0 if self.transport is not self._http else 0.5)
        return status, data

    # -- endpoints --------------------------------------------------------
    def search(self, params):
        ids, page, total = [], 0, None
        while True:
            status, data = self.get("/search", {**params, "countpage": "100", "page": str(page)})
            if status != 200:
                raise RuntimeError(f"поиск вернул HTTP {status}: {data}")
            sr = (data.get("result") or {}).get("search_result") or {}
            batch = [str(i) for i in sr.get("ids") or []]
            total = int(sr.get("count") or 0)
            ids.extend(batch)
            page += 1
            if not batch or len(ids) >= total or page > 50:
                break
        return list(dict.fromkeys(ids)), total

    def info(self, auto_id):
        status, data = self.get("/info", {"auto_id": auto_id})
        if status == 200 and isinstance(data, dict) and (data.get("autoData") or data.get("USD")):
            return data
        return None  # объявление удалено или недоступно


# ------------------------------------------------------------- parsing info


def _num(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _engine(fuel_name):
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*л", fuel_name or "")
    return f"{float(m.group(1).replace(',', '.')):.1f}" if m else ""


def parse_info(auto_id, j):
    ad = j.get("autoData") or {}
    race = _num(ad.get("raceInt"))
    link = j.get("linkToView") or ""
    photo = j.get("photoData") or {}
    return {
        "title": (j.get("title") or f"{j.get('markName', '')} {j.get('modelName', '')}").strip(),
        "version": ad.get("version") or "",
        "year": _num(ad.get("year")),
        "mileage": race * 1000 if race is not None else None,
        "fuel": ad.get("fuelName") or "",
        "gearbox": ad.get("gearboxName") or "",
        "drive": ad.get("driveName") or "",
        "price": _num(j.get("USD")),
        "city": j.get("locationCityName") or (j.get("stateData") or {}).get("name") or "",
        "vin": (j.get("VIN") or ad.get("VIN") or "").strip().upper(),
        "url": ("https://auto.ria.com" + link) if link.startswith("/") else (link or f"https://auto.ria.com/auto___{auto_id}.html"),
        "photo": photo.get("seoLinkM") or photo.get("seoLinkB") or "",
        "added": j.get("addDate") or "",
        "model_id": str(j.get("modelId") or ""),
        "generation_id": ad.get("generationId"),
        "modification_id": ad.get("modificationId"),
        "fuel_type": (ad.get("fuelName") or "").split(",")[0].strip(),
        "engine": _engine(ad.get("fuelName")),
        "sold_date": (j.get("soldDate") or "")[:10],
        "updated": j.get("updateDate") or "",
        "dealer": bool((j.get("dealer") or {}).get("id")),
        "tech_checked": bool(j.get("technicalChecked")),
        "is_sold": bool(ad.get("isSold")),
        "archived": bool(ad.get("fromArchive")),
        "status_id": ad.get("statusId"),
    }


# ------------------------------------------------- human-readable filters

FUEL_NAMES = {"1": "Бензин", "2": "Дизель", "3": "Газ", "4": "Газ пропан-бутан / Бензин", "5": "Гибрид",
              "6": "Электро", "7": "Другое", "8": "Газ метан / Бензин", "9": "Газ пропан-бутан"}
GEARBOX_NAMES = {"1": "Механика", "2": "Автомат", "3": "Типтроник", "4": "Робот", "5": "Вариатор"}
DRIVE_NAMES = {"1": "Полный", "2": "Передний", "3": "Задний"}


def load_names():
    n = load_json("names.json", {})
    for k in ("marks", "models", "generations", "modifications", "_loaded"):
        n.setdefault(k, {})
    return n


def resolve_listing_names(api, monitors, names):
    """Названия поколений и модификаций (1 запрос на модель / поколение, кэшируется)."""
    try:
        for m in monitors:
            for l in m.listings.values():
                g, md, mod = l.get("generation_id"), l.get("model_id"), l.get("modification_id")
                if g and str(g) not in names["generations"] and md and f"gen:{md}" not in names["_loaded"]:
                    names["_loaded"][f"gen:{md}"] = 1
                    _, data = api.get(f"/generations/by/models/{md}/generations", {})
                    for block in data if isinstance(data, list) else []:
                        for gen in block.get("generations") or []:
                            names["generations"][str(gen.get("generationId"))] = gen.get("name")
                if mod and g and str(mod) not in names["modifications"] and f"mod:{g}" not in names["_loaded"]:
                    names["_loaded"][f"mod:{g}"] = 1
                    _, data = api.get(f"/modifications/by/generation/{g}/modifications", {})
                    for x in data if isinstance(data, list) else []:
                        names["modifications"][str(x.get("value"))] = x.get("name")
    except (BudgetExhausted, RateLimited):
        pass
    save_json("names.json", names)


def resolve_names(api, params, names):
    """Подтягивает названия марок и моделей из API (1 запрос на марку, кэшируется)."""
    cat = params.get("category_id", "1")
    marks = {v for k, v in params.items() if k.startswith("marka_id") and v != "0"}
    try:
        if marks - set(names["marks"]):
            _, data = api.get(f"/categories/{cat}/marks", {})
            for m in data if isinstance(data, list) else []:
                names["marks"][str(m.get("value"))] = m.get("name")
        for mk in marks:
            if not any(k.startswith(mk + ":") for k in names["models"]):
                _, data = api.get(f"/categories/{cat}/marks/{mk}/models", {})
                for m in data if isinstance(data, list) else []:
                    names["models"][f"{mk}:{m.get('value')}"] = m.get("name")
                names["models"][f"{mk}:_loaded"] = "1"
    except (BudgetExhausted, RateLimited):
        pass
    save_json("names.json", names)


def describe(params, names=None):
    """Список фильтров человеческим языком."""
    names = names or {"marks": {}, "models": {}}
    out, cars = [], {}
    for k, v in params.items():
        m = re.fullmatch(r"marka_id\[(\d+)\]", k)
        if m:
            cars.setdefault(v, []).append(params.get(f"model_id[{m.group(1)}]", "0"))
    for mk, models in cars.items():
        brand = names["marks"].get(mk) or f"марка #{mk}"
        ms = [names["models"].get(f"{mk}:{md}") or f"модель #{md}" for md in models if md != "0"]
        out.append(brand + (" " + ", ".join(ms) if ms else " (все модели)"))
    yr = year_range(params)
    if yr:
        out.append(yr + " г.")

    def listed(prefix, table, label):
        vals = [table.get(v, f"#{v}") for k, v in sorted(params.items()) if re.fullmatch(prefix + r"\[\d+\]", k)]
        if vals:
            out.append((label + ": " if label else "") + ", ".join(vals))
    listed("type", FUEL_NAMES, "")
    listed("gearbox", GEARBOX_NAMES, "КПП")
    listed("drive_id", DRIVE_NAMES, "привод")
    rng = lambda a, b, unit, f=lambda x: x: (  # noqa: E731
        out.append(f"{unit} {'от ' + f(params[a]) if a in params else ''}{' ' if a in params and b in params else ''}{'до ' + f(params[b]) if b in params else ''}".strip())
        if a in params or b in params else None)
    rng("engineVolumeFrom", "engineVolumeTo", "объём, л:")
    rng("raceFrom", "raceTo", "пробег, тыс. км:")
    rng("powerFrom", "powerTo", "мощность, л.с.:")
    rng("price_ot", "price_do", "цена, $:")
    for k in ("state", "city", "bodystyle"):
        if any(x.startswith(k + "[") for x in params):
            out.append({"state": "область", "city": "город", "bodystyle": "кузов"}[k] + " (выбрано)")
    if params.get("abroad") == "2":
        out.append("только в Украине")
    if params.get("custom") == "1":
        out.append("растаможенные")
    return out


# -------------------------------------------------------------- the monitor


DETAIL_FIELDS = ("title", "version", "year", "mileage", "fuel", "gearbox", "drive",
                 "city", "vin", "url", "photo", "added", "model_id", "generation_id",
                 "modification_id", "fuel_type", "engine", "sold_date", "updated", "dealer", "tech_checked")


class Monitor:
    """Один поиск. Данные лежат в docs/data/s/<id>/."""

    def __init__(self, cfg, search, api, clock=None):
        self.cfg = cfg
        self.search = search
        self.api = api
        self.clock = clock or now
        self.today = today_str(self.clock())
        self.dir = os.path.join(DATA_DIR, "s", search["id"])
        self.listings = load_json("listings.json", {}, self.dir)
        self.events = load_json("events.json", [], self.dir)
        self.daily = load_json("daily.json", [], self.dir)
        self.meta = load_json("meta.json", {}, self.dir)
        self.refresh_days = int(cfg.get("price_refresh_days", 3))
        self.missing_checks = int(cfg.get("missing_checks_before_hidden", 3))
        self.params, self.unknown = build_search_params(search)

    # -- events -----------------------------------------------------------
    def event(self, kind, l, **extra):
        self.events.append({"date": self.today, "type": kind, "id": l["id"],
                            "title": l.get("title") or f"#{l['id']}", "year": l.get("year"),
                            "price": l.get("price"), **extra})

    # -- steps ------------------------------------------------------------
    def search_if_due(self):
        if self.meta.get("last_search") == self.today:
            return
        ids, total = self.api.search(self.params)
        self.meta["last_search"] = self.today
        self.meta["last_search_count"] = total
        found = set(ids)
        for i in ids:
            l = self.listings.get(i)
            if l is None:
                self.listings[i] = {"id": i, "status": "active", "pending": True,
                                    "first_seen": self.today, "market_since": self.today,
                                    "last_seen": self.today, "missing": 0, "price_history": []}
            else:
                if l["status"] != "active":
                    # Объявление вернулось в выдачу (переактивировано продавцом).
                    l.update(status="active", closed_at=None, reason=None)
                    self.event("back", l)
                l["last_seen"] = self.today
                l["missing"] = 0
        for l in self.listings.values():
            if l["status"] == "active" and l["id"] not in found:
                l["missing"] = l.get("missing", 0) + 1
        log(f"[{self.search['id']}] поиск: {len(ids)} объявлений (count={total})")

    def apply_details(self, l, d):
        for k in DETAIL_FIELDS:
            if d.get(k) not in (None, ""):
                l[k] = d[k]
        l["last_checked"] = self.today
        if d.get("price"):
            self.set_price(l, d["price"])

    def set_price(self, l, price):
        l["price_checked"] = self.today
        hist = l.setdefault("price_history", [])
        old = hist[-1][1] if hist else None
        if old != price:
            hist.append([self.today, price])
            if old is not None:
                self.event("price", l, old=old, new=price, diff=price - old)
        l["price"] = price

    def close(self, l, status, reason):
        l.update(status=status, closed_at=self.today, reason=reason, pending=False)
        l["days_on_market"] = days_between(l.get("market_since") or l["first_seen"], l.get("last_seen") or self.today)
        self.event(status, l, days=l["days_on_market"], reason=reason)

    def find_relist_origin(self, l):
        """Ищет закрытое объявление, которое продавец выставил заново."""
        window = int(self.cfg.get("relist_window_days", 45))
        best = None
        for o in self.listings.values():
            if o["id"] == l["id"] or o["status"] not in ("sold", "removed", "hidden"):
                continue
            if not o.get("closed_at") or days_between(o["closed_at"], self.today) > window:
                continue
            if l.get("vin") and o.get("vin"):
                if l["vin"] == o["vin"]:
                    return o
                continue
            same = (o.get("year") == l.get("year") and o.get("fuel") == l.get("fuel")
                    and o.get("gearbox") == l.get("gearbox") and o.get("city") == l.get("city")
                    and o.get("version") == l.get("version")
                    and o.get("mileage") is not None and l.get("mileage") is not None
                    and 0 <= l["mileage"] - o["mileage"] <= 1000
                    and o.get("price") and l.get("price")
                    and abs(l["price"] - o["price"]) <= 0.15 * o["price"])
            if same:
                best = o
        return best

    def check_new(self, l):
        j = self.api.info(l["id"])
        l["pending"] = False
        if j is None:
            l["details_missing"] = True
            l["last_checked"] = self.today
            return
        d = parse_info(l["id"], j)
        self.apply_details(l, d)
        origin = self.find_relist_origin(l)
        if origin:
            prev_status = origin["status"]
            origin.update(status="relisted", relisted_to=l["id"])
            l["relisted_from"] = origin["id"]
            l["market_since"] = origin.get("market_since") or origin["first_seen"]
            self.event("relist", l, origin=origin["id"], origin_status=prev_status)
        else:
            self.event("new", l)

    def check_missing(self, l):
        j = self.api.info(l["id"])
        if j is None:
            self.close(l, "removed", "удалено с сайта")
            return
        d = parse_info(l["id"], j)
        self.apply_details(l, d)
        if d["is_sold"]:
            self.close(l, "sold", "отмечено «продано»")
        elif d["archived"] or (d["status_id"] not in (None, 0, "0")):
            self.close(l, "removed", "снято с продажи")
        else:
            l["missing_checked"] = l.get("missing_checked", 0) + 1
            if l["missing_checked"] >= self.missing_checks:
                self.close(l, "hidden", "активно, но вышло из фильтра")

    def check_price(self, l):
        j = self.api.info(l["id"])
        if j is None:
            l["last_checked"] = self.today
            return
        d = parse_info(l["id"], j)
        self.apply_details(l, d)
        if d["is_sold"]:
            self.close(l, "sold", "отмечено «продано»")

    # -- queues -----------------------------------------------------------
    def _active(self):
        return [l for l in self.listings.values() if l["status"] == "active"]

    def required_queue(self):
        act = self._active()
        return ([(self.check_new, l) for l in act if l.get("pending")]
                + [(self.check_missing, l) for l in act
                   if l.get("missing", 0) > 0 and l.get("last_checked") != self.today])

    def price_queue(self):
        def checked(l):
            return l.get("price_checked") or l.get("last_checked")
        due = [l for l in self._active() if not l.get("pending") and not l.get("missing")
               and (not checked(l) or days_between(checked(l), self.today) >= self.refresh_days)]
        return [(self.check_price, l) for l in sorted(due, key=lambda l: checked(l) or "")]

    # -- prices from /average_price ------------------------------------------
    def stat_groups(self):
        """(марка, модель, год) для запросов статистики; пусто, если фильтр слишком широкий."""
        p, groups = self.params, []
        for k, mk in p.items():
            m = re.fullmatch(r"marka_id\[(\d+)\]", k)
            if not m:
                continue
            i = m.group(1)
            md, lo, hi = p.get(f"model_id[{i}]", "0"), p.get(f"s_yers[{i}]"), p.get(f"po_yers[{i}]")
            if md in ("0", "") or not lo or not hi:
                return []
            groups += [(mk, md, y) for y in range(int(lo), int(hi) + 1)]
        return groups if len(groups) <= int(self.cfg.get("stats_max_groups", 12)) else []

    def prices_from_stats(self):
        """Один запрос на (модель, год) отдаёт цены всех объявлений — дешевле, чем по карточке."""
        if self.meta.get("last_stats") == self.today or self.meta.get("stats_disabled"):
            return
        groups = self.stat_groups()
        if not groups:
            return
        found, stats = {}, {}
        for mk, md, y in groups:
            status, j = self.api.get("/average_price", {"marka_id": mk, "model_id": md, "yers": y})
            if status != 200 or not isinstance(j, dict):
                continue
            for aid, price in zip(j.get("classifieds") or [], j.get("prices") or []):
                found[str(aid)] = int(price)
            stats[f"{md}:{y}"] = {"model_id": md, "year": y, "total": j.get("total"),
                                  "mean": j.get("arithmeticMean"), "percentiles": j.get("percentiles")}
        known = [l for l in self._active() if not l.get("pending") and l.get("price")]
        hits = [l for l in known if l["id"] in found]
        same = sum(1 for l in hits if l["price"] == found[l["id"]])
        # Доверяем статистике, только если на знакомых машинах цены в основном совпадают.
        trusted = len(hits) < 5 or same >= 0.6 * len(hits)
        self.meta.update(last_stats=self.today, price_stats=stats, stats_trusted=trusted,
                         stats_coverage=round(len(hits) / len(known), 2) if known else None)
        if not trusted:
            log(f"[{self.search['id']}] цены из статистики не совпадают с карточками — не использую")
            return
        for l in hits:
            self.set_price(l, found[l["id"]])
        log(f"[{self.search['id']}] статистика: обновлено цен {len(hits)} из {len(known)}")

    def finish(self, names):
        self.meta["updated_at"] = self.clock().isoformat(timespec="minutes")
        self.meta["search_url"] = self.search.get("search_url", "")
        self.meta["filters"] = describe(self.params, names)
        self.meta["unknown_filters"] = self.unknown
        self.meta["search_params"] = self.params
        self.meta["title"] = self.search.get("title") or auto_title(self.params, names)
        self.meta["demo"] = False
        active = [l for l in self.listings.values() if l["status"] == "active" and l.get("price")]
        prices = [l["price"] for l in active]
        todays = [e for e in self.events if e["date"] == self.today]
        row = {
            "date": self.today,
            "active": sum(1 for l in self.listings.values() if l["status"] == "active"),
            "median_price": int(statistics.median(prices)) if prices else None,
            "new": sum(1 for e in todays if e["type"] == "new"),
            "sold": sum(1 for e in todays if e["type"] == "sold"),
            "removed": sum(1 for e in todays if e["type"] == "removed"),
        }
        self.meta["model_error_pct"] = fair_prices(self.listings)
        row["deals"] = sum(1 for l in self.listings.values()
                           if l["status"] == "active" and (l.get("deal_pct") or 0) <= -10)
        if self.meta.get("last_search"):
            self.daily = [r for r in self.daily if r["date"] != self.today] + [row]
        self.events = self.events[-5000:]
        save_json("listings.json", self.listings, self.dir)
        save_json("events.json", self.events, self.dir)
        save_json("daily.json", self.daily, self.dir)
        save_json("meta.json", self.meta, self.dir)


def _solve(a, b):
    """Решение системы линейных уравнений методом Гаусса."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[piv] = m[piv], m[c]
        if abs(m[c][c]) < 1e-12:
            return None
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                m[r] = [x - f * y for x, y in zip(m[r], m[c])]
    return [m[i][n] / m[i][i] for i in range(n)]


def fair_prices(listings):
    """
    «Рыночная цена» каждой активной машины: регрессия log(цены) по году, пробегу,
    модели и объёму двигателя на активных объявлениях этого же поиска.
    deal_pct < 0 — машина дешевле рынка. Возвращает медианную ошибку модели, %.
    """
    import math
    rows = [l for l in listings.values() if l["status"] == "active" and l.get("price")
            and l.get("year") and l.get("mileage") is not None]
    for l in listings.values():
        l.pop("fair_price", None), l.pop("deal_pct", None)
    if len(rows) < 15:
        return None
    cats = {}
    for key in ("year", "model_id", "engine"):
        vals = sorted({str(l.get(key) or "") for l in rows})
        cats[key] = vals[1:]  # первая категория — базовая
    def x(l):
        v = [1.0, l["mileage"] / 100000]
        for key, vals in cats.items():
            v += [1.0 if str(l.get(key) or "") == c else 0.0 for c in vals]
        return v
    X = [x(l) for l in rows]
    y = [math.log(l["price"]) for l in rows]
    k = len(X[0])
    lam = 0.5  # небольшая регуляризация: категорий с 1–2 машинами не переобучаемся
    A = [[sum(r[i] * r[j] for r in X) + (lam if i == j and i > 0 else 0) for j in range(k)] for i in range(k)]
    B = [sum(r[i] * t for r, t in zip(X, y)) for i in range(k)]
    w = _solve(A, B)
    if not w:
        return None
    errs = []
    for l in rows:
        fair = math.exp(sum(a * b for a, b in zip(w, x(l))))
        l["fair_price"] = int(round(fair, -2))
        l["deal_pct"] = round((l["price"] / fair - 1) * 100, 1)
        errs.append(abs(l["deal_pct"]))
    return round(statistics.median(errs), 1)


def auto_title(params, names):
    d = describe(params, names)
    yr = year_range(params)
    return (d[0] if d else "Поиск") + (f", {yr}" if yr else "")


def run_all(cfg, api, clock=None):
    """Общий лимит запросов на все поиски: сначала обязательное, потом цены по кругу."""
    searches = [s for s in cfg.get("searches", []) if not s.get("archived")]
    monitors = [Monitor(cfg, s, api, clock) for s in searches]
    names = load_names()
    try:
        for m in monitors:
            for u in m.unknown:
                log(f"[{m.search['id']}] ВНИМАНИЕ: фильтр не распознан: {u}")
        for m in monitors:
            resolve_names(api, m.params, names)
        for m in monitors:
            m.search_if_due()
        for m in monitors:
            for fn, l in m.required_queue():
                fn(l)
        for m in monitors:
            m.prices_from_stats()
        resolve_listing_names(api, monitors, names)
        queues = [m.price_queue() for m in monitors]
        while any(queues):
            for q in queues:
                if q:
                    fn, l = q.pop(0)
                    fn(l)
    except BudgetExhausted:
        log("дневной лимит запросов исчерпан, продолжу в следующий запуск")
    except RateLimited:
        log("API ответил 429 (лимит), продолжу в следующий запуск")
    finally:
        for m in monitors:
            m.finish(names)
        save_json("budget.json", api.meta)
        write_index(cfg, names)
        log(f"запросов за запуск: {api.run_used}; за месяц: {api.meta['budget']['used_month']}")


def write_index(cfg, names=None):
    """docs/data/searches.json — список поисков для дашборда."""
    names = names or load_names()
    items = []
    for s in cfg.get("searches", []):
        if s.get("archived"):
            continue
        d = os.path.join(DATA_DIR, "s", s["id"])
        meta = load_json("meta.json", {}, d)
        params, unknown = build_search_params(s)
        items.append({
            "id": s["id"],
            "title": s.get("title") or meta.get("title") or auto_title(params, names),
            "search_url": s.get("search_url", ""),
            "filters": describe(params, names),
            "unknown_filters": unknown,
            "added": s.get("added"),
            "updated_at": meta.get("updated_at"),
            "count": meta.get("last_search_count"),
            "demo": bool(meta.get("demo")),
        })
    budget = load_json("budget.json", {}).get("budget", {})
    save_json("searches.json", {"searches": items, "budget": budget,
                                "repo": os.environ.get("GITHUB_REPOSITORY") or cfg.get("repo", ""),
                                "monthly_limit": int(cfg.get("monthly_request_limit", 1000))})


# ----------------------------------------------------------------------- CLI


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    # Старый формат: один поиск в корне конфига.
    if cfg.get("search_url") and not cfg.get("searches"):
        cfg["searches"] = [{"id": search_id(cfg["search_url"]), "title": cfg.get("title", ""),
                            "search_url": cfg["search_url"], "api_params": cfg.get("api_params") or {}}]
    for k in ("search_url", "title", "api_params", "expected_count_on_site"):
        cfg.pop(k, None)
    cfg.setdefault("searches", [])
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
        f.write("\n")


def drop_demo(cfg):
    demo = [s for s in cfg["searches"] if s.get("demo")]
    if demo:
        import shutil
        for s in demo:
            shutil.rmtree(os.path.join(DATA_DIR, "s", s["id"]), ignore_errors=True)
        cfg["searches"] = [s for s in cfg["searches"] if not s.get("demo")]
        save_config(cfg)
        log("демо-данные удалены")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--show", metavar="URL", help="показать, какие фильтры извлекаются из ссылки")
    args = ap.parse_args()
    cfg = load_config()

    if args.show:
        params, unknown = url_to_params(args.show)
        print("Фильтры:", " · ".join(describe(params, load_names())))
        print("Параметры API:", json.dumps(params, ensure_ascii=False))
        for u in unknown:
            print("НЕ распознано (не применяется):", u)
        return

    key = os.environ.get("RIA_API_KEY", "").strip()
    if not key:
        # Без ключа запуск просто пропускается (без ошибки), демо-данные остаются.
        log("RIA_API_KEY не задан: пропускаю запуск. "
            "Добавьте ключ: Settings → Secrets and variables → Actions → RIA_API_KEY")
        write_index(cfg)
        return
    drop_demo(cfg)
    api = Api(key, load_json("budget.json", {}), cfg)
    run_all(cfg, api)


if __name__ == "__main__":
    main()
