"""Download all non-redirect pages of the UESP "Lore" namespace via the
MediaWiki API and save them as JSONL: {"title", "game", "text"}.

Install:  pip install requests mwparserfromhell
Run:      python fetch_lore.py            (from your project folder)

Content is CC BY-SA 2.5 (UESP). Keep attribution if you share anything.
"""
import json
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

import mwparserfromhell
import requests

API_URL = "https://en.uesp.net/w/api.php"
OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "raw" / "Lore.jsonl"
BATCH_SIZE = 50          # pages per request (API limit for page content)
DELAY_SECONDS = 1.0      # pause between requests; be polite to the wiki
HEADERS = {"User-Agent": "StudyBuddy-RAG-learning-project/0.1 (personal, non-commercial)"}
SKIP_TITLE_PATTERNS = ("Names/", "Crown Store")   # name lists / store pages, not lore
HEADING_MARK = "§§H"     # temporary placeholders, replaced after strip_code
BULLET_MARK = "§§B§§"


def api_get(session, params):
    params = {**params, "format": "json", "formatversion": 2}
    for attempt in range(5):
        resp = session.get(API_URL, params=params, headers=HEADERS, timeout=60)
        if resp.status_code == 200:
            return resp.json()
        time.sleep(2 ** attempt)   # back off on errors / rate limiting
    resp.raise_for_status()


def find_lore_namespace(session):
    data = api_get(session, {"action": "query", "meta": "siteinfo", "siprop": "namespaces"})
    for ns in data["query"]["namespaces"].values():
        if ns.get("name") == "Lore":
            return ns["id"]
    raise RuntimeError("Could not find a namespace named 'Lore'")


def arg(template, *names):
    """First non-empty value among the given parameter names/positions."""
    for name in names:
        if template.has(name):
            value = str(template.get(name).value).strip()
            if value:
                return value
    return ""


def render_year(t):            # {{Year|3E 433}} or {{Year|3E 433|display text}}
    return arg(t, 2) or arg(t, 1)


def render_lore_link(t):       # {{Lore Link|Vivec (god)|display=...}} / |2
    shown = arg(t, "display", 2)
    return shown or re.sub(r"\s*\(.*?\)\s*$", "", arg(t, 1))


def render_quote_box(t):       # {{Quote Box|quote|author}}
    quote, author = arg(t, 1), arg(t, 2)
    return f"\n{quote}\n- {author}\n" if author else f"\n{quote}\n"


def render_nst(t):             # {{Nst|default text|Skyrim=...}} -> default text
    return arg(t, 1)


def render_sic(t):             # {{sic|text|correction}} -> text as printed
    return arg(t, 1) or arg(t, 2)


def render_history_entry(t):   # {{History Entry|3E 133|Title|event=...}}
    year, title, event = arg(t, "year", 1), arg(t, "title", 2), arg(t, "event", 3)
    return f"\n{year} - {title}: {event}\n".replace(": \n", "\n")


def render_lore_entry(t):      # glossary-style entries
    return arg(t, "description")


def render_book_link(t):       # {{Book Link|Residential Logistics Log}}
    return arg(t, 1)


def render_familylist(t):      # {{FamilyList|familyname=Camoran|Mankar|Raven}}
    members = [str(p.value).strip() for p in t.params if not p.showkey]
    members = [m for m in members if m]
    family = arg(t, "familyname")
    if not members:
        return ""
    return f"\n{family} family: {', '.join(members)}\n" if family else f"\n{', '.join(members)}\n"


def render_lnote(t):           # {{LNote|nb1|^1|footnote text}}
    note = arg(t, 3)
    return f"\nNote: {note}\n" if note else ""


INFOBOX_SKIP = {"image", "imgdesc", "align", "style", "width", "caption"}


def render_infobox(t):         # {{Lore Place Summary|...}}, {{Lore People Summary|...}}
    lines = []
    for p in t.params:
        key = str(p.name).strip()
        value = " ".join(str(p.value).split())
        if p.showkey and value and key.lower() not in INFOBOX_SKIP:
            lines.append(f"- {key}: {value}")
    return "\n" + "\n".join(lines) + "\n" if lines else ""


