#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Wochensicht Zürcher Gerichte — Datenpipeline (Version 2)
=======================================================

Ablauf:
  1. Übersicht lesen -> alle Publikationswochen (Datum + ID).
  2. Für jede gewünschte Woche die Liste parsen (Betreff, Datum, Nr., Gericht,
     Kammer, Entscheidart, Verweise, PDF-Datei).
  3. Jedes PDF auswerten:
        - Betreffend  -> vollständiger "betreffend"-Text aus dem PDF-Kopf
        - Mitwirkend  -> Kopfzeile "Mitwirkend:"
        - Ergebnis    -> Ziffer 1 des Dispositivs, in Kategorie übersetzt;
                         wenn nicht eindeutig, wird Ziffer 1 wörtlich übernommen
  4. Publiziert = Publikationsdatum der Woche (gilt für alle Entscheide darin).
  5. data.json schreiben (Archiv über mehrere Wochen).

Aufruf:
    python pipeline.py            -> neueste Woche, ergänzt das Archiv
    python pipeline.py --wochen 5 -> die letzten 5 Wochen einlesen (Erstbefüllung)
    python pipeline.py --id 751   -> eine bestimmte Woche

Benötigt: pip install requests beautifulsoup4 pdfplumber
Optional (verbessert unklare Ergebnisse): ANTHROPIC_API_KEY + pip install anthropic
"""

import os, re, json, csv, io, sys, time, argparse
import requests
from bs4 import BeautifulSoup
import pdfplumber

BASE = "https://www.gerichte-zh.ch"
UEBERSICHT_URL = BASE + "/entscheide/entscheide-neuheiten.html"
WOCHEN_URL = UEBERSICHT_URL + "?tx_frpentscheidsammlungextended_pi5%5Bneuheit%5D={id}"
PDF_BASE = BASE + "/fileadmin/user_upload/entscheide/oeffentlich/"
OUT_JSON = "data.json"
OUT_REVIEW = "review_queue.csv"
LLM_MODEL = "claude-haiku-4-5-20251001"
STRAF_PREFIXE = {"GG", "GB", "GC", "DG", "DH", "SB", "SU", "UE"}

KATEGORIEN = ["Abweisung", "teilweise Gutheissung", "Gutheissung", "Nichteintreten",
              "Abschreibung", "Aufhebung", "Rückweisung", "Schuldspruch",
              "teilweiser Schuldspruch", "Freispruch"]

HEADERS = {"User-Agent": "Mozilla/5.0 (Wochensicht-Bot; Lesezugriff auf oeffentliche Entscheide)"}


# ---------------------------------------------------------------- Übersicht
def hole_wochen():
    """Alle Publikationswochen: [{'id':751,'datum':'15.07.2026'}, ...] neueste zuerst."""
    html = requests.get(UEBERSICHT_URL, headers=HEADERS, timeout=30).text
    wochen = {}
    for m in re.finditer(r'neuheit(?:%5D|\])=(\d+)', html):
        wochen.setdefault(int(m.group(1)), None)
    # Datum je ID aus dem Text zuordnen
    text = BeautifulSoup(html, "html.parser").get_text("\n")
    for m in re.finditer(r'(\d{2}\.\d{2}\.\d{4})', text):
        pass
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        mid = re.search(r'neuheit(?:%5D|\])=(\d+)', a["href"])
        md = re.search(r'(\d{2}\.\d{2}\.\d{4})', a.get_text())
        if mid and md:
            wochen[int(mid.group(1))] = md.group(1)
    liste = [{"id": i, "datum": d or ""} for i, d in wochen.items()]
    liste.sort(key=lambda w: w["id"], reverse=True)
    return liste


# ---------------------------------------------------------------- Wochenliste
KOPF = re.compile(r"(?m)^(\d{2}\.\d{2}\.\d{4})\s*\|\s*([A-Z]{2}\d{6})\s*\|\s*([^|\n]+?)\s*\|\s*(.*)$")

def _feld(block, label):
    m = re.search(r"(?m)^" + re.escape(label) + r"[ \t]+(.+)$", block)
    return m.group(1).strip() if m else ""

def scrape_liste(neuheit_id):
    """Parst eine Wochenliste. Liefert die Basisfelder je Entscheid."""
    html = requests.get(WOCHEN_URL.format(id=neuheit_id), headers=HEADERS, timeout=40).text
    text = BeautifulSoup(html, "html.parser").get_text("\n")

    # PDF-Dateinamen: Zuordnung über die Geschäftsnummer im Dateinamen
    pdfs = {}
    for m in re.finditer(r'/entscheide/oeffentlich/([A-Za-z0-9_\-]+\.pdf)', html):
        datei = m.group(1)
        nr = re.match(r"([A-Z]{2}\d{6})", datei)
        if nr:
            pdfs.setdefault(nr.group(1), datei)

    treffer = list(KOPF.finditer(text))
    if not treffer:
        print("  WARNUNG: keine Einträge erkannt – Seitenstruktur geändert?", file=sys.stderr)
    out = []
    for i, m in enumerate(treffer):
        start = m.end()
        ende = treffer[i + 1].start() if i + 1 < len(treffer) else len(text)
        block = text[start:ende]
        vor = text[:m.start()].rstrip().split("\n")
        betreff = vor[-1].strip() if vor else ""
        nr = m.group(2)
        vm = re.search(r"(?m)^Verweise\s*\n(Weiterzug[^\n]*)", block)
        out.append({
            "nr": nr,
            "entscheiddatum": m.group(1),
            "gericht": _feld(block, "Gericht") or m.group(3).strip(),
            "kammer": _feld(block, "Abteilung/Kammer"),
            "art": _feld(block, "Entscheidart"),
            "betreff": betreff,
            "verweis": vm.group(1).strip() if vm else "",
            "pdf": pdfs.get(nr, ""),
        })
    return out


# ---------------------------------------------------------------- PDF
def pdf_text(datei):
    raw = requests.get(PDF_BASE + datei, headers=HEADERS, timeout=90).content
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        return "\n".join(p.extract_text() or "" for p in pdf.pages)

def extrahiere_betreff(t):
    m = re.search(r"\bbetreffend\s+(.+?)(?=\n\s*-\s*\d+\s*-|\n\s*Erw(?:ä|ae)gungen|\n\s*Sachverhalt|"
                  r"\n\s*Rechtsbegehren|\n\s*Anklage|\n\s*Es wird)", t, re.S | re.I)
    return re.sub(r"\s+", " ", m.group(1)).strip()[:400] if m else ""

def extrahiere_mitwirkend(t):
    m = re.search(r"Mitwirkend[:\s]+(.+?)(?=\n\s*(?:Urteil|Beschluss|Verf(?:ü|ue)gung|Zirkulationsbeschluss|"
                  r"Endentscheid|Beschluss und Urteil|Urteil und Beschluss)[^\n]*\bvom\b|\n\s*in Sachen)",
                  t, re.S)
    if not m:
        m = re.search(r"Mitwirkend[:\s]+((?:.*\n){0,5}.*)", t)
    return re.sub(r"\s+", " ", m.group(1)).strip()[:350] if m else ""

DISPO_START = re.compile(r"(Es wird (?:erkannt|beschlossen|verf(?:ü|ue)gt)|"
                         r"Es wird beschlossen und erkannt|erkennt|beschliesst|verf(?:ü|ue)gt)\s*:", re.I)

def extrahiere_ziffer1(t):
    """Ziffer 1 des Dispositivs – dort steht in aller Regel das Ergebnis."""
    m = DISPO_START.search(t)
    if not m:
        return ""
    disp = t[m.end(): m.end() + 3000]
    z = re.search(r"1\.\s*(.+?)(?=\n\s*2\.\s|\Z)", disp, re.S)
    if not z:
        return ""
    txt = re.sub(r"-\s*\d+\s*-", " ", z.group(1))      # Seitenzahlen entfernen
    return re.sub(r"\s+", " ", txt).strip()

def klassiere(ziffer1):
    """Kategorie aus Ziffer 1. Unklar -> Ziffer 1 wörtlich (Konfidenz 0)."""
    if not ziffer1:
        return "", 0.0
    d = ziffer1.lower()
    kurz = len(ziffer1) < 140
    if "nicht eingetreten" in d and kurz:                        return "Nichteintreten", 0.95
    if ("abgeschrieben" in d or "als gegenstandslos" in d) and kurz: return "Abschreibung", 0.9
    if "teilweise gutgeheissen" in d and kurz:                   return "teilweise Gutheissung", 0.9
    if "gutgeheissen" in d and kurz:                             return "Gutheissung", 0.9
    if "abgewiesen" in d and kurz:                               return "Abweisung", 0.95
    if "aufgehoben" in d and kurz:                               return "Aufhebung", 0.85
    if "schuldig" in d and "nicht schuldig" not in d:            return "Schuldspruch", 0.9
    if "freigesprochen" in d:                                    return "Freispruch", 0.9
    return ziffer1[:300], 0.0

def llm_ergaenzung(betreff, bereich, ziffer1):
    """Optional: unklare Fälle vom LLM einordnen lassen."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key or not ziffer1:
        return None, 0.0
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        p = (f"Ordne das Verfahrensergebnis genau einer Kategorie zu: {', '.join(KATEGORIEN)}.\n"
             f"Bereich: {bereich}\nBetreff: {betreff}\nDispositiv Ziffer 1: \"{ziffer1[:900]}\"\n\n"
             'Antworte nur als JSON: {"ergebnis":"<Kategorie>","konfidenz":<0..1>}')
        msg = client.messages.create(model=LLM_MODEL, max_tokens=120,
                                     messages=[{"role": "user", "content": p}])
        txt = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        j = json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
        return (j.get("ergebnis") if j.get("ergebnis") in KATEGORIEN else None), float(j.get("konfidenz", 0))
    except Exception as e:
        print("  LLM-Hinweis:", e, file=sys.stderr)
        return None, 0.0


