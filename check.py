#!/usr/bin/env python3
"""XLBox-checker: kijkt elk kwartier welke garageboxen bij XLBox te huur staan (alle locaties)
en stuurt een pushmelding (ntfy) zodra er op een gekozen locatie een nieuwe box bij komt.

Gebruik:
  python check.py           normale check (stuurt meldingen, werkt state.json bij)
  python check.py --droog   alleen kijken en op het scherm tonen; stuurt niks, bewaart niks
  python check.py --test    stuurt een testmelding naar je telefoon

Instellingen via omgevingsvariabelen:
  NTFY_TOPIC      (verplicht) jouw geheime ntfy-topicnaam
  NTFY_SERVER     (optioneel) standaard https://ntfy.sh
  XLBOX_MELDEN    (optioneel) locaties waarvoor je een pushmelding wilt, met komma's gescheiden;
                  standaard "Arnhem, Nijmegen". De app toont altijd alle locaties.
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
# Locaties die XLBox nu heeft; komt er een bij, dan verschijnt die vanzelf (afgeleid uit de boxnaam).
BEKENDE_LOCATIES = ["Arnhem", "Gouda", "Leeuwarden", "Lelystad", "Nijmegen", "Rotterdam", "Zoetermeer"]
MELDEN = [m.strip().title() for m in (os.environ.get("XLBOX_MELDEN", "") or "Arnhem, Nijmegen").split(",")
          if m.strip()]
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER = (os.environ.get("NTFY_SERVER", "").strip() or "https://ntfy.sh").rstrip("/")
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
UA = "xlbox-checker/1.0 (persoonlijke beschikbaarheidscheck, 1x per kwartier)"
FOUT_MELDEN_NA = 4   # zoveel mislukte checks op rij (= ca. een uur) voordat je een melding krijgt
MAX_IN_MELDING = 10
MAX_PAGINAS = 10


def overzicht(locatie):
    return ("https://xlboxnederland.nl/objecten-overzicht/?plaats="
            + urllib.parse.quote(locatie) + "&verdiepingen=alle&type=te_huur")


def haal_op(pagina=1):
    data = urllib.parse.urlencode([
        ("action", "filter_garagebox"),
        ("current_page", str(pagina)),
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


def locatie_van(titel, url):
    """Boxen heten '3.19-Nijmegen' en staan op /garagebox/nijmegen-3-19/."""
    if "-" in titel:
        kand = titel.rsplit("-", 1)[1].strip()
        if kand and not kand[0].isdigit():
            return kand.title()
    m = re.search(r"/garagebox/([a-z]+)", url)
    return m.group(1).title() if m else "Onbekend"


def ontleed(pagina):
    """Geeft (aantal volgens de site, lijst met huurboxen op deze pagina)."""
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
        box["locatie"] = locatie_van(box["titel"], box["url"])
        boxen.append(box)
    return aantal, boxen


def verzamel():
    """Alle boxen van alle locaties, zo nodig over meerdere pagina's.
    Geeft (aantal volgens de site, aantal gelezen kaartjes, huurboxen)."""
    alles, gezien, aantal = [], set(), None
    for p in range(1, MAX_PAGINAS + 1):
        a, kaarten = ontleed(haal_op(p))
        aantal = a if aantal is None else aantal
        nieuw = [b for b in kaarten if b["url"] not in gezien]
        gezien.update(b["url"] for b in nieuw)
        alles.extend(nieuw)
        if len(alles) >= aantal or not nieuw:
            break
    if aantal > 0 and not alles:
        raise ValueError(f"site meldt {aantal} boxen maar de kaartjes zijn niet leesbaar")
    # Het type-filter van de site laat soms een 'Te koop'-box door; die tellen hier niet mee.
    boxen = [b for b in alles if "huur" in b["label"].lower()]
    return aantal, len(alles), boxen


def groepeer(boxen):
    """{locatie: {"aantal": n, "boxen": [...]}} voor alle bekende locaties plus eventuele nieuwe."""
    per = {naam: [] for naam in BEKENDE_LOCATIES}
    for b in boxen:
        per.setdefault(b["locatie"], []).append(b)
    uit = {}
    for naam, bs in per.items():
        bs = sorted(bs, key=lambda b: b["url"])   # vaste volgorde: de site husselt, anders elke keer een commit
        uit[naam] = {"aantal": len(bs),
                     "boxen": [{k: b[k] for k in ("url", "titel", "prijs", "kenmerken")} for b in bs]}
    return uit


def regel(box):
    delen = [box.get("titel") or "Box", box.get("prijs", "")] + list(box.get("kenmerken", []))
    return " · ".join(d for d in delen if d)


def stuur(titel, bericht, klik=None, prioriteit=4, tags=("package",), droog=False):
    klik = klik or overzicht(MELDEN[0] if MELDEN else "Arnhem")
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
    bekend = state.get("bekend") or {}
    if any(k.startswith("http") for k in bekend):   # oud formaat (alleen Arnhem, plat)
        bekend = {"Arnhem": bekend}
    state["bekend"] = bekend
    state.setdefault("melden", [])
    state.setdefault("locaties", {})
    state.setdefault("fouten", 0)
    state.setdefault("fout_gemeld", False)
    return state


def bewaar(state, droog):
    if droog:
        return
    with open(STATE, "w", encoding="utf-8", newline="\n") as f:
        json.dump(state, f, indent=1, sort_keys=True, ensure_ascii=False)
        f.write("\n")


def meld_nieuw_en_weg(naam, huidig, bekend_hier, droog):
    """Vergelijkt een locatie met wat we al gemeld hadden. Geeft 1 als een melding mislukte."""
    code = 0
    nieuw = [b for u, b in huidig.items() if u not in bekend_hier]
    weg = [b for u, b in bekend_hier.items() if u not in huidig]
    print(f"  {naam}: {len(huidig)} te huur, nieuw {len(nieuw)}, weg {len(weg)}")

    if nieuw:
        regels = [regel(b) for b in nieuw[:MAX_IN_MELDING]]
        if len(nieuw) > MAX_IN_MELDING:
            regels.append(f"... en nog {len(nieuw) - MAX_IN_MELDING}")
        titel = (f"XLBox {naam}: box te huur!" if len(nieuw) == 1
                 else f"XLBox {naam}: {len(nieuw)} boxen te huur!")
        try:
            stuur(titel, "\n".join(regels),
                  klik=nieuw[0]["url"] if len(nieuw) == 1 else overzicht(naam),
                  prioriteit=5, tags=("rotating_light", "package"), droog=droog)
            for u, b in huidig.items():
                bekend_hier.setdefault(u, b)
        except Exception as e:
            # Niet als bekend opslaan: dan probeert de volgende ronde het opnieuw.
            print(f"FOUT bij versturen melding: {e}")
            code = 1

    if weg:
        for u in [u for u in bekend_hier if u not in huidig]:
            del bekend_hier[u]
        try:
            stuur(f"XLBox {naam}: niet meer beschikbaar",
                  "\n".join(regel(b) for b in weg[:MAX_IN_MELDING]),
                  klik=overzicht(naam), prioriteit=2, tags=("x",), droog=droog)
        except Exception as e:
            print(f"FOUT bij versturen melding: {e}")
    return code, bool(nieuw)


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
              f"Dit is een testmelding. De checker let op huurboxen in {', '.join(MELDEN)}.",
              prioriteit=3, tags=("white_check_mark",), droog=droog)
        print("Testmelding verstuurd.")
        return 0

    state = laad()
    # Verandert 1x per dag -> 1 commit per dag -> GitHub zet de planning niet uit wegens inactiviteit.
    state["hartslag"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    code = 0

    try:
        aantal, gelezen, boxen = verzamel()
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

    locaties = groepeer(boxen)
    state["locaties"] = locaties
    state["totaal"] = {"site": aantal, "gelezen": gelezen, "huur": len(boxen)}
    print(f"Site meldt {aantal}, gelezen {gelezen}, waarvan te huur {len(boxen)}; melden voor {', '.join(MELDEN)}")
    for naam, info in locaties.items():
        print(f"  {naam}: {info['aantal']}")

    bekend = state["bekend"]
    eerder = set(state["melden"])
    for naam in list(bekend):
        if naam not in MELDEN:
            del bekend[naam]
    iets_nieuws = False
    for naam in MELDEN:
        huidig = {b["url"]: b for b in locaties.get(naam, {}).get("boxen", [])}
        if naam not in eerder:
            # Nieuw in de meldlijst: wat er nu staat stilletjes onthouden, alleen wat er bijkomt melden.
            bekend[naam] = dict(huidig)
            print(f"  {naam}: nieuw in de meldlijst, {len(huidig)} box(en) onthouden zonder melding")
            continue
        bekend_hier = bekend.setdefault(naam, {})
        c, nieuw = meld_nieuw_en_weg(naam, huidig, bekend_hier, droog)
        code = code or c
        iets_nieuws = iets_nieuws or nieuw
    state["melden"] = list(MELDEN)

    if was_kapot and not iets_nieuws:
        try:
            stuur("XLBox-checker: werkt weer", "De site is weer leesbaar.",
                  prioriteit=2, tags=("white_check_mark",), droog=droog)
        except Exception as e:
            print(f"FOUT bij versturen melding: {e}")

    bewaar(state, droog)
    return code


if __name__ == "__main__":
    sys.exit(main())
