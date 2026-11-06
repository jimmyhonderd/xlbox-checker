#!/usr/bin/env python3
"""XLBox-checker: stuurt een pushmelding (ntfy) zodra er bij XLBox Arnhem een box te huur komt.

Gebruik:
  python check.py           normale check (stuurt meldingen, werkt state.json bij)
  python check.py --droog   alleen kijken en op het scherm tonen; stuurt niks, bewaart niks
  python check.py --test    stuurt een testmelding naar je telefoon

Instellingen via omgevingsvariabelen:
  NTFY_TOPIC      (verplicht) jouw geheime ntfy-topicnaam
  NTFY_SERVER     (optioneel) standaard https://ntfy.sh
  XLBOX_LOCATIE   (optioneel) standaard Arnhem
"""
import datetime
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request

AJAX = "https://xlboxnederland.nl/wp-admin/admin-ajax.php"
LOCATIE = os.environ.get("XLBOX_LOCATIE", "").strip() or "Arnhem"
OVERZICHT = ("https://xlboxnederland.nl/objecten-overzicht/?plaats="
             + urllib.parse.quote(LOCATIE) + "&verdiepingen=alle&type=te_huur")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER = (os.environ.get("NTFY_SERVER", "").strip() or "https://ntfy.sh").rstrip("/")
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
UA = "xlbox-checker/1.0 (persoonlijke beschikbaarheidscheck, 1x per kwartier)"
FOUT_MELDEN_NA = 4   # zoveel mislukte checks op rij (= ca. een uur) voordat je een melding krijgt
MAX_IN_MELDING = 10


