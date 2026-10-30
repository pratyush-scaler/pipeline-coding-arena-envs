"""arena_world -- seeded, stdlib-only building blocks for realistic synthetic data.

A generator imports this module, reads its seed from ARENA_WORLD_SEED and writes every
file under ARENA_WORLD_OUT (`out("exports/orders.csv")`). It runs in a pinned
python:3.12 image with no network, twice, and the two runs must write identical bytes:
draw every random value from a `World(...).stream(name)`, never from `random` itself,
the clock, the environment or a set's iteration order.

What real data looks like, and what these helpers give you (Era by Eon's realism
scorecard and synthetic-artifact detector, arXiv 2609.09853, measure the same things):

  volumes      heavy-tailed: a few customers own most orders     heavy_tail_split, pareto_int, zipf_choice
  amounts      skewed, real price endings, a long right tail      lognormal_amount
  time         business days and hours, a local UTC offset,       business_times, iso
               irregular gaps, month-end spikes, a few weekend
               events -- never a counter that advances evenly
  people       many locales, diacritics, apostrophes, particles,   people, email, phone
               and GENUINE same-name pairs -- never User1/User2
  identifiers  sequential with gaps (deleted rows), mixed widths  id_codes
  dirt         casing, stray spaces, typos, implicit missing      recase, pad, typo, implicit_missing,
               values, locale number and date formats              number_text, date_text
  planting     near misses that each break exactly ONE condition, rotate_violations, scatter, near_duplicate
               members scattered through the population, near
               duplicates of one record

Contacts are fictional by construction: e-mail domains are reserved example domains
(RFC 2606) and telephone numbers sit in ranges reserved for fiction (NANP 555-0100..0199,
Ofcom 020 7946 0000..0999), so the privacy scan passes.
"""
from __future__ import annotations

import csv
import datetime as _dt
import hashlib
import io
import json
import math
import os
import random
import unicodedata
from pathlib import Path

__all__ = [
    "World", "seed", "out", "people", "person_name", "email", "phone", "company_name", "id_codes",
    "lognormal_amount", "pareto_int", "heavy_tail_split", "zipf_choice", "business_times", "iso",
    "recase", "pad", "typo", "implicit_missing", "number_text", "date_text", "maybe",
    "rotate_violations", "scatter", "near_duplicate", "write_csv", "write_jsonl", "write_json", "write_text",
]


# ---------------------------------------------------------------------------
# Seed, streams and output
# ---------------------------------------------------------------------------

def seed() -> int:
    return int(os.environ.get("ARENA_WORLD_SEED", "0"))


def out(rel: str) -> Path:
    """The path of an output file under ARENA_WORLD_OUT; parents are created."""
    rel_path = Path(rel)
    if rel_path.is_absolute() or ".." in rel_path.parts or not rel_path.parts:
        raise ValueError(f"output paths are relative and stay inside the output root: {rel!r}")
    path = Path(os.environ.get("ARENA_WORLD_OUT", "out")) / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class World:
    """One seeded world. Each subsystem draws from its own stream, so extending one
    (more customers) never changes another's output (the orders already written)."""

    def __init__(self, seed_value: int | None = None):
        self.seed = seed() if seed_value is None else int(seed_value)

    def stream(self, name: str) -> random.Random:
        digest = hashlib.sha256(f"{self.seed}:{name}".encode("utf-8")).digest()
        return random.Random(int.from_bytes(digest[:8], "big"))

    def people(self, name: str, n: int, locales=None, same_name_pairs: int = 0) -> list[dict]:
        return people(self.stream(name), n, locales=locales, same_name_pairs=same_name_pairs)


def maybe(rng: random.Random, p: float) -> bool:
    return rng.random() < p


# ---------------------------------------------------------------------------
# People, companies, contacts, identifiers
# ---------------------------------------------------------------------------

