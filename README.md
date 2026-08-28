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

## Performance-Graph

DEGIRO liefert keine fertige historische Wertentwicklung über die API. Der
Graph wird deshalb **lokal in deinem Browser aufgebaut**: Jedes Mal, wenn du
die Seite mit laufendem Proxy öffnest bzw. „Aktualisieren“ klickst, wird ein
Datenpunkt (Datum, Gesamtwert, Cash) für den heutigen Tag gespeichert. Je
regelmässiger du die Seite öffnest, desto aussagekräftiger wird der Verlauf.
Es gibt keine rückwirkende Historie.

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
- Dies ist **keine Anlageberatung** und keine offizielle DEGIRO-Anwendung.

## Lokale Daten löschen

Button „Lokale Daten löschen“ auf der Seite entfernt alle im Browser
gespeicherten Werte (Proxy-Zugang, Portfolio, Transaktionen, Verlauf).
