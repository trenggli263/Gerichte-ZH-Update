#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Wochensicht Zürcher Gerichte — Datenpipeline
============================================

Ablauf:
  1. Neuheiten-Liste von gerichte-zh.ch abrufen und die 8 Basisfelder parsen
     (Betreff, Entscheiddatum, Geschäftsnr., Gericht, Kammer, Entscheidart, Verweise, PDF).
  2. Jedes Entscheid-PDF herunterladen und Text extrahieren.
  3. Aus dem PDF ergänzen:
        - Mitwirkend  -> feste Textregel (steht immer im Kopf hinter "Mitwirkend:")
        - Publiziert  -> PDF-Erstellungsdatum als Näherung (zur Prüfung markiert)
        - Ergebnis    -> zuerst Regeln, bei Unsicherheit LLM-Klassifikator mit Konfidenz
  4. Ergebnis schreiben:
        - data.json         -> von der Webseite geladen
        - review_queue.csv  -> alle Zeilen, die du von Hand prüfen solltest

Was hier "ML" ist: Schritt 3 (Ergebnis). Kein eigenes Modell wird trainiert.
Ein LLM (Claude Haiku) liest nur das Dispositiv und ordnet es einer Kategorie zu,
mit Konfidenz 0..1 und der Beleg-Textstelle. Fällt die Konfidenz unter REVIEW_THRESHOLD
oder greift keine Regel, wird der Entscheid mit status="zu_prüfen" markiert.