_NAMES = {
    "en_US": (["James", "Mary", "Robert", "Patricia", "Michael", "Jennifer", "David", "Linda", "William", "Elizabeth",
               "Christopher", "Jessica", "Matthew", "Ashley", "Anthony", "Kimberly", "Joshua", "Michelle", "Kevin",
               "Amanda", "Brian", "Stephanie", "Tyler", "Megan", "Jordan", "Brittany", "Dakota", "Shannon"],
              ["Smith", "Johnson", "Williams", "Brown", "Jones", "Miller", "Davis", "Wilson", "Anderson", "Taylor",
               "Thomas", "Moore", "Martin", "Jackson", "Thompson", "White", "Harris", "Clark", "Lewis", "Walker",
               "O'Brien", "O'Neill", "McDonald", "MacArthur", "Hall", "Young", "Allen", "Wright"]),
    "en_GB": (["Oliver", "Amelia", "Harry", "Isla", "George", "Ava", "Jack", "Emily", "Charlie", "Sophie",
               "Alfie", "Grace", "Freddie", "Poppy", "Archie", "Evie", "Rory", "Niamh", "Callum", "Siobhan"],
              ["Evans", "Roberts", "Hughes", "Edwards", "Green", "Wood", "Turner", "Hill", "Cooper", "Ward",
               "Bennett", "Fitzgerald", "Ellis", "Ashworth", "Pemberton", "Smith-Jones", "Ap Rhys", "Llewellyn"]),
    "en_IN": (["Aarav", "Priya", "Rohan", "Ananya", "Vikram", "Kavya", "Arjun", "Isha", "Rahul", "Sneha",
               "Siddharth", "Pooja", "Karthik", "Divya", "Aditya", "Meera", "Nikhil", "Lakshmi", "Venkatesh", "Shreya"],
              ["Sharma", "Patel", "Iyer", "Reddy", "Nair", "Gupta", "Singh", "Kumar", "Menon", "Rao",
               "Chatterjee", "Banerjee", "Deshpande", "Kulkarni", "D'Souza", "Fernandes", "Pillai", "Mehta"]),
    "de": (["Lukas", "Anna", "Maximilian", "Lena", "Jonas", "Leonie", "Felix", "Hannah", "Jürgen", "Jörg",
            "Sören", "Bärbel", "Günther", "Käthe", "Moritz", "Johanna"],
           ["Müller", "Schmidt", "Schneider", "Fischer", "Weber", "Meyer", "Wagner", "Becker", "Schäfer", "Koch",
            "Groß", "Weiß", "von Bülow", "von der Heide", "Hoffmann", "Krüger"]),
    "fr": (["Jean-Luc", "Marie-Claire", "Léa", "Hugo", "Chloé", "Théo", "Zoé", "Noël", "Hélène", "François",
            "Anaïs", "Gaëlle", "Raphaël", "Céline"],
           ["Martin", "Bernard", "Dubois", "Durand", "Lefèvre", "Moreau", "Laurent", "Girard", "Rousseau",
            "Le Gall", "de la Fontaine", "D'Amboise", "Lefebvre", "Mercier"]),
    "es": (["José", "María", "Lucía", "Sofía", "Martín", "Mateo", "Valentina", "Alejandro", "Inés", "Ramón",
            "Begoña", "Íñigo", "Nuria", "Álvaro"],
           ["García", "Rodríguez", "Martínez", "López", "Sánchez", "Pérez", "Gómez", "Fernández", "Muñoz",
            "Jiménez", "de la Cruz", "García-Márquez", "Núñez", "Ibáñez"]),
    "pt_BR": (["João", "Ana", "Gabriel", "Júlia", "Lucas", "Beatriz", "Pedro", "Larissa", "Thiago", "Camila",
               "Conceição", "Antônio"],
              ["Silva", "Santos", "Oliveira", "Souza", "Lima", "Pereira", "Costa", "Ferreira", "da Silva",
               "dos Santos", "Gonçalves", "Araújo"]),
    "pl": (["Łukasz", "Agnieszka", "Michał", "Małgorzata", "Paweł", "Katarzyna", "Wojciech", "Zofia",
            "Grzegorz", "Joanna"],
           ["Nowak", "Kowalski", "Wiśniewski", "Wójcik", "Kamiński", "Lewandowski", "Zieliński", "Szymański",
            "Woźniak", "Dąbrowski"]),
    "vi": (["Anh", "Linh", "Minh", "Hương", "Tuấn", "Thảo", "Quang", "Ngọc", "Đức", "Trang"],
           ["Nguyễn", "Trần", "Lê", "Phạm", "Hoàng", "Huỳnh", "Phan", "Vũ", "Đặng", "Bùi"]),
    "zh": (["Wei", "Fang", "Jing", "Lei", "Min", "Yan", "Jun", "Xiu Ying", "Hao", "Li Na", "Zhi Wei", "Mei"],
           ["Wang", "Li", "Zhang", "Liu", "Chen", "Yang", "Huang", "Zhao", "Wu", "Zhou", "Xu", "Sun"]),
    "ja": (["Haruto", "Yui", "Sota", "Aoi", "Ren", "Hina", "Takumi", "Sakura", "Kenji", "Yūko", "Shōta"],
           ["Satō", "Suzuki", "Takahashi", "Tanaka", "Watanabe", "Itō", "Yamamoto", "Nakamura", "Kobayashi",
            "Katō", "Ōno"]),
    "ko": (["Min-jun", "Seo-yeon", "Ji-ho", "Ha-eun", "Do-yun", "Ji-woo", "Joon-ho", "Soo-ah"],
           ["Kim", "Lee", "Park", "Choi", "Jung", "Kang", "Cho", "Yoon"]),
    "ar": (["Mohammed", "Fatima", "Ahmed", "Aisha", "Omar", "Layla", "Yusuf", "Mariam", "Khalid", "Noor"],
           ["Al-Sayed", "Haddad", "Khoury", "Mansour", "Nasser", "El-Amin", "Abdullah", "Farouk", "Saleh",
            "Bin Rashid"]),
    "nl": (["Daan", "Sanne", "Bram", "Femke", "Joost", "Lotte", "Sem", "Fleur"],
           ["de Jong", "Jansen", "de Vries", "van den Berg", "van Dijk", "Bakker", "Visser", "van der Meer"]),
}
_FAMILY_FIRST = {"zh", "ja", "ko", "vi"}
_DEFAULT_LOCALES = ("en_US", "en_US", "en_GB", "en_IN", "en_IN", "de", "fr", "es", "pt_BR", "pl", "vi", "zh",
                    "ja", "ko", "ar", "nl")


