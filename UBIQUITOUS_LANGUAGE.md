# Ubiquitous Language

Diese Begriffe meinen in Oberfläche, Dokumentation und Implementierung dasselbe. Englische Bezeichnungen lehnen sich an **Create Agent** an; die endgültigen UI-Labels sind offen.

| Begriff | Bedeutung |
| --- | --- |
| **Buzz Server** | Das eigenständige Produkt zum Betrieb und zur Verwaltung von Buzz Agents auf einem headless Linux-Host. Kein Buzz-Relay. |
| **Server-CLI** | Die vollständige Verwaltungsoberfläche auf dem Host. Kann Agenten ohne Desktop-Client anlegen, auflisten, starten, stoppen, konfigurieren und löschen. |
| **Host** | Der Rechner, auf dem ein Agent läuft. Ein **Remote Host** ist aus Sicht des Desktop-Clients ein per SSH erreichbarer Linux-Host mit Buzz Server. |
| **Desktop Client** | Eine konkrete Installation von Buzz Desktop mit Verwaltungs-Plugin. Optionale GUI für dieselben Verwaltungsoperationen; kein eigener Agentenbestand. |
| **Operator** | Eine Person mit der Berechtigung, auf einem Host Agenten zu verwalten. SSH-Erreichbarkeit allein beschreibt noch keine Verwaltungsberechtigung. |
| **Run Target** | Die Auswahl, wo der Agent laufen soll: auf dem lokalen Rechner oder auf einem Remote Host. Kein Inference Provider. |
| **Connection Test** | Prüft, ob der Desktop den gewählten Host erreicht und mit Buzz Server sprechen kann. Beweist weder, dass ein Agent läuft, noch dass das Modell antwortet. |
| **Agent** | Der in Buzz ansprechbare Assistent mit einer Agent Identity, einem Profil, Laufkonfiguration und gegebenenfalls Agent State. **Local** und **Remote** beschreiben seinen Laufort, nicht zwei Arten von Identität. |
| **Agent Identity** | Der stabile öffentliche Buzz-Schlüssel, unter dem der Agent ansprechbar ist und antwortet. Kein Anzeigename, kein Provider-Login und kein Linux-Konto. |
| **Agent Profile** | In Buzz sichtbare Angaben wie Anzeigename, Avatar und Beschreibung. Ein Profilwechsel erzeugt keine neue Agent Identity. |
| **Runtime Configuration** | Betriebsparameter des Agenten: gewähltes Harness, Provider und Modell, System Prompt sowie Verweise auf Zugänge. Nicht der bereits erarbeitete Zustand. |
| **Harness** | Die Laufzeitkomponente, die den Agenten ausführt bzw. seine Ausführung an Buzz anbindet. Der Inference Provider liefert Modellinferenz; er ist weder Harness noch Host. |
| **Agent State** | Dauerhaft erarbeiteter Zustand: Gedächtnis, gespeicherte Sitzungen und Arbeitsdateien. Öffentliche Buzz-Nachrichten, Prozessspeicher und Zugangsdaten sind davon zu unterscheiden. |
| **Linux Account** | Das Betriebssystemkonto, unter dem ein Agent läuft. Ein eigenes unprivilegiertes **Dienstkonto** pro Agent kann seine Dateien und Zugänge von denen anderer Agenten trennen. Nicht das SSH-Login des Operators und nicht die Agent Identity. |
| **Create Agent** | Einen neuen Agenten mit eigener Identität am gewählten Run Target anlegen. Kein Umzug eines bestehenden Agenten. |
| **Reconfigure Agent** | Die Konfiguration oder das Profil eines vorhandenen Agenten ändern, über die Server-CLI oder einen berechtigten Desktop. |
| **Move Agent** | Betrieb, Konfiguration und dauerhaften Zustand an ein anderes Run Target verlagern und dieselbe Agent Identity behalten. Kein zweiter aktiver Agent unter diesem Schlüssel. |
| **Delete Agent** | Den verwalteten Agenten am Host ausser Betrieb nehmen und aus der Verwaltung entfernen. Kein blosses Ausblenden auf einem Desktop; keine Zusage, frühere Buzz-Nachrichten oder sämtliche Daten zu vernichten. Die Aufbewahrungs- und Löschregeln sind offen. |

**Verwalten und Ansprechen sind getrennt:** Der Operator verwaltet über die Server-CLI oder über Desktop und SSH. Desktop und Mobile sprechen den Agenten über Buzz an. Dafür muss keine Verwaltungsverbindung offen sein.

Siehe [Vision](VISION.md) für den Ablauf mit zwei Desktops und Mobile.