Benötigt:  pip install requests beautifulsoup4 pdfplumber anthropic
Für den LLM-Schritt:  Umgebungsvariable ANTHROPIC_API_KEY setzen.
Ohne API-Key läuft alles trotzdem — dann nur mit Regeln, unsichere Fälle -> "zu_prüfen".
"""

import os, re, json, csv, io, sys, time, glob, shutil
import requests
from bs4 import BeautifulSoup
import pdfplumber

# ------------------------------------------------------------------ Konfiguration
BASE = "https://www.gerichte-zh.ch"
UEBERSICHT_URL = BASE + "/entscheide/entscheide-neuheiten.html"
NEUHEIT_URL = UEBERSICHT_URL + "?tx_frpentscheidsammlungextended_pi5%5Bneuheit%5D={id}"
PDF_BASE = BASE + "/fileadmin/user_upload/entscheide/oeffentlich/"
REVIEW_THRESHOLD = 0.80          # Konfidenz darunter -> manuelle Prüfung
LLM_MODEL = "claude-haiku-4-5-20251001"
STRAF_PREFIXE = {"GG", "GB", "GC", "DG", "DH", "SB", "SU", "UE"}
CORR_FILE = "corrections.json"        # von der Webseite exportierte, geprüfte Korrekturen
LERN_FILE = "lernbeispiele.json"      # daraus wachsende Beispiele für den LLM-Klassifikator
MAX_BEISPIELE = 6                     # so viele Beispiele bekommt das LLM je Anfrage

def lade_json(pfad, default):
    try:
        with open(pfad, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def lade_korrekturen():
    """Sucht die corrections.json automatisch – im Programmordner UND im Downloads-Ordner.
    Nimmt die zuletzt gespeicherte Datei (auch 'corrections(1).json' etc.), lädt sie und
    legt eine Kopie als ./corrections.json ab, damit sie dauerhaft erhalten bleibt."""
    downloads = os.path.join(os.path.expanduser("~"), "Downloads")
    kandidaten = glob.glob("corrections*.json") + glob.glob(os.path.join(downloads, "corrections*.json"))
    kandidaten = [p for p in kandidaten if os.path.isfile(p)]
    if not kandidaten:
        print("Keine corrections.json gefunden – Lauf ohne manuelle Korrekturen.")
        return {}
    pfad = max(kandidaten, key=os.path.getmtime)     # neueste Datei gewinnt
    print(f"Korrekturen geladen aus: {pfad}")
    daten = lade_json(pfad, {})
    # Kopie im Arbeitsordner sichern (sofern die Quelle woanders lag)
    ziel = os.path.abspath(CORR_FILE)
    if os.path.abspath(pfad) != ziel:
        try:
            shutil.copyfile(pfad, ziel)
            print(f"Kopie gesichert unter: {ziel}")
        except Exception as e:
            print("  Konnte Kopie nicht anlegen:", e, file=sys.stderr)
    return daten

ERGEBNIS_KATEGORIEN = [
    "Abweisung", "teilweise Gutheissung", "Gutheissung", "Nichteintreten",
    "Gegenstandslosigkeit", "Aufhebung", "Rückweisung",
    "Schuldspruch", "teilweiser Schuldspruch", "Freispruch", "andere",
]

# ------------------------------------------------------------------ 0. Wochen-Index
def hole_wochen_index() -> list[dict]:
    """Liest die Übersicht und gibt alle Wochen zurück: [{'id':748,'datum':'24.06.2026','anzahl':27}, ...]
    Die Liste ist nach Datum absteigend (neueste zuerst)."""
    html = requests.get(UEBERSICHT_URL, timeout=30).text
    wochen = []
    # Zeilen der Form: [24.06.2026](...neuheit=748) - 27 Entscheide
    for m in re.finditer(r"neuheit%5D=(\d+)\)\s*[\"']?\s*-?\s*(\d+)?", html):
        pass  # (Fallback nicht nötig – wir parsen unten strukturiert)
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.select('a[href*="neuheit%5D="], a[href*="neuheit]="]'):
        href = a.get("href", "")
        mid = re.search(r"neuheit(?:%5D|\])=(\d+)", href)
        datum = re.search(r"(\d{2}\.\d{2}\.\d{4})", a.get_text())
        if mid and datum:
            # Anzahl steht im Text nach dem Link ("- 27 Entscheide")
            tail = (a.parent.get_text(" ", strip=True) if a.parent else "")
            anz = re.search(re.escape(datum.group(1)) + r".*?(\d+)\s+Entscheide", tail)
            wochen.append({"id": int(mid.group(1)), "datum": datum.group(1),
                           "anzahl": int(anz.group(1)) if anz else None})
    # nach ID absteigend = neueste zuerst
    wochen = {w["id"]: w for w in wochen}
    return sorted(wochen.values(), key=lambda w: w["id"], reverse=True)

def neueste_woche() -> dict:
    idx = hole_wochen_index()
    if not idx:
        raise RuntimeError("Keine Wochen in der Übersicht gefunden.")
    return idx[0]


# ------------------------------------------------------------------ 1. Liste scrapen
def scrape_liste(neuheit_id: int) -> list[dict]:
    """Liest die Neuheiten-Liste und gibt die Basisfelder je Entscheid zurück."""
    html = requests.get(NEUHEIT_URL.format(id=neuheit_id), timeout=30).text
    soup = BeautifulSoup(html, "html.parser")
    eintraege = []

    # Jeder Entscheid beginnt mit einem <strong> (Betreff), gefolgt von der Kopfzeile
    # "datum | nr | gericht | kammer" und den Detailfeldern. Wir gehen die Detail-Blöcke
    # durch; robuste Variante über die PDF-Links, an denen die Geschäftsnr. hängt.
    for pdf_link in soup.select('a[href*="/entscheide/oeffentlich/"]'):
        pdf_href = pdf_link["href"]
        pdf_datei = pdf_href.rsplit("/", 1)[-1]
        block = pdf_link.find_parent()  # Umgebung des Eintrags
        text = block.get_text("\n", strip=True) if block else ""

        rec = {
            "betreff": _feld_vor(soup, pdf_link, "betreff"),
            "datum": _regex(text, r"(\d{2}\.\d{2}\.\d{4})"),
            "nr": _regex(text, r"\b([A-Z]{2}\d{6})\b"),
            "gericht": _detail(text, "Gericht"),
            "kammer": _detail(text, "Abteilung/Kammer"),
            "art": _detail(text, "Entscheidart"),
            "entscheiddatum": _detail(text, "Entscheiddatum"),
            "verweis": _verweis(text),
            "pdf": pdf_datei,
        }
        if rec["nr"]:
            eintraege.append(rec)
    return _dedupe(eintraege)


def _regex(t, p, default=""):
    m = re.search(p, t)
    return m.group(1) if m else default

def _detail(t, label):
    # Detailzeilen der Form "Entscheidart Urteil"
    m = re.search(re.escape(label) + r"\s+(.+)", t)
    return m.group(1).splitlines()[0].strip() if m else ""

def _verweis(t):
    m = re.search(r"Weiterzug[^\n]+", t)
    return m.group(0).strip() if m else ""

def _feld_vor(soup, node, _):
    # nächstgelegenes <strong> oberhalb des PDF-Links = Betreff
    strong = node.find_previous("strong")
    return strong.get_text(strip=True) if strong else ""

def _dedupe(rows):
    seen, out = set(), []
    for r in rows:
        if r["nr"] not in seen:
            seen.add(r["nr"]); out.append(r)
    return out


# ------------------------------------------------------------------ 2./3. PDF auswerten
def pdf_auswerten(pdf_datei: str) -> dict:
    """Lädt ein PDF, gibt Mitwirkende und Dispositiv zurück."""
    url = PDF_BASE + pdf_datei
    raw = requests.get(url, timeout=60).content
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        volltext = "\n".join(p.extract_text() or "" for p in pdf.pages)
    return {
        "mitwirkend": extrahiere_mitwirkend(volltext),
        "betreff": extrahiere_betreff(volltext),
        "dispositiv": extrahiere_dispositiv(volltext),
    }

def extrahiere_mitwirkend(t: str) -> str:
    # "Mitwirkend:" bis zur Entscheidart-Zeile ("... vom ...") oder "in Sachen".
    m = re.search(
        r"Mitwirkend[:\s]+(.+?)(?=\n\s*(?:Urteil|Beschluss|Verfügung|Zirkulationsbeschluss|"
        r"Endentscheid|Beschluss und Urteil|Urteil und Beschluss)[^\n]*\bvom\b|\n\s*in Sachen)",
        t, re.S)
    if not m:
        # Rückfall: bis zu 6 Zeilen nach "Mitwirkend:" nehmen
        m = re.search(r"Mitwirkend[:\s]+((?:.*\n){0,5}.*)", t)
        if not m:
            return ""
    return re.sub(r"\s+", " ", m.group(1)).strip()[:300]

def extrahiere_betreff(t: str) -> str:
    # Vollständiger "betreffend"-Text im Kopf, inkl. Beschwerde-/Aktenzeichen-Zusatz.
    m = re.search(
        r"\bbetreffend\s+(.+?)(?=\n\s*-\s*\d+\s*-|\n\s*Erwägungen|\n\s*Sachverhalt|"
        r"\n\s*Rechtsbegehren|\n\s*Anklage)",
        t, re.S)
    if not m:
        return ""
    return re.sub(r"\s+", " ", m.group(1)).strip()[:300]

def extrahiere_dispositiv(t: str) -> str:
    # Text ab der Entscheidformel; das ist die Grundlage für die Ergebnis-Klassierung.
    m = re.search(r"(Es wird (?:erkannt|beschlossen|verfügt)|wird verfügt|erkennt|beschliesst)\s*:",
                  t)
    if not m:
        return ""
    return t[m.start(): m.start() + 1200]


# ------------------------------------------------------------------ 3. Ergebnis: Regeln
def ergebnis_regeln(dispositiv: str, bereich: str):
    """Schnelle, transparente Regeln. Rückgabe (kategorie, konfidenz) oder (None, 0)."""
    d = dispositiv.lower()
    if bereich == "Straf":
        hat_schuldig = "ist schuldig" in d or "wird schuldig" in d
        hat_frei = "wird freigesprochen" in d or "ist freizusprechen" in d
        if hat_schuldig and hat_frei: return "teilweiser Schuldspruch", 0.85
        if hat_schuldig:              return "Schuldspruch", 0.9
        if hat_frei:                  return "Freispruch", 0.9
        return None, 0
    # Zivil / SchKG
    if "nicht eingetreten" in d or "wird nicht eingetreten" in d:      return "Nichteintreten", 0.9
    if "als gegenstandslos" in d or "abgeschrieben" in d:             return "Gegenstandslosigkeit", 0.85
    if "teilweise gutgeheissen" in d:                                 return "teilweise Gutheissung", 0.88
    if "wird gutgeheissen" in d or "werden gutgeheissen" in d:        return "Gutheissung", 0.88
    if "zurückgewiesen" in d and "vorinstanz" in d:                   return "Rückweisung", 0.8
    if ("wird aufgehoben" in d or "werden aufgehoben" in d or
        "ist aufzuheben" in d) and "abgewiesen" not in d:             return "Aufhebung", 0.8
    if "wird abgewiesen" in d or "werden abgewiesen" in d:            return "Abweisung", 0.9
    return None, 0


# ------------------------------------------------------------------ 3. Ergebnis: LLM
def ergebnis_llm(betreff, bereich, dispositiv, beispiele=None):
    """LLM-Klassifikator mit Konfidenz. Nutzt geprüfte Korrekturen als Few-Shot-Beispiele.
    Fällt aus, wenn kein API-Key vorhanden ist."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key or not dispositiv:
        return None, 0.0, ""
    try:
        import anthropic
    except ImportError:
        return None, 0.0, ""
    client = anthropic.Anthropic(api_key=key)

    # Aus den bisher von Hand geprüften Entscheiden lernen: passende Beispiele einblenden.
    bsp_text = ""
    for b in (beispiele or [])[:MAX_BEISPIELE]:
        bsp_text += (f'\nBeispiel (geprüft): Dispositiv "{b["dispositiv"][:300]}" '
                     f'-> Ergebnis: {b["ergebnis"]}')

    prompt = f"""Du klassifizierst das Verfahrensergebnis eines Schweizer Gerichtsentscheids.
Wähle GENAU eine Kategorie aus dieser Liste: {", ".join(ERGEBNIS_KATEGORIEN)}.
Orientiere dich an den geprüften Beispielen, falls vorhanden.
{bsp_text}

Jetzt klassifizieren:
Bereich: {bereich}
Betreff: {betreff}
Dispositiv (Entscheidformel):
\"\"\"{dispositiv[:1500]}\"\"\"

Antworte NUR als JSON, ohne weiteren Text:
{{"ergebnis": "<eine Kategorie>", "konfidenz": <0..1>, "beleg": "<kurze Textstelle als Begründung>"}}"""
    try:
        msg = client.messages.create(
            model=LLM_MODEL, max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        txt = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        data = json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
        cat = data.get("ergebnis", "")
        if cat not in ERGEBNIS_KATEGORIEN:
            return None, 0.0, ""
        return cat, float(data.get("konfidenz", 0)), str(data.get("beleg", ""))[:200]
    except Exception as e:
        print("  LLM-Fehler:", e, file=sys.stderr)
        return None, 0.0, ""


def bestimme_ergebnis(betreff, bereich, dispositiv, beispiele=None):
    """Regeln zuerst; nur unsichere/leere Fälle gehen ans LLM. Gibt Ergebnis-Dict zurück."""
    kat, konf = ergebnis_regeln(dispositiv, bereich)
    quelle, beleg = "regel", ""
    if kat is None or konf < REVIEW_THRESHOLD:
        lkat, lkonf, lbeleg = ergebnis_llm(betreff, bereich, dispositiv, beispiele)
        if lkat and lkonf >= konf:
            kat, konf, beleg, quelle = lkat, lkonf, lbeleg, "llm"
    status = "auto" if (kat and konf >= REVIEW_THRESHOLD) else "zu_prüfen"
    return {"ergebnis": kat or "", "konfidenz": round(konf, 2),
            "quelle": quelle, "beleg": beleg, "status": status}


# ------------------------------------------------------------------ Orchestrierung
def bereich_von_nr(nr: str) -> str:
    return "Straf" if nr[:2] in STRAF_PREFIXE else "Zivil"

def _merke_beispiel(liste, bereich, dispositiv, ergebnis):
    """Fügt ein geprüftes (Dispositiv -> Ergebnis)-Beispiel hinzu, ohne Duplikate."""
    schnipsel = re.sub(r"\s+", " ", dispositiv)[:400]
    for b in liste:
        if b.get("dispositiv") == schnipsel and b.get("ergebnis") == ergebnis:
            return liste
    liste.append({"bereich": bereich, "dispositiv": schnipsel, "ergebnis": ergebnis})
    return liste

def run(neuheit_id: int | None = None, out_json="data.json", out_review="review_queue.csv"):
    # Woche bestimmen: ohne Angabe automatisch die neueste aus der Übersicht.
    if neuheit_id is None:
        woche = neueste_woche()
        print(f"Neueste Woche automatisch erkannt: {woche['datum']} (ID {woche['id']})")
    else:
        idx = {w["id"]: w for w in hole_wochen_index()}
        woche = idx.get(neuheit_id, {"id": neuheit_id, "datum": ""})
    publikationsdatum = woche.get("datum", "")

    liste = scrape_liste(woche["id"])
    print(f"{len(liste)} Entscheide in der Liste gefunden.")

    korrekturen = lade_korrekturen()               # findet corrections.json auch in Downloads
    lernbeispiele = lade_json(LERN_FILE, [])        # wächst mit jeder Korrektur
    ergebnisse, review = [], []

    for i, rec in enumerate(liste, 1):
        rec["bereich"] = bereich_von_nr(rec["nr"])
        print(f"[{i}/{len(liste)}] {rec['nr']} …")
        try:
            pdf = pdf_auswerten(rec["pdf"])
        except Exception as e:
            print("  PDF-Fehler:", e, file=sys.stderr)
            pdf = {"mitwirkend": "", "betreff": "", "dispositiv": ""}

        # vollständigen Betreff aus dem PDF bevorzugen, sonst Listentext behalten
        if pdf.get("betreff"):
            rec["betreff"] = pdf["betreff"]

        # passende geprüfte Beispiele desselben Bereichs ans LLM geben
        rel = [b for b in lernbeispiele if b.get("bereich") == rec["bereich"]]
        erg = bestimme_ergebnis(rec["betreff"], rec["bereich"], pdf["dispositiv"], rel)

        rec.update({
            "mitwirkend": pdf["mitwirkend"],
            "publiziert": publikationsdatum,
            "ergebnis": erg["ergebnis"], "konfidenz": erg["konfidenz"],
            "quelle": erg["quelle"], "beleg": erg["beleg"],
            "status": "zu_prüfen" if (erg["status"] == "zu_prüfen" or not pdf["mitwirkend"]) else "auto",
        })

        # ---- geprüfte Korrektur hat Vorrang und wird zum Lernbeispiel ----
        c = korrekturen.get(rec["nr"])
        if c:
            if c.get("ergebnis"):    rec["ergebnis"] = c["ergebnis"]
            if c.get("mitwirkend"):  rec["mitwirkend"] = c["mitwirkend"]
            if c.get("publiziert"):  rec["publiziert"] = c["publiziert"]
            if c.get("geprüft"):
                rec.update({"status": "geprüft", "konfidenz": 1.0, "quelle": "manuell"})
                # Dispositiv + geprüftes Ergebnis merken -> Klassifikator lernt daraus
                if pdf["dispositiv"] and c.get("ergebnis"):
                    lernbeispiele = _merke_beispiel(lernbeispiele, rec["bereich"],
                                                    pdf["dispositiv"], c["ergebnis"])

        ergebnisse.append(rec)
        if rec["status"] == "zu_prüfen":
            review.append(rec)
        time.sleep(0.3)  # höflich zum Server

    json.dump(lernbeispiele, open(LERN_FILE, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({"stand": publikationsdatum or time.strftime("%d.%m.%Y"),
                   "neuheit_id": woche["id"], "entscheide": ergebnisse},
                  f, ensure_ascii=False, indent=1)

    with open(out_review, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Geschäftsnr", "Betreff", "Ergebnis(auto)", "Konfidenz", "Quelle", "Beleg", "PDF"])
        for r in review:
            w.writerow([r["nr"], r["betreff"], r["ergebnis"], r["konfidenz"],
                        r["quelle"], r["beleg"], PDF_BASE + r["pdf"]])

    print(f"\nFertig. {len(ergebnisse)} Entscheide -> {out_json}")
    print(f"Davon {len(review)} zur manuellen Prüfung -> {out_review}")


if __name__ == "__main__":
    # ohne Argument: neueste Woche automatisch; mit Argument: bestimmte Wochen-ID
    nid = int(sys.argv[1]) if len(sys.argv) > 1 else None
    run(nid)
