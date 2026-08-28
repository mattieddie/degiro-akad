# degiro-akad

Privates Dashboard für den eigenen DEGIRO-Account: Performance-Graph und
Auswertung der einzelnen Positionen, basierend auf der inoffiziellen
Bibliothek [degiro-connector](https://github.com/Chavithra/degiro-connector).

**Live-Seite:** https://mattieddie.github.io/degiro-akad/

## Wie es funktioniert (wichtig zu verstehen)

Dieses Repository enthält **nur Code**, keine persönlichen Daten:

- Die Webseite (`index.html` / `assets/`) ist eine rein statische Seite, die
  auf GitHub Pages läuft. Sie speichert Login-Daten und abgerufene
  Portfolio-/Transaktionsdaten ausschliesslich im **localStorage deines
  Browsers** (nur auf deinem Gerät, nie auf GitHub).
- DEGIRO blockiert Anfragen von fremden Domains (CORS). Deshalb gibt es einen
  **lokalen Proxy** (`local-proxy/proxy.py`), den du selbst auf deinem
  Rechner startest. Er läuft nur auf `127.0.0.1`, hält deine DEGIRO-Sitzung
  nur im Arbeitsspeicher und schreibt nichts auf die Festplatte. Beendest du
  ihn, ist alles weg.
- Dein DEGIRO-**Passwort** wird nie gespeichert (weder im Browser noch vom
  Proxy) – du gibst es bei jeder Verbindung neu ein, es geht direkt und
  ausschliesslich an deinen eigenen lokalen Proxy und von dort an DEGIRO.

```
Browser (GitHub Pages, statisch)  <-- lokal -->  local-proxy/proxy.py (127.0.0.1)  <-->  DEGIRO
        |
        v
  localStorage (nur auf deinem Gerät)
```

Die Seite funktioniert nur, **solange der lokale Proxy bei dir läuft**. Ohne
laufenden Proxy zeigt sie die zuletzt lokal zwischengespeicherten Daten an.

## Setup

### 1. Lokalen Proxy vorbereiten

```bash
cd local-proxy
pip install -r requirements.txt
python proxy.py
```

Die Konsole zeigt ein **Proxy-Token** an – das brauchst du gleich auf der
Webseite. Falls dein GitHub-Pages-Link anders lautet als
`https://mattieddie.github.io`, starte mit:

```bash
python proxy.py --origin https://<dein-nutzername>.github.io
```

### 2. Webseite öffnen

Öffne https://mattieddie.github.io/degiro-akad/ (oder lokal via
`python -m http.server 8000` im Repo-Root und dann `http://localhost:8000`).

Trage ein:
- **Proxy-Adresse**: `http://127.0.0.1:8765` (Standard)
- **Proxy-Token**: aus der Konsolenausgabe von `proxy.py`
- **DEGIRO-Benutzername / -Passwort**
- **TOTP-Secret**: nur falls du 2FA per Authenticator-App aktiviert hast

Klick auf **„Verbinden & Daten laden“**.

### 3. GitHub Pages aktivieren (einmalig, falls noch nicht geschehen)

Im Repo unter *Settings → Pages*: Source = `Deploy from a branch`, Branch =
`main` / `/ (root)`.

## Alle Beträge in CHF