def haal_op():
    data = urllib.parse.urlencode([
        ("action", "filter_garagebox"),
        ("current_page", "1"),
        ("locatie[]", LOCATIE),
        ("type[]", "te_huur"),
    ]).encode()
    req = urllib.request.Request(AJAX, data=data, headers={
        "User-Agent": UA,
        "X-Requested-With": "XMLHttpRequest",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def tekst(s):
    s = re.sub(r"<svg.*?</svg>", " ", s, flags=re.S)
    s = re.sub(r"\s*<sup>\s*2\s*</sup>", "²", s)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def eerste(patroon, s):
    m = re.search(patroon, s, flags=re.S)
    return tekst(m.group(1)) if m else ""


def ontleed(pagina):
    """Geeft (aantal volgens de site, lijst met huurboxen op deze locatie)."""
    m = re.search(r'class="result-count">\s*<strong>\s*(\d+)\s*</strong>', pagina)
    if not m:
        raise ValueError("resultaat-teller niet gevonden; de site is waarschijnlijk veranderd")
    aantal = int(m.group(1))
    boxen = []
    for stuk in pagina.split('class="garage-archief-box"')[1:]:
        url = re.search(r'<a\s[^>]*href="([^"]+)"', stuk)
        if not url:
            continue
        box = {
            "url": html.unescape(url.group(1)),
            "titel": eerste(r"<h3[^>]*>(.*?)</h3>", stuk),
            "label": eerste(r'class="archief-box-label"[^>]*>(.*?)</span>', stuk),
            "prijs": eerste(r'class="archief-box-prijs"[^>]*>(.*?)</p>', stuk),
            "kenmerken": [tekst(li) for li in re.findall(r"<li[^>]*>(.*?)</li>", stuk, flags=re.S)],
        }
        # Vangnet: mocht de site het filter negeren, dan alleen huurboxen op onze locatie doorlaten.
        is_huur = "huur" in box["label"].lower()
        is_hier = LOCATIE.lower() in (box["titel"] + " " + box["url"]).lower()
        if is_huur and is_hier:
            boxen.append(box)
    return aantal, boxen


def regel(box):
    delen = [box.get("titel") or "Box", box.get("prijs", "")] + list(box.get("kenmerken", []))
    return " · ".join(d for d in delen if d)


def stuur(titel, bericht, klik=OVERZICHT, prioriteit=4, tags=("package",), droog=False):
    if droog:
        print(f"[droog] MELDING (prio {prioriteit}): {titel}\n{bericht}\n-> {klik}\n")
        return
    if not NTFY_TOPIC:
        raise RuntimeError("NTFY_TOPIC is niet ingesteld (GitHub: Settings > Secrets and variables > Actions)")
    body = json.dumps({
        "topic": NTFY_TOPIC,
        "title": titel,
        "message": bericht,
        "click": klik,
        "priority": prioriteit,
        "tags": list(tags),
    }).encode("utf-8")
    req = urllib.request.Request(NTFY_SERVER + "/", data=body, headers={
        "Content-Type": "application/json",
        "User-Agent": UA,
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


def laad():
    try:
        with open(STATE, encoding="utf-8") as f:
            state = json.load(f)
    except (FileNotFoundError, ValueError):
        state = {}
    state.setdefault("bekend", {})
    state.setdefault("fouten", 0)
    state.setdefault("fout_gemeld", False)
    return state


def bewaar(state, droog):
    if droog:
        return
    with open(STATE, "w", encoding="utf-8", newline="\n") as f:
        json.dump(state, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    droog = "--droog" in sys.argv
    if not droog and not NTFY_TOPIC:
        print("FOUT: NTFY_TOPIC is niet ingesteld (GitHub: Settings > Secrets and variables > Actions).")
        return 1

    if "--test" in sys.argv:
        stuur("XLBox-checker: test",
              f"Dit is een testmelding. De checker let op huurboxen in {LOCATIE}.",
              prioriteit=3, tags=("white_check_mark",), droog=droog)
        print("Testmelding verstuurd.")
        return 0

    state = laad()
    # Verandert 1x per dag -> 1 commit per dag -> GitHub zet de planning niet uit wegens inactiviteit.
    state["hartslag"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    code = 0

    try:
        aantal, boxen = ontleed(haal_op())
    except Exception as e:
        print(f"FOUT bij ophalen/lezen van de site: {e}")
        state["fouten"] += 1
        if state["fouten"] >= FOUT_MELDEN_NA and not state["fout_gemeld"]:
            try:
                stuur("XLBox-checker: site niet leesbaar",
                      f"{state['fouten']} checks op rij mislukt ({e}). Kijk zelf even op de site; "
                      "de checker moet mogelijk aangepast worden.",
                      prioriteit=3, tags=("warning",), droog=droog)
                state["fout_gemeld"] = True
            except Exception as e2:
                print(f"FOUT bij versturen melding: {e2}")
                code = 1
        bewaar(state, droog)
        return code

    was_kapot = state["fout_gemeld"]
    state["fouten"] = 0
    state["fout_gemeld"] = False

    huidig = {b["url"]: b for b in boxen}
    if aantal > 0 and not huidig:
        # Site zegt dat er iets is, maar de kaartjes zijn niet te lezen: toch melden.
        huidig = {OVERZICHT + "#onleesbaar": {
            "url": OVERZICHT, "titel": f"{aantal} resultaat/resultaten (details niet leesbaar)",
            "prijs": "", "kenmerken": []}}

    bekend = state["bekend"]
    nieuw = [b for u, b in huidig.items() if u not in bekend]
    weg = [b for u, b in bekend.items() if u not in huidig]
    print(f"{LOCATIE} te huur: site meldt {aantal}, gelezen {len(boxen)}, nieuw {len(nieuw)}, weg {len(weg)}")
    for b in huidig.values():
        print("  - " + regel(b))

    if nieuw:
        regels = [regel(b) for b in nieuw[:MAX_IN_MELDING]]
        if len(nieuw) > MAX_IN_MELDING:
            regels.append(f"... en nog {len(nieuw) - MAX_IN_MELDING}")
        titel = (f"XLBox {LOCATIE}: box te huur!" if len(nieuw) == 1
                 else f"XLBox {LOCATIE}: {len(nieuw)} boxen te huur!")
        try:
            stuur(titel, "\n".join(regels),
                  klik=nieuw[0]["url"] if len(nieuw) == 1 else OVERZICHT,
                  prioriteit=5, tags=("rotating_light", "package"), droog=droog)
            for u, b in huidig.items():
                bekend.setdefault(u, b)
        except Exception as e:
            # Niet als bekend opslaan: dan probeert de volgende ronde het opnieuw.
            print(f"FOUT bij versturen melding: {e}")
            code = 1

    if weg:
        for u in [u for u in bekend if u not in huidig]:
            del bekend[u]
        try:
            stuur(f"XLBox {LOCATIE}: niet meer beschikbaar",
                  "\n".join(regel(b) for b in weg[:MAX_IN_MELDING]),
                  prioriteit=2, tags=("x",), droog=droog)
        except Exception as e:
            print(f"FOUT bij versturen melding: {e}")

    if was_kapot and not nieuw:
        try:
            stuur("XLBox-checker: werkt weer", "De site is weer leesbaar.",
                  prioriteit=2, tags=("white_check_mark",), droog=droog)
        except Exception as e:
            print(f"FOUT bij versturen melding: {e}")

    bewaar(state, droog)
    return code


if __name__ == "__main__":
    sys.exit(main())
