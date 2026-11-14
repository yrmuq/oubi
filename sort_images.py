#!/usr/bin/env python3
"""Раскладывает картинки по папкам по тегам из tags.csv (его делает tag_folder.py).

Структура:  <выход>/<Тайтл>/<sfw|nsfw>/<персонаж или group>/<персонаж>_<тайтл>_0001.jpg

Правила:
- Тайтл берётся из тега copyright (самый уверенный). Нет тега -> "unknown".
- nsfw = rating:q и rating:e, sfw = rating:g и rating:s (меняется флагом --nsfw-from e).
- Одна девушка (1girl), даже если персонажей несколько -> папка этой девушки.
- Несколько девушек из одного тайтла -> папка group. Из разных тайтлов -> тайтл "crossover", папка group.
- Девушек нет (только парни) -> самый уверенный персонаж. Персонаж не распознан -> "unknown".

Пол персонажа определяется по всей твоей коллекции: если персонаж часто один на картинке
с тегом 1girl, он девушка, если с 1boy -> парень. Свои поправки можно задать в файле
genders.csv (строки вида "hatsune_miku,f" или "gojou_satoru,m") через --genders.

По умолчанию файлы КОПИРУЮТСЯ (оригиналы не трогаются). --move перемещает, --dry-run только показывает.
"""
import argparse, csv, re, shutil, sys
from collections import Counter, defaultdict
from pathlib import Path

csv.field_size_limit(10**9)
GIRLS = {"1girl": 1, "2girls": 2, "3girls": 3, "4girls": 4, "5girls": 5, "6+girls": 6, "multiple_girls": 2}
BOYS = {"1boy", "2boys", "3boys", "4boys", "5boys", "6+boys", "multiple_boys"}

def split(s):
    return [t.strip() for t in s.split(",") if t.strip()]

def base_name(char):
    """hatsune_miku_(vocaloid) -> hatsune_miku"""
    return re.sub(r"_\([^)]*\)$", "", char) or char

def safe(name):
    name = name.replace("/", "_").replace("\\", "_")
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "", name).strip(" ._")
    return name[:80] or "unknown"