# Templates whose visible text we keep. Everything else is dropped.
TEMPLATE_RULES = {
    "year": render_year,
    "lore link": render_lore_link,
    "quote box": render_quote_box,
    "nst": render_nst,
    "sic": render_sic,
    "history entry": render_history_entry,
    "lore entry": render_lore_entry,
    "book link": render_book_link,
    "familylist": render_familylist,
    "lnote": render_lnote,
    "lore place summary": render_infobox,
    "lore people summary": render_infobox,
}

template_counts = Counter()        # every template seen
dropped_samples = defaultdict(list)  # a few raw examples of dropped ones


def apply_template_rules(code):
    # Innermost first, so nested templates are resolved before their parents.
    for t in reversed(code.filter_templates()):
        name = re.sub(r"[_\s]+", " ", str(t.name).strip().lower())
        template_counts[name] += 1
        rule = TEMPLATE_RULES.get(name)
        if rule:
            try:
                code.replace(t, rule(t))
            except Exception:
                pass                      # leave it; strip_code will drop it
        elif len(dropped_samples[name]) < 2:
            dropped_samples[name].append(str(t)[:150])


def wikitext_to_markdown(title, wikitext):
    """Convert MediaWiki markup to plain text with markdown-style headings."""
    # Protect bullet markers: strip_code would otherwise delete them.
    wikitext = re.sub(r"^\*+", BULLET_MARK, wikitext, flags=re.MULTILINE)
    code = mwparserfromhell.parse(wikitext)
    apply_template_rules(code)

    # Drop citations and file/category links entirely.
    for tag in code.filter_tags(matches=lambda t: t.tag.matches("ref")):
        code.remove(tag)
    for link in code.filter_wikilinks():
        target = str(link.title).strip()
        if target.lower().startswith(("category:", "file:", "image:")):
            code.remove(link)
        elif link.text is None and ":" in target:
            # [[Lore:Arkay]] -> "Arkay" (keep the visible name only)
            code.replace(link, target.split(":", 1)[1])

    # Mark headings with a placeholder, since a literal "##" would be
    # re-read as wikitext list markup and stripped.
    for heading in code.filter_headings():
        name = heading.title.strip_code().strip()
        code.replace(heading, f"\n{HEADING_MARK}{heading.level} {name}\n")

    # strip_code removes templates and keeps the visible text of links.
    text = code.strip_code(normalize=True, collapse=True).strip()
    text = re.sub(rf"{HEADING_MARK}(\d)", lambda m: "#" * int(m.group(1)), text)
    text = text.replace(BULLET_MARK, "-")
    text = re.sub(r"__[A-Z]+__", "", text)          # magic words like __NOTOC__
    text = re.sub(r"\n{3,}", "\n\n", text)
    return f"# {title}\n\n{text}" if text else ""


def main():
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    ns_id = find_lore_namespace(session)
    print(f"Lore namespace id: {ns_id}")

    params = {
        "action": "query",
        "generator": "allpages",
        "gapnamespace": ns_id,
        "gapfilterredir": "nonredirects",
        "gaplimit": BATCH_SIZE,
        "prop": "revisions",
        "rvprop": "content",
        "rvslots": "main",
    }

    written = 0
    with open(OUT_PATH, "w", encoding="utf-8") as out:
        while True:
            data = api_get(session, params)
            for page in data.get("query", {}).get("pages", []):
                revs = page.get("revisions")
                if not revs:
                    continue
                raw = revs[0]["slots"]["main"]["content"]
                short_title = page["title"].split(":", 1)[-1]
                if any(p in short_title for p in SKIP_TITLE_PATTERNS):
                    continue
                text = wikitext_to_markdown(short_title, raw)
                if not text:
                    continue
                record = {"title": short_title, "game": "Lore", "text": text}
                out.write(json.dumps(record, ensure_ascii=False) + "\n")
                written += 1
            print(f"{written} pages written")

            if "continue" not in data:
                break
            params.update(data["continue"])   # carries gapcontinue etc.
            time.sleep(DELAY_SECONDS)

    print(f"Done: {written} pages -> {OUT_PATH}")
    print("\nMost common DROPPED templates (with raw examples):")
    shown = 0
    for name, count in template_counts.most_common():
        if name in TEMPLATE_RULES:
            continue
        print(f"{count:>7}  {name}: {dropped_samples[name]}")
        shown += 1
        if shown >= 25:
            break


if __name__ == "__main__":
    main()