def person_name(rng: random.Random, locale: str | None = None) -> dict:
    locale = locale or rng.choice(_DEFAULT_LOCALES)
    given_pool, family_pool = _NAMES[locale]
    given, family = rng.choice(given_pool), rng.choice(family_pool)
    if locale == "en_US" and maybe(rng, 0.12):
        given = f"{given} {rng.choice('ABCDEFGHJKLMNPRSTW')}."
    display = f"{family} {given}" if locale in _FAMILY_FIRST else f"{given} {family}"
    return {"given": given, "family": family, "display": display, "locale": locale}


def people(rng: random.Random, n: int, locales=None, same_name_pairs: int = 0) -> list[dict]:
    """`n` people; `same_name_pairs` of them share a display name with a DIFFERENT person
    (real directories hold several John Smiths -- identity is never the name)."""
    pool = tuple(locales) if locales else _DEFAULT_LOCALES
    out_rows = [person_name(rng, rng.choice(pool)) for _ in range(n)]
    for _ in range(min(same_name_pairs, n // 2)):
        a, b = rng.sample(range(n), 2)
        out_rows[b] = {**out_rows[a]}
    return out_rows


_ASCII_FOLD = str.maketrans({"ß": "ss", "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ø": "o", "æ": "ae"})


def _ascii(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.translate(_ASCII_FOLD))
    return "".join(c for c in folded if c.isascii() and (c.isalnum() or c in ".-")).lower()


def email(rng: random.Random, person: dict, domain: str | None = None) -> str:
    """A fictional address on a reserved example domain, in one of the shapes companies use."""
    given = _ascii(person["given"].split()[0]) or "user"
    family = _ascii(person["family"].replace(" ", "")) or "staff"
    local = rng.choice([f"{given}.{family}", f"{given[0]}{family}", f"{given}{family[0]}", f"{family}.{given}",
                        f"{given}_{family}", f"{given}.{family}{rng.randint(1, 99)}"])
    return f"{local}@{domain or rng.choice(('example.com', 'example.org', 'example.net'))}"


def phone(rng: random.Random, country: str = "US") -> str:
    """A number in a range reserved for fiction, formatted the way that country writes it."""
    if country == "GB":
        return rng.choice(["020 7946 0{:03d}", "+44 20 7946 0{:03d}", "(020) 7946 0{:03d}"]).format(rng.randint(0, 999))
    area = rng.choice(["212", "312", "415", "617", "702", "206"])
    last = f"01{rng.randint(0, 99):02d}"
    return rng.choice([f"({area}) 555-{last}", f"{area}-555-{last}", f"+1 {area} 555 {last}", f"{area}.555.{last}"])


_CO_A = ["Northwind", "Bluefield", "Harbor", "Crescent", "Summit", "Ironwood", "Silverline", "Redwood", "Lakeside",
         "Granite", "Meridian", "Copperleaf", "Westbrook", "Kestrel", "Juniper", "Orchard", "Saffron", "Tidewater"]
_CO_B = ["Logistics", "Foods", "Analytics", "Supply", "Health", "Labs", "Textiles", "Freight", "Systems", "Retail",
         "Pharma", "Motors", "Energy", "Holdings", "Media", "Builders"]
_CO_SUFFIX = {"US": ["Inc.", "LLC", "Corp.", "Co."], "GB": ["Ltd", "Ltd.", "PLC", "LLP"], "IN": ["Pvt Ltd", "Pvt. Ltd.", "Ltd"],
              "DE": ["GmbH", "AG", "GmbH & Co. KG"], "FR": ["SARL", "SAS", "S.A."], "BR": ["Ltda.", "S.A."]}


def company_name(rng: random.Random, country: str | None = None) -> str:
    country = country or rng.choice(list(_CO_SUFFIX))
    return f"{rng.choice(_CO_A)} {rng.choice(_CO_B)} {rng.choice(_CO_SUFFIX[country])}"


def id_codes(rng: random.Random, prefix: str, n: int, *, start: int = 1000, width: int = 6,
             gap_share: float = 0.04) -> list[str]:
    """Sequential identifiers with the gaps deleted rows leave."""
    codes, value = [], start
    while len(codes) < n:
        value += 1 + (rng.randint(1, 9) if maybe(rng, gap_share) else 0)
        codes.append(f"{prefix}{value:0{width}d}")
    return codes


# ---------------------------------------------------------------------------
# Distributions
# ---------------------------------------------------------------------------

def lognormal_amount(rng: random.Random, median: float, sigma: float = 0.9, *, minimum: float = 0.01,
                     price_points: bool = True) -> float:
    """A skewed positive amount around `median`; `price_points` snaps most values to the endings shops use."""
    value = max(minimum, median * math.exp(sigma * rng.gauss(0.0, 1.0)))
    if price_points and value >= 1 and maybe(rng, 0.65):
        whole = math.floor(value)
        value = whole + rng.choice((0.99, 0.99, 0.95, 0.49, 0.5, 0.0))
    return round(value, 2)


def pareto_int(rng: random.Random, alpha: float = 1.3, xmin: int = 1, cap: int | None = None) -> int:
    value = int(xmin / (1.0 - rng.random()) ** (1.0 / alpha))
    return min(value, cap) if cap else value


def heavy_tail_split(rng: random.Random, total: int, parts: int, alpha: float = 1.2, min_each: int = 0) -> list[int]:
    """Split `total` over `parts` so a few parts hold most of it (largest-remainder rounding)."""
    if parts <= 0:
        return []
    base = min_each * parts
    if base > total:
        raise ValueError("min_each * parts exceeds total")
    weights = [rng.paretovariate(alpha) for _ in range(parts)]
    scale = (total - base) / sum(weights)
    raw = [w * scale for w in weights]
    counts = [min_each + int(r) for r in raw]
    order = sorted(range(parts), key=lambda i: raw[i] - int(raw[i]), reverse=True)
    for i in order[: total - sum(counts)]:
        counts[i] += 1
    return counts


def zipf_choice(rng: random.Random, items: list, s: float = 1.1):
    weights = [1.0 / (rank + 1) ** s for rank in range(len(items))]
    return rng.choices(items, weights=weights, k=1)[0]


def business_times(rng: random.Random, n: int, start: str, end: str, *, utc_offset_hours: float = 0.0,
                   open_hour: int = 8, close_hour: int = 19, weekend_share: float = 0.03,
                   holidays=(), month_end_boost: float = 1.6, peak_hours=(10.5, 15.0)) -> list[_dt.datetime]:
    """`n` sorted, timezone-aware event times inside business hours of a local UTC offset:
    weekday-heavy, month-end spikes, a few weekend events, irregular gaps."""
    tz = _dt.timezone(_dt.timedelta(hours=utc_offset_hours))
    first, last = _dt.date.fromisoformat(start), _dt.date.fromisoformat(end)
    holiday_set = {_dt.date.fromisoformat(h) if isinstance(h, str) else h for h in holidays}
    days, weights = [], []
    day = first
    while day <= last:
        weight = 1.0
        if day.weekday() >= 5:
            weight = weekend_share * 5 / 2
        if day in holiday_set:
            weight = 0.02
        nxt = day + _dt.timedelta(days=1)
        if nxt.month != day.month or (day + _dt.timedelta(days=3)).month != day.month:
            weight *= month_end_boost
        days.append(day)
        weights.append(weight)
        day = nxt
    times = []
    for chosen in rng.choices(days, weights=weights, k=n):
        peak = rng.choice(peak_hours)
        hour = rng.gauss(peak, 1.8)
        while not open_hour <= hour < close_hour:   # resample: clipping would pile events up at opening time
            hour = rng.gauss(peak, 1.8)
        seconds = min(int(hour * 3600) + rng.randint(0, 59), close_hour * 3600 - 1)
        moment = _dt.datetime.combine(chosen, _dt.time(0), tz) + _dt.timedelta(seconds=seconds)
        times.append(moment)
    times.sort()
    return times


def iso(moment: _dt.datetime, sep: str = "T", timespec: str = "seconds") -> str:
    return moment.isoformat(sep=sep, timespec=timespec)


# ---------------------------------------------------------------------------
# Dirt real exports carry
# ---------------------------------------------------------------------------

def recase(rng: random.Random, text: str) -> str:
    return rng.choice([text.upper(), text.lower(), text.title(), text])


def pad(rng: random.Random, text: str) -> str:
    return rng.choice([f" {text}", f"{text} ", f"{text}  ", text.replace(" ", "  ", 1), f"{text} "])


_ADJACENT = {"a": "qwsz", "b": "vghn", "c": "xdfv", "d": "serfcx", "e": "wsdr", "f": "drtgvc", "g": "ftyhbv",
             "h": "gyujnb", "i": "ujko", "j": "huikmn", "k": "jiolm", "l": "kop", "m": "njk", "n": "bhjm",
             "o": "iklp", "p": "ol", "q": "wa", "r": "edft", "s": "awedxz", "t": "rfgy", "u": "yhji",
             "v": "cfgb", "w": "qase", "x": "zsdc", "y": "tghu", "z": "asx"}


def typo(rng: random.Random, text: str) -> str:
    """One keyboard slip: an adjacent key or two swapped letters."""
    letters = [i for i, c in enumerate(text) if c.lower() in _ADJACENT]
    if len(letters) < 2:
        return text
    i = rng.choice(letters[:-1])
    if maybe(rng, 0.5):
        j = i + 1
        return text[:i] + text[j] + text[i] + text[j + 1:]
    swap = rng.choice(_ADJACENT[text[i].lower()])
    return text[:i] + (swap.upper() if text[i].isupper() else swap) + text[i + 1:]


def implicit_missing(rng: random.Random) -> str:
    return rng.choice(["", "", "N/A", "n/a", "-", "NULL", "null", "none", "?", "TBD"])


def number_text(rng: random.Random, value: float, decimals: int = 2, style: str | None = None) -> str:
    """`value` written the way one locale or one tool writes it."""
    style = style or rng.choice(["plain", "plain", "us", "eu", "space", "currency_us", "short"])
    plain = f"{value:.{decimals}f}"
    if style == "plain":
        return plain
    if style == "short":
        return f"{value:g}"
    us = f"{value:,.{decimals}f}"
    if style == "us":
        return us
    if style == "currency_us":
        return ("-$" + us[1:]) if us.startswith("-") else "$" + us
    if style == "eu":
        return us.replace(",", "\u0000").replace(".", ",").replace("\u0000", ".")
    if style == "space":
        return us.replace(",", " ").replace(".", ",")
    raise ValueError(f"unknown number style {style!r}")


def date_text(rng: random.Random, day: _dt.date, style: str | None = None) -> str:
    style = style or rng.choice(["iso", "iso", "dmy", "mdy", "dmon", "slash_iso", "compact"])
    formats = {"iso": "%Y-%m-%d", "dmy": "%d/%m/%Y", "mdy": "%m/%d/%Y", "dmon": "%d %b %Y",
               "slash_iso": "%Y/%m/%d", "compact": "%Y%m%d"}
    if style not in formats:
        raise ValueError(f"unknown date style {style!r}")
    return day.strftime(formats[style])


# ---------------------------------------------------------------------------
# Planting
# ---------------------------------------------------------------------------

def rotate_violations(conditions: list[str], n: int) -> list[str]:
    """For `n` near misses, the ONE condition each violates, rotating so every condition is
    the deciding one for some record (a solver that drops any single condition is caught)."""
    if not conditions:
        raise ValueError("no conditions to violate")
    return [conditions[i % len(conditions)] for i in range(n)]


def scatter(rng: random.Random, rows: list, extra: list) -> list:
    """`rows` with `extra` inserted at random interior positions (never first or last)."""
    merged = list(rows)
    for item in extra:
        merged.insert(rng.randint(1, max(1, len(merged) - 1)), item)
    return merged


def near_duplicate(rng: random.Random, row: dict, fields: list[str]) -> dict:
    """A copy of `row` with one field written differently (case, spacing or a typo)."""
    copy = dict(row)
    field = rng.choice(fields)
    value = str(copy.get(field, ""))
    copy[field] = rng.choice([recase, pad, typo])(rng, value) if value else value
    return copy


# ---------------------------------------------------------------------------
# Writers (byte-stable)
# ---------------------------------------------------------------------------

def write_csv(path, rows: list[dict], fields: list[str], *, delimiter: str = ",", bom: bool = False,
              crlf: bool = False) -> Path:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields, delimiter=delimiter, extrasaction="ignore",
                            lineterminator="\r\n" if crlf else "\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return write_text(path, ("﻿" if bom else "") + buffer.getvalue())


def write_jsonl(path, rows: list) -> Path:
    return write_text(path, "".join(json.dumps(r, ensure_ascii=False, sort_keys=False) + "\n" for r in rows))


def write_json(path, value, indent: int = 2) -> Path:
    return write_text(path, json.dumps(value, ensure_ascii=False, indent=indent) + "\n")


def write_text(path, text: str) -> Path:
    target = out(path) if isinstance(path, str) else Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    return target
