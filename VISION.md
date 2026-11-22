# Vision: Buzz Server

Wer Buzz Agents auf einem headless Linux-Host betreibt, soll sie vollständig über die **Server-CLI** verwalten können: anlegen, auflisten, starten, stoppen, konfigurieren und löschen. Dafür braucht es keinen Desktop-Client. Das Buzz-Desktop-Plugin ist eine optionale grafische Oberfläche für dieselben Verwaltungsoperationen. Ein Agent gehört nicht zu der Oberfläche, mit der er angelegt wurde. Der Host betreibt ihn; über Buzz ist er ansprechbar, etwa von Mobile aus.

## Zwei Wege zur selben Verwaltung

**Ohne Desktop:** Ich installiere die Server-CLI an der Konsole eines headless Linux-Hosts. Ich lege einen Agenten an, konfiguriere ihn, starte ihn und sehe seinen Status. Ich bekomme eine Antwort in Buzz, kann den Agenten umkonfigurieren, stoppen und löschen. Kein Schritt setzt voraus, dass Buzz Desktop installiert ist oder jemals verbunden war.

**Mit Desktop:**

1. Ich aktiviere das Plugin in Buzz Desktop und verbinde es per SSH mit dem Host, auf dem Buzz Server installiert ist.
2. In **Create Agent** wähle ich den Host als **Run Target**. Der **Connection Test** ist das erste sichtbare Erfolgssignal: Der gewählte Host ist erreichbar und der Client kann mit Buzz Server sprechen. Das ist noch kein Nachweis, dass ein Agent antworten kann.
3. Ich lege den Agenten an. Ich wähle Harness und Inference Provider, richte den Provider-Zugang ein und setze System Prompt, Profil und Avatar. Der Host startet den Agenten und betreibt ihn unabhängig vom Desktop.
4. Ich schliesse den ersten Desktop. Von Mobile aus erwähne ich den Agenten in Buzz und er antwortet. Mobile braucht dafür weder SSH noch das Plugin.
5. Ich verbinde einen zweiten berechtigten Desktop mit demselben Host. Er zeigt den bereits vorhandenen Agenten, ohne Daten vom ersten Desktop importieren zu müssen. Ich ändere dort zum Beispiel das Profil, prüfe die Änderung in Buzz und erhalte eine weitere Antwort auf Mobile. Wenn ich den Agenten anschliessend lösche, endet sein Betrieb auf dem Host; nur den Eintrag im zweiten Desktop zu entfernen, wäre kein Löschen.

Der **Host** hält den massgeblichen Verwaltungszustand für seine Remote Agents. Buzz bleibt die Plattform für Agent Identity, veröffentlichte Profile und Nachrichten. Ein Desktop darf Verbindungsdaten für seinen eigenen Zugang brauchen, aber keine unersetzliche Kopie der Agentenverwaltung besitzen. Die Abnahme verlangt Antworten ohne den ursprünglichen Desktop und zwischen Verwaltungsaktionen, keine Garantie auf unterbrechungsfreie Neustarts und keine Antworten nach dem Löschen.

CLI und Desktop zeigen dieselben Agenten: Einen in der Konsole angelegten Agenten kann ich später in Desktop bearbeiten und umgekehrt. Das Plugin führt keine zweite Agentenliste mit eigenem Lebenszyklus.

## Eigene Linux-Konten

Der Operator soll einem Agenten optional ein eigenes unprivilegiertes Linux-Dienstkonto zuordnen können. Der Agent läuft dann unter diesem Konto. Mit passenden Dateirechten bleiben Arbeitsdateien, Gedächtnis und Schlüssel vor anderen unprivilegierten Agenten geschützt. Das ist keine vollständige Sandbox. Das **Dienstkonto** des Agenten ist weder das SSH-Konto des Operators noch seine Buzz-Identität.

**Vorschlag für v1:** Ein Administrator richtet das Dienstkonto vorab ein; Buzz Server ordnet es zu. Desktop erhält keinen allgemeinen Root-Zugang. Einen Agenten zu löschen löscht nicht automatisch sein Linux-Konto. Automatisches Anlegen oder Entfernen von Konten ist eine spätere, gesonderte Entscheidung über Rechte und Datenaufbewahrung.

## Umziehen statt kopieren

Ein lokaler Agent soll später vollständig auf einen Remote Host umziehen können. Danach spreche ich weiterhin **denselben Agenten** an: gleiche Buzz-Identität und gleiches Profil, dieselbe Konfiguration, dasselbe Gedächtnis, gespeicherte Sitzungen und Arbeitsdateien. Auch die zum weiteren Betrieb nötigen Zugänge und Abhängigkeiten müssen am Ziel verfügbar sein. Das bedeutet nicht, dass jedes Geheimnis exportierbar ist: Wo ein Provider einen neuen Login verlangt, muss der Umzug das anzeigen statt stillschweigend Zustand oder Zugang zu verlieren.

Der Umzug verlagert den Betrieb, er erzeugt keinen Klon. Nie sollen Quelle und Ziel gleichzeitig mit derselben Agent Identity arbeiten. Der Zielhost muss die Übernahme nachweisen; bei einem Fehlschlag soll ich zum lokalen Betrieb zurückkehren können. Unterstützte Harnesses, portable Zustandsformate und der Umgang mit einem noch laufenden Turn bleiben zu klären.

## Schnitt für die erste Version – Vorschlag

- Zuerst den vollständigen **CLI-Ablauf** für einen neu angelegten Agenten nachweisen. In der Desktop-Integration zusätzlich den Verbindungstest, eine Antwort in Mobile und die Verwaltung desselben Agenten von zwei Desktops ohne Abhängigkeit vom ersten belegen. Das ist mehr als ein erfolgreicher SSH-Login.
- Vollständigen Umzug als eigenes nachfolgendes Ziel führen. Die erste Version darf die Agent Identity nicht an den ersten Desktop binden.
- Kein eigenes Tailnet, keine automatische Tailscale-Einrichtung und kein neues Relay als Voraussetzung.

## Upstream und offene Entscheidungen

Buzz Server ist ein eigenständiges Produkt; Buzz Desktop wird nicht geforkt. [Buzz-PR #3449](https://github.com/block/buzz/pull/3449) ist eine Referenz für Zielauswahl und Host-Check, keine Codebasis oder Voraussetzung. Seine Beschreibung nennt eine schreibgeschützte Remote-Server-Ansicht und keine Undeploy-Operation im Provider-Protokoll v1. Für das Plugin prüfen wir, welche unterstützten Erweiterungspunkte die nötigen Operationen abdecken. Falls einer fehlt, ist eine kleine Ergänzung in Buzz Desktop eine Option, die vor einem Beitrag mit den Maintainern abzustimmen wäre. Die CLI bleibt davon unabhängig.

Vor Implementierung entscheiden: Wie werden Operator-Rechte geprüft? Welche Zugangsdaten verbleiben am Host, welche werden am Ziel neu eingerichtet? Was wird beim Löschen unwiderruflich entfernt und was bleibt erhalten? Für welchen Harness lässt sich der erste vollständige Umzug sicher nachweisen?

Siehe [Ubiquitous Language](UBIQUITOUS_LANGUAGE.md).
