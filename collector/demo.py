#!/usr/bin/env python3
"""
Заполняет docs/data/ вымышленными данными за 90 дней, чтобы посмотреть дашборд
до получения API-ключа. Дашборд покажет плашку «демо-данные».
Первый настоящий запуск collect.py удалит демо-данные автоматически.
"""
import datetime as dt
import random

import collect

CITIES = ["Київ", "Київ", "Київ", "Львів", "Одеса", "Дніпро", "Харків", "Вінниця", "Житомир"]
VERSIONS = ["Comfort", "Highline", "Comfortline", "Trendline", ""]


class DemoMarket:
    def __init__(self):
        self.rnd = random.Random(7)
        self.ads = {}
        self.n = 34_100_000
        for _ in range(70):
            self.add()

    def add(self):
        self.n += self.rnd.randint(1, 4000)
        year = self.rnd.choice([2016, 2017, 2017, 2018, 2018, 2019, 2019, 2020])
        mileage = max(20, int(self.rnd.gauss(14 * (2026 - year), 30)))
        engine = self.rnd.choice(["1.6", "2.0", "2.0"])
        price = 9000 + (year - 2016) * 1900 - mileage * 9 + (1500 if engine == "2.0" else 0) + self.rnd.randint(-1500, 2500)
        self.ads[str(self.n)] = {"year": year, "mileage": mileage, "price": round(price, -2), "state": "active",
                                 "city": self.rnd.choice(CITIES), "version": self.rnd.choice(VERSIONS),
                                 "gearbox": self.rnd.choice(["Автомат", "Автомат", "Ручна / Механіка"]), "engine": engine,
                                 "dealer": self.rnd.random() < 0.2}

    def step(self, day):
        for a in self.ads.values():
            if a["state"] != "active":
                continue
            r = self.rnd.random()
            if r < 0.022:
                a["state"] = "sold"
            elif r < 0.032:
                a["state"] = "deleted"
            elif r < 0.07:
                a["price"] -= self.rnd.choice([200, 300, 500, 500, 1000])
            # лёгкий рыночный тренд вниз
        for _ in range(self.rnd.choice([0, 1, 1, 1, 2, 2, 3])):
            self.add()

    def transport(self, path, params):
        if path == "/search":
            ids = sorted(i for i, a in self.ads.items() if a["state"] == "active")
            p, s = int(params["page"]), int(params["countpage"])
            return 200, {"result": {"search_result": {"ids": ids[p * s:(p + 1) * s], "count": len(ids)}}}
        a = self.ads.get(params["auto_id"])
        if not a or a["state"] == "deleted":
            return 404, {}
        return 200, {"modelId": 1, "dealer": {"id": 7 if a["dealer"] else 0}, "title": "Демо Авто", "USD": a["price"], "VIN": "",
                     "linkToView": "#", "locationCityName": a["city"],
                     "autoData": {"year": a["year"], "raceInt": a["mileage"], "fuelName": f"Дизель, {a['engine']} л.",
                                  "gearboxName": a["gearbox"], "driveName": "Передній", "version": a["version"],
                                  "isSold": a["state"] == "sold", "statusId": 0, "fromArchive": False}}


def main():
    import shutil
    ddir = collect.os.path.join(collect.DATA_DIR, "s", "demo")
    shutil.rmtree(ddir, ignore_errors=True)
    m = DemoMarket()
    search = {"id": "demo", "demo": True, "title": "Демо: вымышленная модель", "search_url": "",
              "api_params": {"category_id": 1, "s_yers[0]": 2016, "po_yers[0]": 2020, "type[0]": 2}}
    dcfg = {"searches": [search], "monthly_request_limit": 10 ** 6, "hourly_request_limit": 10 ** 6,
            "price_refresh_days": 1}
    start = collect.now().replace(hour=9, minute=0) - dt.timedelta(days=90)
    real_budget = collect.load_json("budget.json", None)
    for d in range(91):
        t = start + dt.timedelta(days=d)
        if d:
            m.step(d)
        mon = collect.Monitor(dcfg, search, collect.Api("demo", {}, dcfg, clock=lambda: t, transport=m.transport), clock=lambda: t)
        mon.search_if_due()
        for fn, l in mon.required_queue() + mon.price_queue():
            fn(l)
        mon.finish(collect.load_names())
    meta = collect.load_json("meta.json", {}, ddir)
    meta["demo"] = True
    collect.save_json("meta.json", meta, ddir)
    if real_budget is not None:
        collect.save_json("budget.json", real_budget)
    cfg = collect.load_config()
    if not any(s["id"] == "demo" for s in cfg["searches"]):
        cfg["searches"].insert(0, {k: v for k, v in search.items()})
        collect.save_config(cfg)
    collect.write_index(cfg)
    print("демо-данные записаны в", ddir)


if __name__ == "__main__":
    main()
