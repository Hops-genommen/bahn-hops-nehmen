#!/usr/bin/env python3
"""
Verspätungen NRW
----------------
Holt stündlich Plan- und Echtzeitdaten der DB Timetables API für ALLE
Bahnhöfe in NRW (inklusive S-Bahn-Halte), fasst sie pro Zug und Tag zusammen
und baut daraus eine Webseite in docs/ (für GitHub Pages).

Die Bahnhofsliste kommt automatisch aus der DB-API „StaDa – Station Data“
(Filter: Nordrhein-Westfalen) und wird einmal pro Woche aktualisiert.
In stations.txt kannst du zusätzliche Halte eintragen.

Braucht nur Python 3.10+ ohne Zusatzpakete.
Zugangsdaten: Umgebungsvariablen DB_CLIENT_ID und DB_API_KEY.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TIMETABLES = "https://apis.deutschebahn.com/db-api-marketplace/apis/timetables/v1"
STADA = "https://apis.deutschebahn.com/db-api-marketplace/apis/station-data/v2/stations"
TZ = ZoneInfo("Europe/Berlin")
ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DOCS = ROOT / "docs"
STATIONS_FILE = ROOT / "stations.txt"
BAHNHOF_FILE = DATA / "bahnhoefe.json"
ZUSTAND_FILE = DATA / "zustand.json"

PAUSE = 1.03            # Sekunden zwischen Anfragen (Limit: 60 pro Minute)
MAX_NACHHOLEN = 3       # so viele verpasste Stunden werden höchstens nachgeholt
PUENKTLICH_BIS = 5      # DB-Definition: unter 6 Minuten gilt als pünktlich
ROHDATEN_TAGE = 3       # Rohdaten so viele Tage behalten
SEITEN_TAGE = 60        # Tagesseiten so viele Tage behalten
LISTE_NEU_NACH = 7      # Bahnhofsliste nach so vielen Tagen neu laden

# Verspätungsbegründungen der DB (Code -> Text). Quelle: Open-Source-Projekt
# Travel-Status-DE-IRIS, das die Codes der DB-Schnittstelle dokumentiert.
GRUENDE = {
    2: "Polizeieinsatz", 3: "Feuerwehreinsatz auf der Strecke",
    4: "Kurzfristiger Personalausfall", 5: "Ärztliche Versorgung eines Fahrgastes",
    6: "Betätigen der Notbremse", 7: "Unbefugte Personen auf der Strecke",
    8: "Notarzteinsatz auf der Strecke", 9: "Streikauswirkungen",
    10: "Tiere auf der Strecke", 11: "Unwetter", 12: "Warten auf ein verspätetes Schiff",
    13: "Pass- und Zollkontrolle", 14: "Defekt am Bahnhof",
    15: "Beeinträchtigung durch Vandalismus", 16: "Entschärfung einer Fliegerbombe",
    17: "Beschädigung einer Brücke", 18: "Umgestürzter Baum auf der Strecke",
    19: "Unfall an einem Bahnübergang", 20: "Tiere im Gleis",
    21: "Warten auf Anschlussreisende", 22: "Witterungsbedingte Beeinträchtigungen",
    23: "Betriebsstabilisierung", 24: "Verspätung im Ausland",
    25: "Bereitstellung weiterer Wagen", 26: "Abhängen von Wagen",
    27: "Technische Störung am Bus", 28: "Gegenstände auf der Strecke",
    29: "Ersatzverkehr mit Bus ist eingerichtet", 30: "Personalausfall im Stellwerk",
    31: "Bauarbeiten", 32: "Längere Haltezeit am Bahnhof",
    33: "Defekt an der Oberleitung", 34: "Defekt an einem Signal",
    35: "Streckensperrung", 36: "Technische Störung am Zug",
    37: "Kurzfristiger Fahrzeugausfall", 38: "Defekt an der Strecke",
    39: "Stau / Hohes Verkehrsaufkommen", 40: "Defektes Stellwerk",
    41: "Defekt an einem Bahnübergang", 42: "Außerplanmäßige Geschwindigkeitsbeschränkung",
    43: "Verspätung eines vorausfahrenden Zuges", 44: "Warten auf einen entgegenkommenden Zug",
    45: "Vorfahrt eines anderen Zuges", 46: "Vorfahrt eines anderen Zuges",
    47: "Verspätete Bereitstellung", 48: "Verspätung aus vorheriger Fahrt",
    49: "Kurzfristiger Personalausfall", 50: "Kurzfristige Erkrankung von Personal",
    51: "Verspätetes Personal aus vorheriger Fahrt", 52: "Streik",
    53: "Unwetterauswirkungen", 54: "Verfügbarkeit der Gleise derzeit eingeschränkt",
    55: "Technischer Defekt an einem anderen Zug", 56: "Laden der Antriebsbatterie",
    57: "Zusätzlicher Halt", 58: "Umleitung", 59: "Schnee und Eis",
    60: "Witterungsbedingt verminderte Geschwindigkeit", 61: "Defekte Tür",
    62: "Behobener Defekt am Zug", 63: "Technische Untersuchung am Zug",
    64: "Defekt an einer Weiche", 65: "Erdrutsch", 66: "Hochwasser",
    67: "Behördliche Maßnahme", 68: "Hohes Fahrgastaufkommen",
    69: "Zug verkehrt mit verminderter Geschwindigkeit",
    99: "Verzögerungen im Betriebsablauf",
}

CLIENT_ID = os.environ.get("DB_CLIENT_ID", "")
API_KEY = os.environ.get("DB_API_KEY", "")
_letzte_anfrage = 0.0


# ---------------------------------------------------------------- API
def hole(url, accept="application/xml"):
    """Fragt eine DB-API an. Gibt die Antwort als Bytes zurück oder None."""
    global _letzte_anfrage
    req = urllib.request.Request(url, headers={
        "DB-Client-Id": CLIENT_ID, "DB-Api-Key": API_KEY, "Accept": accept})
    for _ in range(3):
        warten = PAUSE - (time.monotonic() - _letzte_anfrage)
        if warten > 0:
            time.sleep(warten)
        _letzte_anfrage = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 429:
                print("Limit erreicht, warte 60 s …", file=sys.stderr)
                time.sleep(61)
                continue
            if e.code in (401, 403):
                print(f"Zugriff abgelehnt ({e.code}) bei {url.split('/apis/')[1][:40]}", file=sys.stderr)
                return None
            if e.code != 404:
                print(f"HTTP {e.code} bei {url}", file=sys.stderr)
            return None
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"Netzwerkfehler: {e}", file=sys.stderr)
            time.sleep(5)
    return None


def xml(pfad):
    inhalt = hole(TIMETABLES + pfad)
    if not inhalt or not inhalt.strip():
        return None
    try:
        return ET.fromstring(inhalt)
    except ET.ParseError:
        return None


def zeit(s):
    return datetime.strptime(s, "%y%m%d%H%M") if s else None


def lade(pfad, standard):
    return json.loads(pfad.read_text("utf-8")) if pfad.exists() else standard


def speichere(pfad, daten):
    pfad.write_text(json.dumps(daten, ensure_ascii=False, separators=(",", ":")), "utf-8")


# ---------------------------------------------------------- Bahnhöfe
def bahnhoefe_aus_stada():
    """Alle Stationen in NRW mit ihrer Haupt-EVA-Nummer."""
    for filter_wert in ("nordrhein-westfalen", None):
        url = STADA + "?limit=10000"
        if filter_wert:
            url += "&federalstate=" + filter_wert
        inhalt = hole(url, "application/json")
        if not inhalt:
            return None
        ergebnis = {}
        for st in json.loads(inhalt).get("result", []):
            if "westfalen" not in (st.get("federalState") or "").lower():
                continue
            for e in st.get("evaNumbers", []):
                if e.get("isMain"):
                    ergebnis[str(e["number"])] = st.get("name", "")
        if ergebnis:
            return ergebnis
    return None


def bahnhoefe():
    gespeichert = lade(BAHNHOF_FILE, {})
    stand = gespeichert.get("stand")
    frisch = stand and datetime.fromisoformat(stand) > datetime.now() - timedelta(days=LISTE_NEU_NACH)
    liste = gespeichert.get("liste", {})
    zusatz = gespeichert.get("zusatz", {})

    if not frisch:
        neu = bahnhoefe_aus_stada()
        if neu:
            liste = neu
            print(f"Bahnhofsliste aktualisiert: {len(liste)} Stationen in NRW")
        elif liste:
            print("StaDa nicht erreichbar, nutze gespeicherte Liste.", file=sys.stderr)
        else:
            print("StaDa nicht erreichbar oder nicht abonniert. Es werden nur die "
                  "Bahnhöfe aus stations.txt abgefragt.", file=sys.stderr)

    # Zusätzliche Halte aus stations.txt (z. B. Stationen anderer Betreiber)
    namen = [z.strip() for z in STATIONS_FILE.read_text("utf-8").splitlines()
             if z.strip() and not z.strip().startswith("#")] if STATIONS_FILE.exists() else []
    for name in namen:
        if name in zusatz:
            continue
        root = xml("/station/" + urllib.parse.quote(name))
        treffer = root.findall("station") if root is not None else []
        if treffer:
            best = next((s for s in treffer if s.get("name") == name), treffer[0])
            zusatz[name] = [best.get("eva"), best.get("name")]
            print(f"Zusatz: {name} -> {best.get('name')} ({best.get('eva')})")
        else:
            print(f"Zusatz-Bahnhof nicht gefunden: {name}", file=sys.stderr)
    zusatz = {k: v for k, v in zusatz.items() if k in namen}

    if liste and not frisch:
        stand = datetime.now().isoformat(timespec="seconds")
    speichere(BAHNHOF_FILE, {"stand": stand, "liste": liste, "zusatz": zusatz})
    alle = dict(liste)
    for eva, name in zusatz.values():
        alle.setdefault(eva, name)
    return alle


# ------------------------------------------------------- Daten holen
def verspaetung(e):
    if e.get("pt") and e.get("ct"):
        return int((zeit(e["ct"]) - zeit(e["pt"])).total_seconds() // 60)
    return None


def zugname(tl, ar, dp):
    kat = tl.get("c", "")
    linie = dp.get("l") or ar.get("l") or ""
    if linie:
        return linie if linie.upper().startswith(kat.upper()) else f"{kat} {linie}".strip()
    return f"{kat} {tl.get('n', '')}".strip()


def plan_holen(eva, stunde):
    root = xml(f"/plan/{eva}/{stunde:%y%m%d}/{stunde:%H}")
    halte = {}
    if root is None:
        return halte
    for s in root.findall("s"):
        tl = s.find("tl")
        if tl is None:
            continue
        ar, dp = s.find("ar"), s.find("dp")
        halte[s.get("id")] = {
            "tl": {k: tl.get(k) for k in ("c", "n") if tl.get(k)},
            "ar": {k: ar.get(k) for k in ("pt", "l", "ppth") if ar is not None and ar.get(k)},
            "dp": {k: dp.get(k) for k in ("pt", "l", "ppth") if dp is not None and dp.get(k)},
        }
    return halte


def aenderungen_anwenden(eva, halte):
    root = xml(f"/fchg/{eva}")
    if root is None:
        return
    for s in root.findall("s"):
        h = halte.get(s.get("id"))
        if not h:
            continue
        for art in ("ar", "dp"):
            e = s.find(art)
            if e is not None and h[art]:
                for k in ("ct", "cs"):
                    if e.get(k) is not None:
                        h[art][k] = e.get(k)
        # Begründungen: Meldungen vom Typ "d" (delay) mit bekanntem Code
        meldungen = []
        for m in s.iter("m"):
            code = m.get("c")
            if m.get("t") == "d" and code and code.isdigit() and int(code) in GRUENDE:
                meldungen.append((m.get("ts") or "", int(code)))
        if meldungen:
            codes = []
            for _, c in sorted(meldungen, reverse=True):   # neueste zuerst
                if c not in codes:
                    codes.append(c)
            h["gr"] = codes


def halt_auswerten(sid, h, bf_name):
    tl, ar, dp = h["tl"], h["ar"], h["dp"]
    plan = zeit(dp.get("pt") or ar.get("pt"))
    if plan is None:
        return None
    werte = [v for v in (verspaetung(ar), verspaetung(dp)) if v is not None]
    return {
        "zug": sid.rsplit("-", 1)[0],
        "tag": plan.strftime("%Y-%m-%d"),
        "name": zugname(tl, ar, dp),
        "nr": tl.get("n", ""),
        "von": (ar.get("ppth") or "").split("|")[0] or bf_name,
        "nach": (dp.get("ppth") or "").split("|")[-1] or bf_name,
        "halt": [plan.strftime("%H:%M"), max(werte) if werte else None,
                 1 if "c" in (ar.get("cs"), dp.get("cs")) else 0, h.get("gr", [])],
    }


# ---------------------------------------------------------- Auswerten
def zuege_des_tages(rohdaten, namen):
    """Fasst die gespeicherten Halte pro Zug zusammen."""
    bf_index = {}
    zuege = []
    for z in rohdaten.values():
        halte = sorted(z["h"].items(), key=lambda x: x[1][0])
        halte = [(eva, (h + [[]])[:4]) for eva, h in halte]   # ältere Daten ohne Gründe
        max_v, wo = None, None
        gruende = {}
        for eva, (plan, v, aus, gr) in halte:
            if v is not None and (max_v is None or v > max_v):
                max_v, wo = v, namen.get(eva, eva)
            for c in gr:
                gruende[c] = gruende.get(c, 0) + 1
        h_liste = []
        for eva, (plan, v, aus, gr) in halte:
            name = namen.get(eva, eva)
            if name not in bf_index:
                bf_index[name] = len(bf_index)
            h_liste.append([bf_index[name], v, aus])
        zuege.append({
            "name": z["name"], "nr": z["nr"], "von": z["von"], "nach": z["nach"],
            "ab": halte[0][1][0], "max": max_v, "wo": wo,
            "ausfall": any(aus for _, (_, _, aus, _) in halte), "h": h_liste,
            "gr": sorted(gruende, key=lambda c: -gruende[c]),
        })
    zuege.sort(key=lambda z: z["ab"])
    return zuege, list(bf_index)


def statistik(zuege):
    zu_spaet = [z for z in zuege if (z["max"] or 0) > PUENKTLICH_BIS and not z["ausfall"]]
    linien = {}
    for z in zu_spaet:
        linien[z["name"]] = linien.get(z["name"], 0) + 1
    gruende = {}
    for z in zu_spaet + [z for z in zuege if z["ausfall"]]:
        for c in z["gr"]:
            gruende[c] = gruende.get(c, 0) + 1
    ohne_grund = sum(1 for z in zu_spaet if not z["gr"])
    schlimmster = max(zu_spaet, key=lambda z: z["max"], default=None)
    if schlimmster:
        schlimmster = {k: schlimmster[k] for k in ("name", "nach", "max", "wo", "gr")}
    return {"gesamt": len(zuege), "spaet": len(zu_spaet),
            "ausfall": sum(1 for z in zuege if z["ausfall"]),
            "linien": sorted(linien.items(), key=lambda x: -x[1])[:8],
            "gruende": sorted(gruende.items(), key=lambda x: -x[1])[:10],
            "ohneGrund": ohne_grund,
            "schlimmster": schlimmster}


# ------------------------------------------------------------ Webseite
def seite_bauen(tag, rohdaten, namen, alle_tage, stand, anzahl_bf):
    zuege, bf_liste = zuege_des_tages(rohdaten, namen)
    i = alle_tage.index(tag)
    daten = {
        "tag": tag, "stand": stand, "heute": datetime.now(TZ).strftime("%Y-%m-%d"),
        "davor": alle_tage[i - 1] if i > 0 else "",
        "danach": alle_tage[i + 1] if i < len(alle_tage) - 1 else "",
        "grenze": PUENKTLICH_BIS, "anzahlBf": anzahl_bf,
        "stat": statistik(zuege), "bf": bf_liste, "zuege": zuege,
        "gruende": {c: GRUENDE[c] for c in {c for z in zuege for c in z["gr"]}},
    }
    json_text = json.dumps(daten, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = (ROOT / "vorlage.html").read_text("utf-8").replace("__DATEN__", json_text)
    (DOCS / f"{tag}.html").write_text(html, "utf-8")
    if tag == alle_tage[-1]:
        (DOCS / "index.html").write_text(html, "utf-8")


def aufraeumen():
    grenze_roh = (datetime.now(TZ) - timedelta(days=ROHDATEN_TAGE)).strftime("%Y-%m-%d")
    grenze_seite = (datetime.now(TZ) - timedelta(days=SEITEN_TAGE)).strftime("%Y-%m-%d")
    for p in DATA.glob("????-??-??.json"):
        if p.stem < grenze_roh:
            p.unlink()
    for p in DOCS.glob("????-??-??.html"):
        if p.stem < grenze_seite:
            p.unlink()


# ------------------------------------------------------------- Ablauf
def main():
    if not CLIENT_ID or not API_KEY:
        sys.exit("DB_CLIENT_ID und DB_API_KEY fehlen (als GitHub-Secrets anlegen).")
    DATA.mkdir(exist_ok=True)
    DOCS.mkdir(exist_ok=True)
    (DOCS / ".nojekyll").touch()

    namen = bahnhoefe()
    if not namen:
        sys.exit("Keine Bahnhöfe gefunden. Prüfe das StaDa-Abo oder stations.txt.")

    # Welche Stunden sind dran? Normalerweise nur die letzte volle Stunde,
    # nach ausgefallenen Läufen werden bis zu MAX_NACHHOLEN Stunden nachgeholt.
    jetzt = datetime.now(TZ).replace(minute=0, second=0, microsecond=0, tzinfo=None)
    letzte_volle = jetzt - timedelta(hours=1)
    zustand = lade(ZUSTAND_FILE, {})
    if zustand.get("letzte"):
        erledigt = datetime.strptime(zustand["letzte"], "%Y-%m-%dT%H")
        stunden = []
        t = max(erledigt + timedelta(hours=1), letzte_volle - timedelta(hours=MAX_NACHHOLEN - 1))
        while t <= letzte_volle:
            stunden.append(t)
            t += timedelta(hours=1)
    else:
        stunden = [letzte_volle]
    print(f"{len(namen)} Bahnhöfe, neue Stunden:",
          ", ".join(f"{t:%d.%m. %H} Uhr" for t in stunden) or "keine")

    alte_halte = zustand.get("halte", {})
    neue_halte = {}
    tage = {}
    for n, (eva, bf_name) in enumerate(sorted(namen.items(), key=lambda x: x[1]), 1):
        halte = dict(alte_halte.get(eva, {}))   # Vorstunde: Verspätungen nachziehen
        frisch = {}
        for t in stunden:
            frisch.update(plan_holen(eva, t))
        halte.update(frisch)
        if not halte:
            continue
        aenderungen_anwenden(eva, halte)
        neue_halte[eva] = frisch
        for sid, h in halte.items():
            e = halt_auswerten(sid, h, bf_name)
            if not e:
                continue
            tag = tage.setdefault(e["tag"], {})
            z = tag.setdefault(e["zug"], {"name": e["name"], "nr": e["nr"],
                                           "von": e["von"], "nach": e["nach"], "h": {}})
            z["h"][eva] = e["halt"]
        if n % 50 == 0:
            print(f"… {n}/{len(namen)} Bahnhöfe")

    # Rohdaten pro Tag zusammenführen
    for tag, zuege in tage.items():
        datei = DATA / f"{tag}.json"
        bestand = lade(datei, {})
        for k, z in zuege.items():
            if k in bestand:
                bestand[k]["h"].update(z["h"])
            else:
                bestand[k] = z
        speichere(datei, bestand)

    if stunden:
        zustand = {"letzte": stunden[-1].strftime("%Y-%m-%dT%H"), "halte": neue_halte}
        speichere(ZUSTAND_FILE, zustand)

    aufraeumen()
    roh_tage = sorted(p.stem for p in DATA.glob("????-??-??.json"))
    seiten_tage = sorted(set(roh_tage) | {p.stem for p in DOCS.glob("????-??-??.html")})
    stand = datetime.now(TZ).strftime("%d.%m.%Y, %H:%M Uhr")
    for tag in roh_tage:
        if tag in tage or tag in seiten_tage[-2:] or not (DOCS / f"{tag}.html").exists():
            seite_bauen(tag, lade(DATA / f"{tag}.json", {}), namen, seiten_tage, stand, len(namen))
    print(f"Fertig. Aktualisierte Tage: {sorted(tage) or 'keine'}")


if __name__ == "__main__":
    main()