Der Proxy rechnet alle Geldbeträge (Kurse, Werte, Cash, G/V) automatisch in
Schweizer Franken um, via [api.frankfurter.app](https://www.frankfurter.app/)
(kostenlose, öffentliche EZB-Wechselkurse). Es wird jeweils der Kurs des
entsprechenden Tages verwendet – auch für die historische Rekonstruktion.

## Cash / „Available to Trade“

Die Kennzahl „Verfügbar“ stammt aus DEGIROs `CASH_FUNDS`-Block (dein
tatsächliches, freies Barguthaben), nicht aus den mehrdeutigen internen
Report-Feldern des Gesamtportfolios.

## Unrealisierte Gewinne/Verluste

Wird pro Position exakt berechnet als `Stück × (aktueller Kurs − Ø
Einstandskurs)`, in der Originalwährung, danach nach CHF umgerechnet.

## Performance-Graph

Der Graph kombiniert zwei Quellen:

1. **Seit-Kauf-Rekonstruktion** (`/api/history/backfill`): Der Proxy
   berechnet beim Verbinden aus deiner kompletten Transaktionshistorie plus
   DEGIROs Kurscharts einen Best-Effort-Verlauf zurück bis zu deinem ersten
   Kauf. Da das Kurschart-Rohformat von DEGIRO nicht offiziell dokumentiert
   ist, wird jede Kursserie automatisch gegen den separat gemeldeten
   Schlusskurs geprüft. Nur **verifizierte** Positionen fliessen mit echtem
   Kursverlauf ein; bei nicht verifizierten wird ein Näherungswert (letzter
   bekannter Kurs, flach) verwendet – die Seite zeigt dir unter dem Graphen
   an, wie viele Positionen betroffen sind.
2. **Lokale Tages-Schnappschüsse**: Zusätzlich wird bei jedem Öffnen der
   Seite mit laufendem Proxy ein exakter Datenpunkt für den heutigen Tag
   gespeichert (localStorage). Für bereits erfasste Tage überschreibt dieser
   exakte Wert die Näherung aus der Rekonstruktion.

Wählbare Zeiträume (1T/1W/1M/3M/6M/1J/YTD/Max) filtern diese kombinierte
Serie clientseitig. Da nur Tageswerte (kein Intraday) erfasst werden, zeigt
„1T“ entsprechend wenig Auflösung.

## Positions-Detailansicht

Klick auf eine Position öffnet ein Popup mit Kursverlauf (gleiche
Verifizierungslogik wie oben) sowie ISIN, Stückzahl, Kurs, Wert und
unrealisiertem G/V.

## Sicherheits- und Risikohinweise

- **Inoffizielle API**: `degiro-connector` nutzt die interne, nicht
  öffentlich dokumentierte Schnittstelle des DEGIRO-Webtraders. DEGIRO kann
  diese jederzeit ändern; die Nutzung liegt ausserhalb der offiziellen
  DEGIRO-Unterstützung und ggf. ausserhalb der Nutzungsbedingungen – Nutzung
  auf eigenes Risiko, nur für den eigenen Account.
- **App-Bestätigung**: Loggt sich DEGIRO von einem neuen Gerät/Standort ein,
  kann eine Bestätigung in der DEGIRO-App verlangt werden. Der Proxy wartet
  in diesem Fall bis zu 90 Sekunden – bitte in der App bestätigen.
- **Öffentliches Repo**: Der Code in diesem Repository ist öffentlich
  einsehbar. Er enthält **keine** Zugangsdaten oder Kontodaten – die
  entstehen erst zur Laufzeit lokal bei dir.
- **Proxy-Bindung**: `proxy.py` bindet ausschliesslich an `127.0.0.1`
  (nicht ans Netzwerk erreichbar) und verlangt für jede Anfrage das beim
  Start ausgegebene Token.
- **Externe FX-API**: Für die CHF-Umrechnung kontaktiert der Proxy
  `api.frankfurter.app`. Dabei werden ausschliesslich Währungscodes und
  Datumswerte übertragen – keine Konto- oder Portfoliodaten.
- **Historische Kursdaten**: Die Seit-Kauf-Rekonstruktion nutzt DEGIROs
  nicht offiziell dokumentiertes Kurschart-Format mit automatischer
  Plausibilitätsprüfung (siehe oben). Bitte die „nicht verifiziert“-Hinweise
  auf der Seite ernst nehmen.
- Dies ist **keine Anlageberatung** und keine offizielle DEGIRO-Anwendung.

## Lokale Daten löschen

Button „Lokale Daten löschen“ auf der Seite entfernt alle im Browser
gespeicherten Werte (Proxy-Zugang, Portfolio, Transaktionen, Verlauf).
