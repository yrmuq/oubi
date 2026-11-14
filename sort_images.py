#!/usr/bin/env python3
"""Раскладывает картинки по папкам по тегам из tags.csv (его делает tag_folder.py).

Структура:  <выход>/<Тайтл>/<sfw|nsfw>/<персонаж или group>/<персонаж>_<тайтл>_0001.jpg
Серия (имя совпадает, отличается только число из 1–2 цифр):
  <выход>/<Тайтл>/<sfw|nsfw>/<персонаж или group>/<общее имя>/<цифра>.jpg
Если один персонаж опознан больше чем на половине страниц серии, его тег пишется на остальные.
Если такого большинства нет, серия кладётся в group.

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
import argparse, csv, os, re, shutil, sys
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

def under_roots(path, roots):
    if not roots:
        return True
    resolved = Path(os.path.abspath(path))
    for root in roots:
        base = Path(os.path.abspath(root))
        if resolved == base or base in resolved.parents:
            return True
    return False


PAGE_NUMBER = re.compile(r"(?<!\d)(\d{1,2})(?!\d)")


def group_series(paths):
    buckets = defaultdict(list)
    for path in paths:
        file = Path(path)
        stem = file.stem
        parent = str(file.parent)
        for index, match in enumerate(PAGE_NUMBER.finditer(stem)):
            key = stem[:match.start()] + "#" + stem[match.end():]
            buckets[(parent, key)].append((path, int(match.group(1)), index))
    found = []
    for (parent, key), items in buckets.items():
        by_file = {}
        slot = 0
        for path, number, index in items:
            by_file[path] = number
            slot = index
        if len(by_file) < 2 or len(set(by_file.values())) < 2:
            continue
        found.append({"parent": parent, "key": key, "files": by_file, "slot": slot})
    found.sort(key=lambda item: (-len(item["files"]), -item["slot"]))
    used = set()
    chosen = []
    for item in found:
        files = {path: number for path, number in item["files"].items() if path not in used}
        if len(files) < 2 or len(set(files.values())) < 2:
            continue
        used.update(files)
        chosen.append({"key": item["key"], "files": files})
    return chosen


def chosen_character(row, gender):
    chars = row["_chars"]
    girls = girl_count(row["_gen"])
    if not chars or girls >= 2:
        return None
    if girls == 1:
        fem = [c for c in chars if gender.get(c) == "f"]
        unk = [c for c in chars if c not in gender]
        return (fem or unk or chars)[0]
    return chars[0]


def majority_tag(rows, gender):
    total = len(rows)
    counts = Counter()
    forms = defaultdict(Counter)
    for row in rows:
        tag = chosen_character(row, gender)
        if not tag:
            continue
        key = base_name(tag)
        counts[key] += 1
        forms[key][tag] += 1
    if not counts:
        return None
    key, count = counts.most_common(1)[0]
    if count * 2 <= total:
        return None
    return forms[key].most_common(1)[0][0]


def stick_character(row, tag):
    if row["_chars"][:1] == [tag]:
        return False
    row["_chars"] = [tag] + [c for c in row["_chars"] if c != tag]
    row["character"] = ", ".join(row["_chars"])
    return True


def stick_txt(path, tag):
    txt = Path(path).with_suffix(".txt")
    if not txt.is_file():
        return
    tags = [part.strip() for part in txt.read_text(encoding="utf-8", errors="replace").split(",") if part.strip()]
    if tag in tags:
        return
    tags.insert(0, tag)
    txt.write_text(", ".join(tags), encoding="utf-8")


def series_folder_name(key):
    return safe(key.replace("#", "")) or "series"


def rating_of(row, nsfw_set):
    return "nsfw" if any(tag in nsfw_set for tag in row["_rating"]) else "sfw"


def series_targets(work, gender, nsfw_set, title_of):
    by_file = {row["file"]: row for row in work}
    targets = {}
    stuck = []
    for group in group_series(list(by_file)):
        members = [by_file[path] for path in group["files"]]
        tag = majority_tag(members, gender)
        if tag:
            for row in members:
                if stick_character(row, tag):
                    stuck.append((row["file"], tag))
            folder_char = base_name(tag)
            sample = next(row for row in members if tag in row["_chars"])
            title = title_of(tag, sample)
        else:
            folder_char = "group"
            titles = Counter(row["_copy"][0] for row in members if row["_copy"])
            if not titles:
                title = "unknown"
            else:
                top = titles.most_common()
                title = "crossover" if len(top) > 1 and top[0][1] == top[1][1] else top[0][0]
        rating = Counter(rating_of(row, nsfw_set) for row in members).most_common(1)[0][0]
        folder = series_folder_name(group["key"])
        used = set()
        for path, number in sorted(group["files"].items(), key=lambda item: (item[1], item[0])):
            suffix = Path(path).suffix.lower()
            name = f"{number}{suffix}"
            while name in used:
                name = f"{number}_{len(used)}{suffix}"
            used.add(name)
            targets[path] = (title, rating, folder_char, folder, name)
    return targets, stuck


def write_tags(path, fieldnames, rows):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in fieldnames})

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
    ap.add_argument("--only", nargs="*", default=[], help="раскладывать только файлы внутри этих папок")
    ap.add_argument("--sorted-list", default="", help="дописывать путь после успешного копирования")
    args = ap.parse_args()

    with open(args.csv, encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
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

    work = [r for r in rows if under_roots(r["file"], args.only)]
    series, stuck = series_targets(work, gender, nsfw_set, title_of)
    for path, tag in stuck:
        print(f"Тег серии: {path} -> {tag}", flush=True)
    if stuck and not args.dry_run and fieldnames:
        write_tags(args.csv, fieldnames, rows)
        for path, tag in stuck:
            stick_txt(path, tag)
    print(f"Сейчас: раскладываю {len(work)} строк из tags.csv.", flush=True)
    for index, r in enumerate(work, 1):
        if index == 1 or index % 25 == 0 or index == len(work):
            print(f"Раскладка: {index} из {len(work)}", flush=True)
        src = Path(r["file"])
        if not src.exists():
            stats["нет файла"] += 1
            continue
        planned = series.get(r["file"])
        if planned:
            title, rating, folder_char, folder, name = planned
            dst_dir = Path(args.out) / safe(title) / rating / safe(folder_char) / folder
            dst = dst_dir / name
        else:
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
            if args.sorted_list:
                with open(args.sorted_list, "a", encoding="utf-8") as mark:
                    mark.write(str(src.resolve()) + "\n")
        stats[folder_char == "group" and "group" or ("unknown" if folder_char == "unknown" else "персонаж")] += 1

    print("Итого:", dict(stats))
    unknown_gender = sorted({c for r in rows for c in r["_chars"]} - set(gender))
    if unknown_gender:
        Path("unknown_genders.txt").write_text("\n".join(unknown_gender), encoding="utf-8")
        print(f"Пол не определён у {len(unknown_gender)} персонажей, список в unknown_genders.txt "
              "(их можно дописать в genders.csv)")

if __name__ == "__main__":
    main()