def girl_count(general):
    n = 0
    for t in general:
        n = max(n, GIRLS.get(t, 0))
    return n

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="tags.csv")
    ap.add_argument("--out", required=True, help="папка, куда раскладывать")
    ap.add_argument("--move", action="store_true", help="перемещать вместо копирования")
    ap.add_argument("--dry-run", action="store_true", help="только показать, ничего не копировать")
    ap.add_argument("--nsfw-from", choices=["q", "e"], default="q",
                    help="q: questionable и explicit = nsfw (по умолчанию); e: только explicit")
    ap.add_argument("--genders", help="CSV с ручными поправками пола: персонаж,f|m")
    ap.add_argument("--copy-txt", action="store_true", help="переносить рядом и .txt с тегами")
    args = ap.parse_args()

    rows = list(csv.DictReader(open(args.csv, encoding="utf-8")))
    for r in rows:
        r["_chars"] = split(r.get("character", ""))
        r["_copy"] = split(r.get("copyright", ""))
        r["_gen"] = set(split(r.get("general", "")))
        r["_rating"] = split(r.get("rating", ""))

    # 1. Пол персонажей по одиночным картинкам из всей коллекции
    votes = defaultdict(Counter)
    for r in rows:
        if len(r["_chars"]) == 1 and "solo" in r["_gen"]:
            g = r["_gen"]
            if "1girl" in g and not (g & BOYS):
                votes[r["_chars"][0]]["f"] += 1
            elif "1boy" in g and "1girl" not in g:
                votes[r["_chars"][0]]["m"] += 1
    gender = {c: v.most_common(1)[0][0] for c, v in votes.items()}
    if args.genders:
        for line in csv.reader(open(args.genders, encoding="utf-8")):
            if len(line) >= 2 and line[1].strip() in ("f", "m"):
                gender[line[0].strip()] = line[1].strip()

    # Тайтл персонажа: самый частый copyright на картинках, где он единственный персонаж
    char_title = defaultdict(Counter)
    for r in rows:
        if len(r["_chars"]) == 1 and r["_copy"]:
            char_title[r["_chars"][0]][r["_copy"][0]] += 1

    def title_of(char, r):
        if char in char_title:
            return char_title[char].most_common(1)[0][0]
        m = re.search(r"_\(([^)]*)\)$", char)
        if m:
            return m.group(1)
        return r["_copy"][0] if r["_copy"] else "unknown"

    nsfw_set = {"rating:q", "rating:e"} if args.nsfw_from == "q" else {"rating:e"}
    counters = {}
    stats = Counter()

    print(f"Сейчас: раскладываю {len(rows)} строк из tags.csv.", flush=True)
    for index, r in enumerate(rows, 1):
        if index == 1 or index % 25 == 0 or index == len(rows):
            print(f"Раскладка: {index} из {len(rows)}", flush=True)
        src = Path(r["file"])
        if not src.exists():
            stats["нет файла"] += 1
            continue
        rating = "nsfw" if any(t in nsfw_set for t in r["_rating"]) else "sfw"
        chars, girls = r["_chars"], girl_count(r["_gen"])

        if not chars:
            title = r["_copy"][0] if r["_copy"] else "unknown"
            folder_char, file_char = "unknown", "unknown"
        elif girls >= 2:
            girl_chars = [c for c in chars if gender.get(c) != "m"] or chars
            known = [c for c in girl_chars if c in char_title or re.search(r"_\([^)]*\)$", c)]
            titles = {title_of(c, r) for c in known}
            unclaimed = [t for t in r["_copy"] if t not in titles]
            if len(known) < len(girl_chars):
                # у части девушек тайтл неизвестен: берём им ещё не занятый copyright с картинки
                titles.add(unclaimed[0] if (unclaimed and titles) else (r["_copy"][0] if r["_copy"] else "unknown"))
            title = titles.pop() if len(titles) == 1 else "crossover"
            folder_char, file_char = "group", "group"
        else:
            if girls == 1:
                fem = [c for c in chars if gender.get(c) == "f"]
                unk = [c for c in chars if c not in gender]
                pick = (fem or unk or chars)[0]  # список уже отсортирован по уверенности
            else:
                pick = chars[0]
            title = title_of(pick, r)
            folder_char = file_char = base_name(pick)

        title_s, char_s = safe(title), safe(folder_char)
        dst_dir = Path(args.out) / title_s / rating / char_s
        key = str(dst_dir)
        if key not in counters:
            n = 0
            if dst_dir.exists():
                for p in dst_dir.iterdir():
                    m = re.search(r"_(\d+)$", p.stem)
                    if m:
                        n = max(n, int(m.group(1)))
            counters[key] = n
        counters[key] += 1
        dst = dst_dir / f"{safe(file_char)}_{title_s}_{counters[key]:04d}{src.suffix.lower()}"

        if args.dry_run:
            print(f"{src} -> {dst}")
        else:
            dst_dir.mkdir(parents=True, exist_ok=True)
            op = shutil.move if args.move else shutil.copy2
            op(str(src), str(dst))
            txt = src.with_suffix(".txt")
            if args.copy_txt and txt.exists():
                op(str(txt), str(dst.with_suffix(".txt")))
        stats[folder_char == "group" and "group" or ("unknown" if folder_char == "unknown" else "персонаж")] += 1

    print("Итого:", dict(stats))
    unknown_gender = sorted({c for r in rows for c in r["_chars"]} - set(gender))
    if unknown_gender:
        Path("unknown_genders.txt").write_text("\n".join(unknown_gender), encoding="utf-8")
        print(f"Пол не определён у {len(unknown_gender)} персонажей, список в unknown_genders.txt "
              "(их можно дописать в genders.csv)")

if __name__ == "__main__":
    main()
