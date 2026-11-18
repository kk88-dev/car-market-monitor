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


def load_json(name, default):
    path = os.path.join(DATA_DIR, name)
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(name, obj):
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, name)
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


def build_search_params(cfg):
    params, unknown = url_to_params(cfg.get("search_url", ""))
    params.update({k: str(v) for k, v in (cfg.get("api_params") or {}).items()})
    return params, unknown


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
        req = urllib.request.Request(f"{API}{path}?{q}", headers={"User-Agent": "car-market-monitor/1.0"})
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
        "is_sold": bool(ad.get("isSold")),
        "archived": bool(ad.get("fromArchive")),
        "status_id": ad.get("statusId"),
    }


# -------------------------------------------------------------- the monitor


DETAIL_FIELDS = ("title", "version", "year", "mileage", "fuel", "gearbox", "drive",
                 "city", "vin", "url", "photo", "added")


class Monitor:
    def __init__(self, cfg, api, clock=None):
        self.cfg = cfg
        self.api = api
        self.clock = clock or now
        self.today = today_str(self.clock())
        self.listings = load_json("listings.json", {})
        self.events = load_json("events.json", [])
        self.daily = load_json("daily.json", [])
        self.meta = api.meta
        self.refresh_days = int(cfg.get("price_refresh_days", 3))
        self.missing_checks = int(cfg.get("missing_checks_before_hidden", 3))

    # -- events -----------------------------------------------------------
    def event(self, kind, l, **extra):
        self.events.append({"date": self.today, "type": kind, "id": l["id"],
                            "title": l.get("title") or f"#{l['id']}", "year": l.get("year"),
                            "price": l.get("price"), **extra})

    # -- steps ------------------------------------------------------------
    def do_search(self, params):
        ids, total = self.api.search(params)
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
        log(f"поиск: {len(ids)} объявлений (count={total})")

    def apply_details(self, l, d):
        for k in DETAIL_FIELDS:
            if d.get(k) not in (None, ""):
                l[k] = d[k]
        l["last_checked"] = self.today
        price = d.get("price")
        if price:
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
            # Убираем ложное «продано» из ленты: это перевыставление.
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

    # -- orchestration ----------------------------------------------------
    def run(self):
        params, unknown = build_search_params(self.cfg)
        for u in unknown:
            log(f"ВНИМАНИЕ: фильтр не распознан и не применён: {u}")
        self.meta["search_params"] = params
        self.meta["unknown_filters"] = unknown
        try:
            if self.meta.get("last_search") != self.today:
                self.do_search(params)
            active = [l for l in self.listings.values() if l["status"] == "active"]
            queue = (
                [(self.check_new, l) for l in active if l.get("pending")]
                + [(self.check_missing, l) for l in active if l.get("missing", 0) > 0 and l.get("last_checked") != self.today]
                + [(self.check_price, l) for l in sorted(
                    (l for l in active if not l.get("pending") and not l.get("missing")
                     and (not l.get("last_checked") or days_between(l["last_checked"], self.today) >= self.refresh_days)),
                    key=lambda l: l.get("last_checked") or "")]
            )
            for fn, l in queue:
                fn(l)
        except BudgetExhausted:
            log("дневной лимит запросов исчерпан, продолжу в следующий запуск")
        except RateLimited:
            log("API ответил 429 (лимит), продолжу в следующий запуск")
        finally:
            self.finish()

    def finish(self):
        self.meta["updated_at"] = self.clock().isoformat(timespec="minutes")
        self.meta["title"] = self.cfg.get("title", "")
        self.meta["search_url"] = self.cfg.get("search_url", "")
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
        self.daily = [r for r in self.daily if r["date"] != self.today] + [row]
        self.events = self.events[-5000:]
        save_json("listings.json", self.listings)
        save_json("events.json", self.events)
        save_json("daily.json", self.daily)
        save_json("meta.json", self.meta)
        log(f"запросов за запуск: {self.api.run_used}; за месяц: {self.meta['budget']['used_month']}")


# ----------------------------------------------------------------------- CLI


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--show-params", action="store_true", help="показать, как ссылка переводится в параметры API")
    ap.add_argument("--reset", action="store_true", help="удалить накопленные данные (например, после демо)")
    args = ap.parse_args()
    cfg = load_config()

    if args.show_params:
        params, unknown = build_search_params(cfg)
        print(json.dumps(params, ensure_ascii=False, indent=1))
        for u in unknown:
            print("не распознано:", u)
        return

    key = os.environ.get("RIA_API_KEY", "").strip()
    if not key and not args.reset:
        # Без ключа запуск просто пропускается (без ошибки), демо-данные остаются.
        log("RIA_API_KEY не задан: пропускаю запуск. "
            "Добавьте ключ: Settings → Secrets and variables → Actions → RIA_API_KEY")
        return

    meta = load_json("meta.json", {})
    if args.reset or meta.get("demo"):
        for name in ("listings.json", "events.json", "daily.json", "meta.json"):
            p = os.path.join(DATA_DIR, name)
            if os.path.exists(p):
                os.remove(p)
        meta = {}
        log("данные очищены")
        if args.reset:
            return

    if not cfg.get("search_url") and not cfg.get("api_params"):
        raise SystemExit("В config.json не указана search_url")
    api = Api(key, meta, cfg)
    Monitor(cfg, api).run()


if __name__ == "__main__":
    main()