# ---------------------------------------------------------------- Ablauf
def bereich_von_nr(nr):
    return "Straf" if nr[:2] in STRAF_PREFIXE else "Zivil"

def verarbeite_woche(woche):
    print(f"\n=== Woche {woche['datum']} (ID {woche['id']}) ===")
    liste = scrape_liste(woche["id"])
    print(f"{len(liste)} Entscheide in der Liste gefunden.")
    ergebnisse = []
    for i, rec in enumerate(liste, 1):
        rec["bereich"] = bereich_von_nr(rec["nr"])
        rec["publiziert"] = woche["datum"]          # gilt für alle Entscheide der Woche
        print(f"[{i}/{len(liste)}] {rec['nr']}", end=" ", flush=True)
        try:
            t = pdf_text(rec["pdf"]) if rec["pdf"] else ""
        except Exception as e:
            print("PDF-Fehler:", e, file=sys.stderr)
            t = ""
        if t:
            b = extrahiere_betreff(t)
            if b:
                rec["betreff"] = b                   # vollständiger Betreff aus dem PDF
            rec["mitwirkend"] = extrahiere_mitwirkend(t)
            z1 = extrahiere_ziffer1(t)
            kat, konf = klassiere(z1)
            if konf == 0.0 and z1:
                lk, lkonf = llm_ergaenzung(rec["betreff"], rec["bereich"], z1)
                if lk and lkonf >= 0.8:
                    kat, konf = lk, lkonf
            if konf > 0:                      # eindeutige Kategorie erkannt
                rec["ergebnis"] = kat
                rec["erg_art"] = "kategorie"
                rec["wortlaut"] = ""
            else:                             # unklar -> Ziffer 1 wörtlich anzeigen
                rec["ergebnis"] = ""
                rec["erg_art"] = "wortlaut" if z1 else "fehlt"
                rec["wortlaut"] = z1[:400]
            rec["konfidenz"] = round(konf, 2)
        else:
            rec.update({"mitwirkend": "", "ergebnis": "", "erg_art": "fehlt",
                        "wortlaut": "", "konfidenz": 0.0})
        rec["status"] = "auto" if rec["konfidenz"] >= 0.8 else "zu_prüfen"
        print(f"-> {(rec['ergebnis'] or rec['wortlaut'])[:45] or '(leer)'}")
        ergebnisse.append(rec)
        time.sleep(0.25)
    return ergebnisse

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wochen", type=int, default=1, help="Anzahl der neuesten Wochen")
    ap.add_argument("--id", type=int, help="bestimmte Wochen-ID")
    args = ap.parse_args()

    alle = hole_wochen()
    if not alle:
        sys.exit("Keine Wochen in der Übersicht gefunden.")
    ziel = [w for w in alle if w["id"] == args.id] if args.id else alle[:max(1, args.wochen)]
    print("Zu verarbeiten:", ", ".join(f"{w['datum']}({w['id']})" for w in ziel))

    # bestehendes Archiv laden und ergänzen
    archiv = {}
    try:
        with open(OUT_JSON, encoding="utf-8") as f:
            alt = json.load(f)
        for w in alt.get("wochen", []):
            archiv[w["id"]] = w
        print(f"Bestehendes Archiv: {len(archiv)} Wochen")
    except Exception:
        pass

    for w in ziel:
        eintraege = verarbeite_woche(w)
        archiv[w["id"]] = {"id": w["id"], "datum": w["datum"],
                           "anzahl": len(eintraege), "entscheide": eintraege}

    wochen = sorted(archiv.values(), key=lambda w: w["id"], reverse=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump({"erstellt": time.strftime("%d.%m.%Y %H:%M"), "wochen": wochen},
                  f, ensure_ascii=False, indent=1)

    offen = [(w["datum"], e) for w in wochen for e in w["entscheide"] if e.get("status") != "auto"]
    with open(OUT_REVIEW, "w", encoding="utf-8", newline="") as f:
        cw = csv.writer(f)
        cw.writerow(["Woche", "Geschäftsnr", "Betreff", "Ergebnis", "Konfidenz", "PDF"])
        for datum, e in offen:
            cw.writerow([datum, e["nr"], e["betreff"][:80], e["ergebnis"][:80],
                         e["konfidenz"], PDF_BASE + e["pdf"]])

    total = sum(len(w["entscheide"]) for w in wochen)
    print(f"\nFertig: {len(wochen)} Wochen, {total} Entscheide -> {OUT_JSON}")
    print(f"Nicht eindeutig klassiert: {len(offen)} -> {OUT_REVIEW}")


if __name__ == "__main__":
    main()
