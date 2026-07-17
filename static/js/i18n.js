/* Leichtgewichtige Laufzeit-i18n (DE -> EN).
 *
 * Das UI ist auf Deutsch verfasst (Quellsprache). Bei Sprache "en" wird der
 * DOM einmalig übersetzt und über einen MutationObserver auch für dynamisch
 * erzeugte Inhalte (Tabellen, Status, Diagnose …) nachgezogen. Nachschlag über
 * ein Wörterbuch mit auf Einfach-Leerzeichen normalisierten Schlüsseln plus
 * ein paar Regex-Regeln für interpolierte Zähler/Status.
 *
 * Wechsel der Sprache lädt die Seite neu (sauberer als Rück-Übersetzung).
 */
(function () {
  "use strict";

  var DICT = {
    // --- Sidebar / Navigation ---
    "VMAF-gesteuerte Komprimierung": "VMAF-guided compression",
    "Audio-Optimierung": "Audio Optimization",
    "A/B-Vergleich": "A/B Compare",
    "Warteschlange": "Queue",
    "Statistik": "Statistics",
    "Bibliothek": "Library",
    "Daten & Archive": "Data & Archives",
    "Einstellungen": "Settings",
    "Diagnose": "Diagnostics",
    "Design-Modus": "Theme",
    "Server Anthrazit": "Server Anthracite",
    "Hardware-Auslastung": "Hardware Usage",
    "— Threads": "— threads",
    "Verlauf (≈2 min)": "History (≈2 min)",
    "Verbinde …": "Connecting …",
    "Gesamt eingespart": "Total saved",

    // --- Detail-Modal / Player ---
    "Schließen": "Close",
    "Details": "Details",
    "Details / ffprobe anzeigen": "Show details / ffprobe",
    "Im Browser abspielen": "Play in browser",
    "Wiedergabe": "Playback",
    "Lade …": "Loading …",
    "Quelle": "Source",
    "Ausgabe": "Output",
    "Ø Speed": "Avg speed",
    "Ø FPS": "Avg FPS",
    "Original": "Original",
    "Dauer": "Duration",
    "Eingespart": "Saved",
    "Keine Analyse verfügbar.": "No analysis available.",
    "Keine abspielbare Datei gefunden.": "No playable file found.",
    "🎞 Im A/B-Vergleich öffnen (alt vs. neu)": "🎞 Open in A/B compare (old vs. new)",
    "Encoder wirklich testen": "Really test encoders",
    "Encoder-Funktionstest läuft (kann etwas dauern) …": "Encoder function test running (may take a while) …",
    "Encoder werden real getestet …": "Encoders are being tested for real …",
    " — von der Hardware nicht unterstützt": " — not supported by hardware",
    " — nicht verfügbar": " — not available",
    "Läuft die Wiedergabe nicht, unterstützt der Browser den Codec (z. B. HEVC/AV1) evtl. nicht direkt.":
      "If playback fails, your browser may not support the codec (e.g. HEVC/AV1) directly.",

    // --- Topbar ---
    "Quelle:": "Source:",
    "· Daten:": "· Data:",
    "Bereit": "Ready",

    // --- Karte 1: Quellenauswahl ---
    "Quellenauswahl & Analyse": "Source Selection & Analysis",
    "Nichts ausgewählt": "Nothing selected",
    "Lade Verzeichnis …": "Loading directory …",
    "Diesen Ordner auswählen (Batch)": "Select this folder (batch)",

    // --- Karte 2: Encoding ---
    "Enkodierung & Workflow": "Encoding & Workflow",
    "Profil": "Profile",
    "— kein Profil —": "— no profile —",
    "Speichern": "Save",
    "Löschen": "Delete",
    "Aktuelle Einstellungen als Profil speichern": "Save current settings as a profile",
    "Gewähltes Profil löschen": "Delete selected profile",
    "GPU / Plattform": "GPU / Platform",
    "Zielauflösung": "Target resolution",
    "Original beibehalten": "Keep original",
    "HDR-Behandlung": "HDR handling",
    "HDR → SDR (Tone-Mapping)": "HDR → SDR (tone mapping)",
    "HDR beibehalten (10-bit)": "Keep HDR (10-bit)",
    "Nur bei HDR-Quellen. „Beibehalten“ überträgt HDR10/HLG-Metadaten (kein Dolby-Vision-Layer).":
      "HDR sources only. \u201CKeep\u201D transfers HDR10/HLG metadata (no Dolby Vision layer).",
    "Dolby Vision erkannt: Beim Re-Encode kann die dynamische DV-Schicht (RPU) nicht übernommen werden. Bei Profil 8.1 bleibt die HDR10-Basis erhalten; bei Profil 5 wird Tone-Mapping empfohlen.":
      "Dolby Vision detected: the dynamic DV layer (RPU) cannot be carried over on re-encode. Profile 8.1 keeps the HDR10 base; profile 5 recommends tone mapping.",
    "Dolby Vision (RPU) beibehalten – experimentell, nur HEVC/Profil 8.1":
      "Keep Dolby Vision (RPU) – experimental, HEVC/profile 8.1 only",
    "Extrahiert die DV-RPU aus der Quelle und re-injiziert sie nach dem HEVC-Encode (dovi_tool). Erzwingt „HDR beibehalten“. Bei Fehlschlag bleibt die reine HDR10-Ausgabe erhalten.":
      "Extracts the DV RPU from the source and re-injects it after the HEVC encode (dovi_tool). Forces \u201CKeep HDR\u201D. On failure the plain HDR10 output is kept.",
    "Aus Quelle übernehmen": "Copy from source",
    "Untertitel": "Subtitles",
    "Kapitelmarken": "Chapter markers",
    "Metadaten (Titel, Tags)": "Metadata (title, tags)",
    "Rauschminderung (Denoise)": "Noise reduction (denoise)",
    "Aus": "Off",
    "Leicht": "Light",
    "Mittel": "Medium",
    "Stark": "Strong",
    "Film-Grain-Synthese:": "Film grain synthesis:",
    "Nur AV1 (CPU/SVT). 0 = aus. Spart Bits bei körnigem Material.":
      "AV1 only (CPU/SVT). 0 = off. Saves bits on grainy material.",
    "Zwei-Pass (nur im Bitraten-Modus – gleichmäßigere Qualität)":
      "Two-pass (bitrate mode only – more consistent quality)",
    "Anime-Modus (VMAF-NEG-Bewertung + 10-bit gegen Banding)":
      "Anime mode (VMAF-NEG scoring + 10-bit against banding)",
    "Chunked Adaptive Encoding (Segmente mit komplexitätsabhängigem CQ – nur CQ-Modus, langsamer)":
      "Chunked adaptive encoding (segments with complexity-based CQ – CQ mode only, slower)",
    "Segmentlänge (Sekunden)": "Segment length (seconds)",
    "CQ-Spanne (±)": "CQ range (±)",
    "Aufwändige Szenen erhalten bessere Qualität (niedrigerer CQ), ruhige Szenen werden kleiner. Der eingestellte CQ ist der Mittelwert.":
      "Complex scenes get better quality (lower CQ), calm scenes get smaller. The set CQ is the average.",
    "Qualitäts-Guardrail: echten VMAF nach dem Encode messen":
      "Quality guardrail: measure real VMAF after the encode",
    "Ziel-VMAF (min.)": "Target VMAF (min.)",
    "Bei Unterschreiten automatisch mit höherer Qualität neu encoden":
      "If below, automatically re-encode at higher quality",
    "Nach dem Encode werden Stichproben-Clips der Ausgabe gegen die Quelle gemessen. Liegt der VMAF darunter, wird (falls aktiviert) mit besserer Qualität wiederholt – sonst als Warnung markiert.":
      "After the encode, sample clips of the output are measured against the source. If VMAF is below target, it re-encodes at higher quality (if enabled) – otherwise it is flagged as a warning.",
    "Qualität": "Quality",
    "Steuerungsmodus": "Rate control",
    "CQ / QP / CRF (Qualitätszahl)": "CQ / QP / CRF (quality number)",
    "Festbitrate (CBR)": "Fixed bitrate (CBR)",
    "Average Bitrate (VBR-Ziel)": "Average bitrate (VBR target)",
    "Qualität (CQ/QP/CRF):": "Quality (CQ/QP/CRF):",
    "10 · hohe Qualität": "10 · high quality",
    "51 · klein": "51 · small",
    "Ziel-Bitrate": "Target bitrate",
    "kbit/s · CBR = feste Rate, ABR = variables Ziel.":
      "kbit/s · CBR = fixed rate, ABR = variable target.",
    "Reines Encoding ohne Test-Encodes. Für Vergleiche und automatische Wertfindung das":
      "Pure encoding without test encodes. For comparisons and automatic value-finding use the",
    "nutzen und den Gewinner übernehmen.": "and apply the winner.",
    "Ton – Standard für alle Spuren": "Audio – default for all tracks",
    "Original kopieren (verlustfrei, schnell)": "Copy original (lossless, fast)",
    "Neu codieren": "Re-encode",
    "Ohne Ton (entfernen)": "No audio (remove)",
    "Gilt für Spuren auf „Standard“. Einzelne Spuren lassen sich in der Quellanalyse abweichend einstellen.":
      "Applies to tracks set to \u201CDefault\u201D. Individual tracks can be set differently in the source analysis.",
    "Audio-Codec": "Audio codec",
    "AAC (universell)": "AAC (universal)",
    "Opus (effizient)": "Opus (efficient)",
    "FLAC (verlustfrei)": "FLAC (lossless)",
    "Kanäle": "Channels",
    "Audio-Bitrate:": "Audio bitrate:",
    "Lautstärke normalisieren (EBU R128, −16 LUFS)": "Normalize loudness (EBU R128, −16 LUFS)",
    "Nach erfolgreichem Encode": "After a successful encode",
    "Original behalten + Suffix": "Keep original + suffix",
    "Inplace ersetzen (Original löschen)": "Replace in place (delete original)",
    "Original nach .archiv/ verschieben": "Move original to .archiv/",
    "Zur Warteschlange hinzufügen": "Add to queue",

    // --- VMAF-Tool ---
    "VMAF-Vergleich": "VMAF Comparison",
    "Reiner Vergleich mehrerer Encoder/Codecs und Qualitätsstufen – es wird":
      "Pure comparison of several encoders/codecs and quality levels – it is",
    "nicht": "not",
    "encodiert. Der Gewinner lässt sich anschließend ins Encoding übernehmen.":
      "encoded. The winner can then be applied to encoding.",
    "GPU / Plattform (Basis)": "GPU / Platform (base)",
    "Codec (Basis)": "Codec (base)",
    "Testwerte (1–4 · Feld leeren = weniger Tests)": "Test values (1–4 · clear field = fewer tests)",
    "CQ/QP: niedrig = hohe Qualität · hoch = kleinere Datei":
      "CQ/QP: low = high quality · high = smaller file",
    "Vergleichsclip-Länge:": "Comparison clip length:",
    "Sek.": "sec.",
    "Stichproben (Szenen)": "Samples (scenes)",
    "1 – nur Mitte (schnell)": "1 – middle only (fast)",
    "2 Szenen": "2 scenes",
    "3 Szenen (robuster)": "3 scenes (more robust)",
    "Mehr Stichproben = genauere Größenprognose, aber längere Analyse.":
      "More samples = more accurate size prediction, but longer analysis.",
    "Zusätzlich vergleichen mit": "Additionally compare with",
    "Alle auf diesem System verfügbaren Encoder. Mehr Encoder = längerer Vergleich.":
      "All encoders available on this system. More encoders = longer comparison.",
    "Screenshots für optischen Vergleich erstellen": "Create screenshots for visual comparison",
    "VMAF-Vergleich starten": "Start VMAF comparison",
    "VMAF-Analyse & Größenprognose": "VMAF Analysis & Size Prediction",
    "Aktuelle Analyse": "Current analysis",
    "Früheren Vergleich ansehen": "View an earlier comparison",
    "Archiv-Ansicht – gespeicherter Vergleich.": "Archive view – saved comparison.",
    "Zur aktuellen Analyse": "To current analysis",
    "Einstellung": "Setting",
    "Prognose": "Prediction",
    "Ersparnis": "Savings",
    "Wähle eine Qualitätsstufe für den Encode:": "Choose a quality level for the encode:",
    "Nur Vergleich – kein Encode": "Compare only – no encode",

    // --- Live / Warteschlange ---
    "Live-Verarbeitung": "Live Processing",
    "Warteschlange & Status": "Queue & Status",
    "Wie viele Encodes gleichzeitig laufen dürfen": "How many encodes may run simultaneously",
    "Pausieren": "Pause",
    "Fortsetzen": "Resume",
    "Erledigte entfernen": "Clear completed",
    "Titel": "Title",
    "Auflösung": "Resolution",
    "Dauer": "Duration",
    "Warteschlange ist leer.": "Queue is empty.",
    "Abbrechen": "Cancel",
    "Nach oben": "Move up",
    "Nach unten": "Move down",
    "Verarbeitung läuft": "Processing",
    "Pausiert": "Paused",

    // --- Daten & Archive ---
    "Datenordner & VMAF-Archive": "Data Folder & VMAF Archives",
    "Aktualisieren": "Refresh",
    "Bereich": "Area",
    "VMAF-Sessions (Clips & Logs)": "VMAF sessions (clips & logs)",
    "Arbeit / Temp": "Work / Temp",
    "Gesamten Bereich leeren": "Clear entire area",
    "Lade …": "Loading …",
    "Vorschau": "Preview",
    "Schließen": "Close",

    // --- Statistik ---
    "Statistik & Historie": "Statistics & History",
    "Historie leeren": "Clear history",
    "Letzte Jobs": "Recent jobs",
    "Original": "Original",
    "Ergebnis": "Result",
    "Datum": "Date",
    "Noch keine Jobs.": "No jobs yet.",
    "Encodes fertig": "Encodes done",

    // --- Bibliothek ---
    "Bibliotheks-Scan": "Library Scan",
    "Durchsucht den Eingabeordner rekursiv und filtert Videos (z. B. „alle H.264 > 10 Mbit/s“). Treffer lassen sich mit den aktuellen Encode-Einstellungen in die Warteschlange legen.":
      "Recursively scans the input folder and filters videos (e.g. \u201Call H.264 > 10 Mbit/s\u201D). Matches can be queued with the current encode settings.",
    "Name enthält": "Name contains",
    "Name ausschließen": "Exclude name",
    "z. B. sample, trailer, .archiv": "e.g. sample, trailer, .archiv",
    "Kommagetrennt – Dateien, deren Pfad einen dieser Begriffe enthält, werden übersprungen":
      "Comma-separated – files whose path contains one of these terms are skipped",
    "Kommagetrennt – Pfade mit einem dieser Begriffe werden übersprungen":
      "Comma-separated – paths containing one of these terms are skipped",
    "Min. Größe (MB)": "Min. size (MB)",
    "Min. Video-Bitrate (Mbit/s)": "Min. video bitrate (Mbit/s)",
    "Min. Höhe (px)": "Min. height (px)",
    "Codec-Filter": "Codec filter",
    "Alle Codecs": "All codecs",
    "Nur nicht-AV1 (AV1 überspringen)": "Only non-AV1 (skip AV1)",
    "Nur H.264": "Only H.264",
    "Nur HEVC/H.265": "Only HEVC/H.265",
    "Projektion für Ziel-Codec": "Projection for target codec",
    "Bereits effiziente Dateien ausblenden": "Hide already efficient files",
    "Bereits verarbeitete Dateien ausblenden": "Hide already processed files",
    "Treffer": "Matches",
    "Gesamtgröße": "Total size",
    "Einsparung (ca.)": "Savings (approx.)",
    "Anteil": "Share",
    "Scan starten": "Start scan",
    "Auswahl zur Warteschlange": "Selection to queue",
    "Datei": "File",
    "Bitrate": "Bitrate",
    "Größe": "Size",
    "Noch kein Scan.": "No scan yet.",
    "Keine Treffer.": "No matches.",

    // --- Super-Tool ---
    "Super-Tool – Stapel-Assistent": "Super-Tool – Batch Assistant",
    "Ganze Ordner automatisch prüfen und neu encodieren: Ordner + Filter wählen, Qualitätsmodus festlegen, scannen und starten. Der Fortschritt je Datei erscheint unten.":
      "Automatically check and re-encode entire folders: pick folder + filters, choose a quality mode, scan and start. Per-file progress appears below.",
    "Ordner auswählen": "Select folder",
    "Aktuell: gesamter Eingabeordner (alle Unterordner)": "Currently: entire input folder (all subfolders)",
    "Passende Dateien": "Matching files",
    "Ordner wählen …": "Choose a folder …",
    "Vorschau nach Ordner/Name/Format/Größe (ohne Codec-/Bitraten-/Höhen-Filter – diese greifen erst beim Scan).":
      "Preview by folder/name/format/size (without codec/bitrate/height filters – those apply on scan).",
    "Container / Formate": "Container / Formats",
    "Nichts auswählen = alle unterstützten Formate.": "Select nothing = all supported formats.",
    "Qualitätsmodus": "Quality mode",
    "Ziel-VMAF – pro Datei Test-Encodes (autonom)": "Target VMAF – per-file test encodes (autonomous)",
    "Repräsentativ – 1 VMAF-Check für alle": "Representative – 1 VMAF check for all",
    "Feste Qualität – ohne VMAF": "Fixed quality – without VMAF",
    "Ziel-VMAF:": "Target VMAF:",
    "Aus den Test-Encodes wird der effizienteste Wert (kleinste Datei) mit VMAF ≥ Ziel automatisch gewählt.":
      "From the test encodes, the most efficient value (smallest file) with VMAF ≥ target is chosen automatically.",
    "Test-Steuerung": "Test control",
    "Szenen-Stichproben": "Scene samples",
    "1 (Mitte)": "1 (middle)",
    "2 Szenen": "2 scenes",
    "3 Szenen": "3 scenes",
    "Clip-Länge (Sekunden)": "Clip length (seconds)",
    "Test-CQ-Werte": "Test CQ values",
    "Test-Bitraten (kbit/s)": "Test bitrates (kbit/s)",
    "(Anzahl der Felder = Test-Encodes je Datei)": "(number of fields = test encodes per file)",
    "Leere Felder werden ignoriert. Niedriger CQ = höhere Qualität/größer.":
      "Empty fields are ignored. Lower CQ = higher quality/larger.",
    "Leere Felder werden ignoriert. Höhere Bitrate = höhere Qualität/größer.":
      "Empty fields are ignored. Higher bitrate = higher quality/larger.",
    "Ziel-Bitrate (kbit/s)": "Target bitrate (kbit/s)",
    "Ton": "Audio",
    "Original kopieren": "Copy original",
    "Neu codieren (AAC 160k)": "Re-encode (AAC 160k)",
    "Ohne Ton": "No audio",
    "Scan – Codec/Bitrate ermitteln": "Scan – detect codec/bitrate",
    "Stapel starten": "Start batch",
    "Container": "Container",
    "Stapel-Fortschritt": "Batch Progress",

    // --- Einstellungen: Benachrichtigungen ---
    "Benachrichtigungen": "Notifications",
    "Bei Fertigstellung oder Fehler wird über die konfigurierten Kanäle informiert. Alle Felder sind optional.":
      "On completion or error, the configured channels are notified. All fields are optional.",
    "Discord Webhook-URL": "Discord webhook URL",
    "Telegram Bot-Token": "Telegram bot token",
    "unverändert lassen zum Beibehalten": "leave unchanged to keep",
    "Telegram Chat-ID": "Telegram chat ID",
    "z. B. 123456789": "e.g. 123456789",
    "Generischer Webhook (JSON POST)": "Generic webhook (JSON POST)",
    "Ereignisse": "Events",
    "Bei Fertigstellung": "On completion",
    "Bei Fehler": "On error",
    "Testnachricht senden": "Send test message",
    "gesetzt – leer lassen zum Beibehalten": "set – leave empty to keep",
    "Bot-Token": "Bot token",
    "Gesendet ✓": "Sent ✓",
    "Aktiv": "Active",

    // --- Einstellungen: Zeitplan ---
    "Zeitplan & Last": "Schedule & Load",
    "Steuert, wann neue Encodes starten dürfen. Laufende Jobs werden nie unterbrochen.":
      "Controls when new encodes may start. Running jobs are never interrupted.",
    "Zeitplan aktiv": "Schedule active",
    "Nur in Zeitfenster encoden": "Encode only within time window",
    "Ab Stunde": "From hour",
    "Bis Stunde": "To hour",
    "Fenster über Mitternacht ist erlaubt (z. B. 22 → 6).":
      "A window across midnight is allowed (e.g. 22 → 6).",
    "Last-Drosselung aktiv": "Load throttling active",
    "Keine neuen Jobs über CPU-Auslastung (%)": "No new jobs above CPU usage (%)",
    "Encodes sind aktuell freigegeben.": "Encodes are currently allowed.",
    "Zeitplan deaktiviert – Encodes laufen jederzeit.": "Schedule disabled – encodes run anytime.",

    // --- Einstellungen: API ---
    "Neuen Schlüssel erzeugen": "Generate new key",
    "Widerrufen": "Revoke",
    "Keine gespeicherten Schlüssel.": "No stored keys.",
    "Geschützt": "Protected",
    "Offen": "Open",
    "In Sonarr/Radarr unter": "In Sonarr/Radarr under",
    "als URL eintragen (Trigger „On Import“/„On Upgrade“):":
      "enter as the URL (trigger \u201COn Import\u201D/\u201COn Upgrade\u201D):",
    "Optional Profil anhängen:": "Optionally append a profile:",

    // --- Einstellungen: Watch-Ordner ---
    "Watch-Ordner": "Watch Folder",
    "Überwacht einen Unterordner des Eingabeordners und legt neue Videos automatisch in die Warteschlange – optional nur in einem Zeitfenster.":
      "Watches a subfolder of the input folder and queues new videos automatically – optionally only within a time window.",
    "Watch-Ordner aktiv": "Watch folder active",
    "Ordner (relativ zum Eingabeordner)": "Folder (relative to input folder)",
    "leer = gesamter Eingabeordner": "empty = entire input folder",
    "Prüfintervall (Minuten)": "Check interval (minutes)",
    "Profil anwenden": "Apply profile",
    "Standard-Einstellungen": "Default settings",
    "Aktiv ab Stunde": "Active from hour",
    "Aktiv bis Stunde": "Active to hour",
    "immer": "always",
    "Jetzt prüfen": "Check now",

    // --- Audio-Optimierung ---
    "Aktuell: gesamter Eingabeordner": "Currently: entire input folder",
    "Zieleinstellungen": "Target settings",
    "Ziel-Codec": "Target codec",
    "Bitrate (kbit/s, 0 = automatisch)": "Bitrate (kbit/s, 0 = automatic)",
    "Umfang": "Scope",
    "Nur aufgeblähte Spuren": "Bloated tracks only",
    "Alle Tonspuren": "All audio tracks",
    "Als „aufgebläht“ ab (kbit/s)": "Considered \u201Cbloated\u201D from (kbit/s)",
    "Verlustfreie Codecs (TrueHD/DTS-HD/PCM/FLAC) gelten immer als aufgebläht.":
      "Lossless codecs (TrueHD/DTS-HD/PCM/FLAC) always count as bloated.",
    "Lautheit normalisieren (EBU R128)": "Normalize loudness (EBU R128)",
    "Nach Erfolg": "After success",
    "Original behalten (Suffix)": "Keep original (suffix)",
    "Original ersetzen": "Replace original",
    "Original ins .archiv verschieben": "Move original to .archiv",
    "Ordner scannen": "Scan folder",
    "Aufgeblähte Spuren": "Bloated tracks",
    "Auswahl optimieren": "Optimize selection",
    "Verkleinert aufgeblähte Tonspuren (TrueHD, DTS-HD MA, PCM, FLAC …) ohne das Video neu zu encoden – das Video wird 1:1 kopiert (":
      "Shrinks bloated audio tracks (TrueHD, DTS-HD MA, PCM, FLAC …) without re-encoding the video – the video is copied 1:1 (",
    "). Spart bei Blu-ray-Remuxes oft mehrere GB in Sekunden.":
      "). Often saves several GB in seconds on Blu-ray remuxes.",

    // --- A/B-Vergleich ---
    "A/B-Vergleichsplayer": "A/B Compare Player",
    "Spielt zwei Videos synchron nebeneinander ab – ideal für Vorher/Nachher. Hinweis: Der Browser muss den Codec abspielen können (AV1/VP9/H.264 meist ja, HEVC oft nicht). Bei nicht abspielbaren Dateien den Screenshot-Vergleich im VMAF-Tool nutzen.":
      "Plays two videos side by side in sync – ideal for before/after. Note: the browser must be able to play the codec (AV1/VP9/H.264 usually yes, HEVC often not). For unplayable files use the screenshot comparison in the VMAF tool.",
    "Eingang": "Input",
    "Ausgang": "Output",
    "relativer Pfad, z. B. Filme/film.mkv": "relative path, e.g. Movies/film.mkv",
    "relativer Pfad, z. B. Filme/film_av1.mkv": "relative path, e.g. Movies/film_av1.mkv",
    "Laden": "Load",
    "Synchron": "Sync",
    "Versatz B (s)": "Offset B (s)",
    "▶ / ⏸ (beide)": "▶ / ⏸ (both)",

    // --- Diagnose ---
    "Diagnose & Selbsttest": "Diagnostics & Self-Test",
    "Prüft, ob alle Bausteine vorhanden und nutzbar sind: FFmpeg & Encoder, VMAF-Modelle, dovi_tool, GPU/VAAPI und die Datenordner. Hilfreich nach einem Update oder bei Encode-Fehlern.":
      "Checks that all building blocks are present and usable: FFmpeg & encoders, VMAF models, dovi_tool, GPU/VAAPI and the data folders. Helpful after an update or on encode errors.",
    "Selbsttest ausführen": "Run self-test",
    "Selbsttest läuft …": "Self-test running …",
    "Warnung": "Warning",
    "Fehler": "Error",

    // --- dynamische JS-Strings (fix) ---
    "Auswählen": "Select",
    "Leerer Ordner.": "Empty folder.",
    "Datei ausgewählt": "File selected",
    "Ordner ausgewählt (Batch)": "Folder selected (batch)",
    "Live verbunden": "Live connected",
    "Getrennt – erneuter Versuch …": "Disconnected – retrying …",
    "Keine weiteren Encoder verfügbar.": "No further encoders available.",
    "GPU / Hardware": "GPU / Hardware",
    "CPU / Software": "CPU / Software",
    "Referenz-Clip": "Reference clip",
    "Test-Encode": "Test encode",
    "VMAF-Analyse läuft …": "VMAF analysis running …",
    "Encode": "Encode",
    "Analyse": "Analysis",
    "1 VMAF-Analyse": "1 VMAF analysis",
    "Auswahl vergleichen": "Compare selection",
    "Für Vergleich auswählen": "Select for comparison",
    "Kein aktueller Vergleich – oben einen früheren auswählen":
      "No current comparison – pick an earlier one above",
    "Scan läuft …": "Scan running …",
    "Scan gestartet …": "Scan started …",
    "CQ/QP: niedrig = hohe Qualität · hoch = kleinere Datei · leere Felder werden ignoriert":
      "CQ/QP: low = high quality · high = smaller file · empty fields are ignored",
    "Bitrate in kbit/s (z. B. 8000, 6000, 4000, 2000) · leere Felder werden ignoriert":
      "Bitrate in kbit/s (e.g. 8000, 6000, 4000, 2000) · empty fields are ignored",
    "Gesamt-Bitrate": "Total bitrate",
    "Video-Bitrate": "Video bitrate",
    "Bit-Tiefe": "Bit depth",
    "Klasse": "Class",
    "4K / UHD": "4K / UHD",
    "Codec": "Codec",

    // --- Sprachumschalter (bleibt zweisprachig) ---
    "Sprache / Language": "Sprache / Language"
  };

  // Interpolierte Zähler/Status: [Regex auf normalisierten Text, Ersatzfunktion]
  var RULES = [
    [/^(\d+) wartend$/, function (m) { return m[1] + " waiting"; }],
    [/^(\d+) aktiv$/, function (m) { return m[1] + " active"; }],
    [/^(\d+) fertig$/, function (m) { return m[1] + " done"; }],
    [/^(\d+) fehlgeschlagen$/, function (m) { return m[1] + " failed"; }],
    [/^(\d+) Auswahl$/, function (m) { return m[1] + " selection"; }],
    [/^(\d+) Threads$/, function (m) { return m[1] + " threads"; }],
    [/^(\d+) VMAF-Analysen$/, function (m) { return m[1] + " VMAF analyses"; }],
    [/^Auswahl vergleichen \((\d+)\)$/, function (m) { return "Compare selection (" + m[1] + ")"; }],
    [/^Aktuell: (\/.*) \(inkl\. Unterordner\)$/, function (m) { return "Currently: " + m[1] + " (incl. subfolders)"; }],
    [/^Aktuell: \/(.*)$/, function (m) { return "Currently: /" + m[1]; }],
    [/^FFmpeg-Encoder: (.+)$/, function (m) { return "FFmpeg encoder: " + m[1]; }],
    [/^Profil (.+) erkannt/, function (m) { return "Profile " + m[1] + " detected"; }],
    [/^(\d+)\/(\d+) geprüft · (\d+) Treffer · ca\. (.+) einsparbar$/,
      function (m) { return m[1] + "/" + m[2] + " checked · " + m[3] + " matches · approx. " + m[4] + " saveable"; }],
    [/^(\d+)\/(\d+) geprüft · (\d+) Treffer$/,
      function (m) { return m[1] + "/" + m[2] + " checked · " + m[3] + " matches"; }],
    [/^(\d+) Treffer$/, function (m) { return m[1] + " matches"; }],
    [/^(.+) Treffer · ca\. (.+) einsparbar$/, function (m) { return m[1] + " matches · approx. " + m[2] + " saveable"; }],
    [/^Pausiert: (.+)$/, function (m) { return "Paused: " + m[1]; }],
    [/^Analyse-Fehler: (.+)$/, function (m) { return "Analysis error: " + m[1]; }],
    [/^Fehler: (.+)$/, function (m) { return "Error: " + m[1]; }],
    // Reine Status-Badges (Backend-Status)
    [/^wartend$/, function () { return "waiting"; }],
    [/^in arbeit$/, function () { return "processing"; }],
    [/^vmaf-test$/, function () { return "vmaf test"; }],
    [/^auswahl$/, function () { return "selection"; }],
    [/^fertig$/, function () { return "done"; }],
    [/^fehlgeschlagen$/, function () { return "failed"; }],
    [/^abgebrochen$/, function () { return "cancelled"; }]
  ];

  var SKIP_TAGS = { SCRIPT: 1, STYLE: 1, CODE: 1, NOSCRIPT: 1 };
  var ATTRS = ["placeholder", "title"];

  // Typografische Anführungszeichen/Bindestriche vereinheitlichen, damit der
  // Abgleich unabhängig von den konkreten Unicode-Zeichen funktioniert.
  function canon(s) {
    return s
      .replace(/\s+/g, " ")
      .replace(/[\u201E\u201C\u201D\u00AB\u00BB]/g, '"')
      .replace(/[\u2018\u2019]/g, "'")
      .trim();
  }

  // Wörterbuch mit kanonisierten Schlüsseln (einmalig).
  var CDICT = {};
  for (var k in DICT) {
    if (Object.prototype.hasOwnProperty.call(DICT, k)) CDICT[canon(k)] = DICT[k];
  }

  function lookup(norm) {
    var c = canon(norm);
    if (Object.prototype.hasOwnProperty.call(CDICT, c)) return CDICT[c];
    for (var i = 0; i < RULES.length; i++) {
      var m = c.match(RULES[i][0]);
      if (m) return RULES[i][1](m);
    }
    return null;
  }

  function translateText(node) {
    var raw = node.nodeValue;
    if (!raw) return;
    var trimmed = raw.trim();
    if (!trimmed) return;
    var norm = trimmed.replace(/\s+/g, " ");
    var en = lookup(norm);
    if (en != null && en !== trimmed) {
      var idx = raw.indexOf(trimmed);
      node.nodeValue = raw.slice(0, idx) + en + raw.slice(idx + trimmed.length);
    }
  }

  function translateAttrs(el) {
    for (var i = 0; i < ATTRS.length; i++) {
      var a = ATTRS[i];
      if (!el.hasAttribute || !el.hasAttribute(a)) continue;
      var v = el.getAttribute(a);
      if (!v) continue;
      var norm = v.trim().replace(/\s+/g, " ");
      var en = lookup(norm);
      if (en != null && en !== v) el.setAttribute(a, en);
    }
  }

  function walk(node) {
    if (node.nodeType === 3) { translateText(node); return; }
    if (node.nodeType !== 1) return;
    if (SKIP_TAGS[node.tagName]) return;
    if (node.hasAttribute && node.hasAttribute("data-no-i18n")) return;
    translateAttrs(node);
    for (var c = node.firstChild; c; c = c.nextSibling) walk(c);
  }

  function wireSelector(lang) {
    var sel = document.getElementById("lang-select");
    if (!sel) return;
    sel.value = lang;
    sel.addEventListener("change", function () {
      try { localStorage.setItem("lang", sel.value); } catch (e) { /* ignore */ }
      location.reload();
    });
  }

  var lang = "de";
  try { lang = localStorage.getItem("lang") || "de"; } catch (e) { /* ignore */ }
  document.documentElement.setAttribute("lang", lang);
  wireSelector(lang);

  if (lang !== "en") return; // Quellsprache – nichts zu tun

  if (document.body) walk(document.body);

  // Dynamisch nachgeladene Inhalte (Tabellen, Status, Diagnose) mitnehmen.
  var obs = new MutationObserver(function (muts) {
    for (var i = 0; i < muts.length; i++) {
      var mu = muts[i];
      if (mu.type === "characterData") { translateText(mu.target); continue; }
      if (mu.type === "attributes" && mu.target.nodeType === 1) { translateAttrs(mu.target); continue; }
      for (var j = 0; j < mu.addedNodes.length; j++) walk(mu.addedNodes[j]);
    }
  });
  obs.observe(document.body, {
    childList: true, subtree: true, characterData: true,
    attributes: true, attributeFilter: ATTRS
  });

  window.I18N = { t: function (s) { var en = lookup((s || "").trim().replace(/\s+/g, " ")); return en == null ? s : en; }, lang: lang };
})();
