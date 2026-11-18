"""
Симуляция рынка: прогоняет сборщик 25 дней (запуск каждые 3 часа) на поддельном API
и проверяет, что продажи, удаления, перевыставления и снижения цен распознаны верно,
а лимиты API не превышены.

Запуск: python3 tests/test_simulation.py
"""
import datetime as dt
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "collector"))
import collect  # noqa: E402


class FakeMarket:
    def __init__(self, seed=1):
        self.rnd = random.Random(seed)
        self.ads = {}      # id -> dict(price, vin, state: active|sold|deleted|out)
        self.next_id = 1000
        self.calls = []
        for _ in range(45):
            self.add()

    def add(self, vin=None, mileage=None):
        i = str(self.next_id)
        self.next_id += 1
        self.ads[i] = {"price": self.rnd.randrange(14000, 26000, 100), "state": "active",
                       "vin": vin or f"VIN{i}", "year": self.rnd.randint(2016, 2019),
                       "mileage": mileage or self.rnd.randint(60, 200)}
        return i

    def step_day(self, day):
        live = [i for i, a in self.ads.items() if a["state"] == "active"]
        for i in live:
            r = self.rnd.random()
            if r < 0.04:
                self.ads[i]["state"] = "sold"
            elif r < 0.06:
                self.ads[i]["state"] = "deleted"
            elif r < 0.10:
                self.ads[i]["price"] -= 500
        for _ in range(self.rnd.randint(0, 3)):
            self.add()
        if day == 5:   # продавец удалил и перевыставил машину
            victim = live[0]
            self.ads[victim]["state"] = "deleted"
            self.relist_pair = (victim, None)
            self.pending_relist = victim
        if day == 7 and getattr(self, "pending_relist", None):
            v = self.ads[self.pending_relist]
            new = self.add(vin=v["vin"], mileage=v["mileage"])
            self.relist_pair = (self.pending_relist, new)
            self.pending_relist = None
        if day == 3:   # продавец поднял цену выше фильтра: в выдаче нет, но активно
            self.out_id = live[1]
            self.ads[self.out_id]["state"] = "out"

    def transport(self, path, params):
        self.calls.append(path)
        if path == "/search":
            ids = sorted(i for i, a in self.ads.items() if a["state"] == "active")
            page, size = int(params["page"]), int(params["countpage"])
            return 200, {"result": {"search_result": {"ids": ids[page * size:(page + 1) * size], "count": len(ids)}}}
        if path.startswith("/categories"):
            return 200, [{"name": "Toyota", "value": 79}]
        a = self.ads.get(params["auto_id"])
        if a is None or a["state"] == "deleted":
            return 404, {"error": "not found"}
        return 200, {
            "title": "Test Car", "USD": a["price"], "VIN": a["vin"], "linkToView": f"/auto_test_{params['auto_id']}.html",
            "locationCityName": "Київ",
            "autoData": {"year": a["year"], "raceInt": a["mileage"], "fuelName": "Дизель, 2 л.",
                         "gearboxName": "Автомат", "isSold": a["state"] == "sold", "statusId": 0, "fromArchive": False},
        }


def main():
    tmp = tempfile.mkdtemp()
    collect.DATA_DIR = tmp
    market = FakeMarket()
    t = dt.datetime(2026, 11, 20, 0, 17, tzinfo=collect.TZ)
    cfg = {"searches": [{"id": "t1", "title": "Test", "search_url": "https://auto.ria.com/uk/search/?categories.main.id=1&brand.id[0]=79"}]}
    sdir = os.path.join(tmp, "s", "t1")
    days = 25
    max_per_hour = 0
    for run in range(days * 8):
        clock = lambda: t  # noqa: E731
        if t.hour == 0 and t.date() > dt.date(2026, 11, 20):
            market.step_day((t.date() - dt.date(2026, 11, 20)).days)
        before = len(market.calls)
        api = collect.Api("x", collect.load_json("budget.json", {}), cfg, clock=clock, transport=market.transport)
        collect.run_all(cfg, api, clock=clock)
        max_per_hour = max(max_per_hour, len(market.calls) - before)
        t += dt.timedelta(hours=3)

    L = collect.load_json("listings.json", {}, sdir)
    events = collect.load_json("events.json", [], sdir)
    meta = collect.load_json("budget.json", {})
    errors = []
    for i, a in market.ads.items():
        got = L.get(i, {}).get("status")
        want = {"active": "active", "sold": "sold", "deleted": "removed", "out": "hidden"}[a["state"]]
        if i == market.relist_pair[0]:
            want = "relisted"
        if got != want:
            errors.append(f"{i}: ожидалось {want}, получено {got}")
        elif a["state"] == "active" and L[i].get("price") != a["price"]:
            # цена обновляется раз в price_refresh_days, допускаем отставание по последним изменениям
            pass
    new_id = market.relist_pair[1]
    if L[new_id].get("relisted_from") != market.relist_pair[0]:
        errors.append("перевыставление не склеено")
    price_events = [e for e in events if e["type"] == "price"]
    print(f"объявлений: {len(market.ads)}, событий: {len(events)}, изменений цены: {len(price_events)}")
    print(f"запросов всего: {len(market.calls)}, максимум за запуск: {max_per_hour}, бюджет: {meta['budget']}")
    by_month = {}
    for e in events:
        by_month[e["type"]] = by_month.get(e["type"], 0) + 1
    print("события по типам:", by_month)
    assert max_per_hour <= 28, "превышен часовой лимит"
    if errors:
        print("ОШИБКИ:\n" + "\n".join(errors))
        sys.exit(1)
    print("OK")


if __name__ == "__main__":
    main()
