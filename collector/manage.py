#!/usr/bin/env python3
"""
Добавление и удаление поисков через GitHub Issues (запускается workflow manage.yml).

  Заголовок «[add] Название» (название можно не указывать), в тексте — ссылка auto.ria.com.
  Заголовок «[remove] <id поиска>».

Ответ для комментария в issue пишется в comment.md.
"""
import os
import re
import sys

import collect


def reply(text):
    with open("comment.md", "w", encoding="utf-8") as f:
        f.write(text)
    print(text)


def fmt_filters(params, names, unknown):
    lines = ["**Фильтры, которые будут применяться:**", ""]
    lines += [f"- {f}" for f in collect.describe(params, names)] or ["- (нет)"]
    if unknown:
        lines += ["", "⚠️ **Не распознано, НЕ применяется:**", ""] + [f"- `{u}`" for u in unknown]
        lines += ["", "Если эти фильтры важны — напишите об этом, их можно добавить в сборщик."]
    return "\n".join(lines)


def add(cfg, title, body):
    m = re.search(r"https?://(?:www\.)?auto\.ria\.(?:com|ua)/\S+", body or "")
    if not m:
        return reply("❌ В тексте не найдена ссылка на поиск auto.ria.com. Создайте запрос заново и вставьте ссылку.")
    url = m.group(0).rstrip(").,>")
    params, unknown = collect.url_to_params(url)
    if not any(k.startswith("marka_id") for k in params):
        return reply("❌ В ссылке не выбрана марка. Откройте поиск на auto.ria.com, выберите марку и модель, "
                     "скопируйте адрес из браузера и попробуйте ещё раз.\n\n" + fmt_filters(params, collect.load_names(), unknown))
    sid = collect.search_id(url)
    names = collect.load_names()
    key = os.environ.get("RIA_API_KEY", "").strip()
    count = None
    if key:
        api = collect.Api(key, collect.load_json("budget.json", {}), cfg)
        collect.resolve_names(api, params, names)
        try:
            _, count = api.search({**params})
        except Exception as e:  # noqa: BLE001
            print("поиск не удался:", e, file=sys.stderr)
        collect.save_json("budget.json", api.meta)
    existing = next((s for s in cfg["searches"] if s["id"] == sid), None)
    name = title or collect.auto_title(params, names)
    if existing and not existing.get("archived"):
        msg = f"ℹ️ Такой поиск уже есть: **{existing.get('title') or name}**."
    else:
        if existing:
            existing.update(archived=False, title=title or existing.get("title", ""))
        else:
            cfg["searches"].append({"id": sid, "title": title, "search_url": url, "api_params": {},
                                    "added": collect.today_str()})
        collect.save_config(cfg)
        msg = f"✅ Добавлен поиск **{name}** (id `{sid}`)."
    msg += "\n\n" + fmt_filters(params, names, unknown)
    if count is not None:
        msg += (f"\n\nAPI сейчас находит **{count}** объявлений. Сравните с числом на сайте — "
                "если сильно отличается, какой-то фильтр перенёсся неточно.")
        if count > 100:
            msg += f"\n\nОбъявлений много: лимит API общий для всех поисков, цены будут обновляться реже."
    if key:
        msg += "\n\nСбор по этому поиску начнётся в ближайшие минуты, на дашборде он появится сразу."
    else:
        msg += "\n\nНа дашборде поиск появится сразу, а сбор начнётся, когда будет добавлен API-ключ."
    reply(msg)


def remove(cfg, sid):
    s = next((s for s in cfg["searches"] if s["id"] == sid.strip()), None)
    if not s or s.get("archived"):
        return reply(f"❌ Поиск `{sid}` не найден.")
    s["archived"] = True
    collect.save_config(cfg)
    reply(f"🗑 Поиск **{s.get('title') or sid}** убран с дашборда. Собранные данные сохранены; "
          "если добавить ту же ссылку снова, история продолжится.")


def main():
    title = os.environ.get("ISSUE_TITLE", "").strip()
    body = os.environ.get("ISSUE_BODY", "")
    cfg = collect.load_config()
    m = re.match(r"\[(add|remove)\]\s*(.*)", title, re.I)
    if not m:
        return reply("Не понял запрос: заголовок должен начинаться с [add] или [remove].")
    if m.group(1).lower() == "add":
        add(cfg, m.group(2).strip(), body)
    else:
        remove(cfg, m.group(2))
    collect.write_index(collect.load_config())


if __name__ == "__main__":
    main()
