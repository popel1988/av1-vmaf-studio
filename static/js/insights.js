/* Laufzeit-, Prüf- und Analyseinformationen. Keine Änderung der Qualitätsregeln. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const TEXT = {
    runtime: ["Laufende Prozesse & Vorschauen", "Running processes & previews", "Procesos y vistas previas", "Processus et aperçus"],
    runtimeNote: ["CPU kann pro Prozess über 100 % liegen. GPU-Last wird nur systemweit gemessen; eine Zuordnung wird nicht geschätzt.", "Process CPU may exceed 100%. GPU load is measured system-wide only; no per-job attribution is guessed.", "La CPU por proceso puede superar el 100 %. La carga de GPU solo se mide globalmente, sin atribuciones estimadas.", "Le CPU par processus peut dépasser 100 %. La charge GPU est mesurée globalement, sans attribution estimée."],
    empty: ["Keine aktiven Server-Prozesse.", "No active server processes.", "No hay procesos de servidor activos.", "Aucun processus serveur actif."],
    refresh: ["Aktualisieren", "Refresh", "Actualizar", "Actualiser"],
    stop: ["Sitzung beenden", "Stop session", "Detener sesión", "Arrêter la session"],
    stopping: ["Wird beendet", "Stopping", "Deteniendo", "Arrêt en cours"],
    running: ["Läuft", "Running", "En ejecución", "En cours"],
    paused: ["Angehalten", "Paused", "Pausado", "En pause"],
    finished: ["Beendet", "Finished", "Finalizado", "Terminé"],
    failed: ["Fehlgeschlagen", "Failed", "Fallido", "Échec"],
    starting: ["Wird gestartet", "Starting", "Iniciando", "Démarrage"],
    player: ["Player", "Player", "Reproductor", "Lecteur"],
    ab_standard: ["A/B · Standard", "A/B · Standard", "A/B · Estándar", "A/B · Standard"],
    ab_nerd: ["A/B · Nerd", "A/B · Nerd", "A/B · Avanzado", "A/B · Avancé"],
    preview: ["Schnellvorschau", "Quick preview", "Vista previa rápida", "Aperçu rapide"],
    editor: ["Editor-Vorschau", "Editor preview", "Vista previa del editor", "Aperçu de l’éditeur"],
    job: ["Auftrag", "Job", "Trabajo", "Tâche"],
    phase: ["Aktuelle Phase", "Current phase", "Fase actual", "Phase actuelle"],
    requested: ["Angefordert", "Requested", "Solicitado", "Demandé"],
    effective: ["Tatsächlich verwendet", "Actually used", "Realmente utilizado", "Réellement utilisé"],
    parameters: ["Encoding-Details", "Encoding details", "Detalles de codificación", "Détails d’encodage"],
    filters: ["Bildfilter", "Picture filters", "Filtros de imagen", "Filtres d’image"],
    audio: ["Tonspuren", "Audio tracks", "Pistas de audio", "Pistes audio"],
    subtitles: ["Untertitel", "Subtitles", "Subtítulos", "Sous-titres"],
    chapters: ["Kapitel", "Chapters", "Capítulos", "Chapitres"],
    output: ["Ausgabe", "Output", "Salida", "Sortie"],
    source: ["Quelle", "Source", "Origen", "Source"],
    resolution: ["Auflösung", "Resolution", "Resolución", "Résolution"],
    codec: ["Videoformat", "Video format", "Formato de vídeo", "Format vidéo"],
    hdr: ["HDR / Farbraum", "HDR / color space", "HDR / espacio de color", "HDR / espace colorimétrique"],
    size: ["Dateigröße", "File size", "Tamaño del archivo", "Taille du fichier"],
    duration: ["Dauer", "Duration", "Duración", "Durée"],
    integrity: ["Integrität (Stichproben)", "Integrity (samples)", "Integridad (muestras)", "Intégrité (échantillons)"],
    quality: ["Qualitätsprüfung", "Quality check", "Control de calidad", "Contrôle qualité"],
    report: ["Ausgabe-Prüfbericht", "Output verification report", "Informe de verificación de salida", "Rapport de vérification de sortie"],
    reportNote: ["Der Bericht beschreibt die ausgeführten Prüfungen, keine vollständige Fehlerfreiheit des Films.", "This report describes the checks performed, not a guarantee that the entire movie is error-free.", "El informe describe las comprobaciones realizadas, no garantiza que toda la película esté libre de errores.", "Ce rapport décrit les contrôles effectués, sans garantir l’absence d’erreurs sur tout le film."],
    noReport: ["Für diesen Auftrag ist kein gespeicherter Prüfbericht vorhanden.", "No saved verification report for this job.", "No hay un informe guardado para este trabajo.", "Aucun rapport enregistré pour cette tâche."],
    ok: ["Bestanden", "Passed", "Correcto", "Validé"],
    warn: ["Hinweis / Abweichung", "Note / difference", "Aviso / diferencia", "Remarque / écart"],
    fail: ["Nicht bestanden", "Failed", "No superado", "Non validé"],
    not_checked: ["Nicht geprüft", "Not checked", "Sin comprobar", "Non vérifié"],
    planned: ["Geplant", "Planned", "Previsto", "Prévu"],
    measured: ["Ermittelt", "Observed", "Observado", "Constaté"],
    unknown: ["Unbekannt", "Unknown", "Desconocido", "Inconnu"],
    copy: ["Unverändert übernehmen", "Copy unchanged", "Copiar sin cambios", "Copier sans modification"],
    none: ["Keine", "None", "Ninguno", "Aucun"],
    all: ["Alle", "All", "Todas", "Toutes"],
    on: ["An", "On", "Activado", "Activé"],
    off: ["Aus", "Off", "Desactivado", "Désactivé"],
    previewNote: ["Browser-Vorschau, nicht die unveränderte Encodedatei. Vorschau-Encoding und Tonemapping können den Bildeindruck verändern.", "Browser preview, not the unchanged encoded file. Preview encoding and tone mapping can change the appearance.", "Vista previa del navegador, no el archivo codificado sin cambios. La recodificación y el mapeo tonal pueden alterar la imagen.", "Aperçu navigateur, pas le fichier encodé intact. Le réencodage et le tone mapping peuvent modifier l’apparence."],
    directNote: ["Datei unverändert an den Browser; Darstellung und HDR-Wiedergabe hängen vom Browser und Display ab.", "Unchanged file sent to the browser; rendering and HDR playback depend on browser and display.", "Archivo sin cambios enviado al navegador; la imagen y el HDR dependen del navegador y la pantalla.", "Fichier intact envoyé au navigateur ; le rendu et le HDR dépendent du navigateur et de l’écran."],
    fallback: ["Abweichende Verarbeitung / Fallback", "Different processing / fallback", "Procesamiento distinto / alternativa", "Traitement différent / repli"],
    coverage: ["Aussagekraft der Stichproben", "Sample coverage", "Cobertura de muestras", "Couverture des échantillons"],
    coverageNote: ["Die Empfehlung gilt für die untersuchten Stichproben. Der übrige Film wurde hier nicht bewertet. Quellbitrate ist nur ein Hilfsmittel für die Szenenauswahl.", "The recommendation applies to the tested samples. The rest of the movie was not scored here. Source bitrate is only a scene-selection aid.", "La recomendación se aplica a las muestras analizadas. El resto de la película no se ha evaluado aquí. El bitrate original solo ayuda a seleccionar escenas.", "La recommandation porte sur les échantillons étudiés. Le reste du film n’a pas été évalué ici. Le débit source aide seulement à choisir les scènes."],
    scenes: ["Szenen", "Scenes", "Escenas", "Scènes"],
    seconds: ["bewertete Sekunden", "scored seconds", "segundos evaluados", "secondes évaluées"],
    frames: ["bewertete Frames", "scored frames", "fotogramas evaluados", "images évaluées"],
    movie: ["des Films", "of the movie", "de la película", "du film"],
    archive: ["Technischer Analyse-Fingerabdruck", "Analysis technical fingerprint", "Huella técnica del análisis", "Empreinte technique de l’analyse"],
    source_changed: ["Die Quelle wurde seit dieser Analyse verändert. Vor dem Encoding neu prüfen.", "The source has changed since this analysis. Recheck before encoding.", "El origen ha cambiado desde este análisis. Compruébalo antes de codificar.", "La source a changé depuis cette analyse. Revérifiez avant l’encodage."],
    source_missing: ["Die Quelle ist nicht mehr verfügbar.", "The source is no longer available.", "El origen ya no está disponible.", "La source n’est plus disponible."],
    provenance_unknown: ["Technische Vergleichsdaten fehlen teilweise; Quelle oder Verarbeitung sind nicht vollständig verifizierbar.", "Some technical comparison data is missing; source or processing cannot be fully verified.", "Faltan datos técnicos; no se pueden verificar completamente el origen o el procesamiento.", "Certaines données techniques manquent ; source ou traitement partiellement invérifiables."],
    model_changed: ["Anderes VMAF-Modell: Werte nicht unmittelbar vergleichbar.", "Different VMAF model: scores are not directly comparable.", "Modelo VMAF distinto: los valores no son directamente comparables.", "Modèle VMAF différent : les scores ne sont pas directement comparables."],
    toolchain_changed: ["Die Werkzeugversion unterscheidet sich vom gespeicherten Lauf.", "The toolchain version differs from the saved run.", "La versión de herramientas difiere de la ejecución guardada.", "La version des outils diffère de l’exécution enregistrée."],
    fingerprintMatch: ["Quellgröße und Änderungszeit passen zum Archiv. Kein vollständiger Inhalts-Hash.", "Source size and modification time match the archive. Not a full content hash.", "El tamaño y la fecha del origen coinciden. No es un hash completo del contenido.", "Taille et date de la source conformes à l’archive. Pas de hachage intégral du contenu."],
    protect: ["Testclips vor automatischem Aufräumen schützen", "Protect test clips from automatic cleanup", "Proteger clips de la limpieza automática", "Protéger les clips du nettoyage automatique"],
    extraScene: ["Zusätzliche Szene nachprüfen", "Check an additional scene", "Comprobar otra escena", "Vérifier une scène supplémentaire"],
    extraNote: ["Startet einen separaten Vergleich nur für diese Szene mit den gespeicherten Teststufen. Die bisherige Analyse wird nicht verändert oder mit der Zusatzszene vermischt.", "Starts a separate comparison for this scene using the saved test tiers. The previous analysis is neither changed nor merged with this extra scene.", "Inicia una comparación separada para esta escena con los niveles guardados. El análisis anterior no se modifica ni se mezcla con la nueva escena.", "Lance une comparaison séparée sur cette scène avec les niveaux enregistrés. L’analyse précédente n’est ni modifiée ni fusionnée avec cette scène."],
    start: ["Startzeit (Sekunden)", "Start time (seconds)", "Inicio (segundos)", "Début (secondes)"],
    length: ["Cliplänge (Sekunden)", "Clip length (seconds)", "Duración (segundos)", "Durée (secondes)"],
    enqueue: ["Nachprüfung einreihen", "Queue extra check", "Añadir comprobación", "Ajouter la vérification"],
    queued: ["Nachprüfung wurde eingereiht.", "Extra check queued.", "Comprobación añadida.", "Vérification ajoutée."],
    storage: ["Speicherplanung", "Storage planning", "Planificación de espacio", "Prévision de stockage"],
    storageNote: ["Grobe Planung einschließlich temporärer Dateien, keine feste Größenobergrenze. Gleichzeitige Aufträge und andere Programme können zusätzlichen Platz benötigen.", "Rough estimate including temporary files, not a size limit. Concurrent jobs and other programs may need additional space.", "Estimación aproximada con temporales, no un límite fijo. Otros trabajos y programas pueden necesitar más espacio.", "Estimation incluant les fichiers temporaires, pas une limite de taille. D’autres tâches et programmes peuvent nécessiter plus d’espace."],
    free: ["Frei", "Free", "Libre", "Libre"],
    needed: ["Voraussichtlich benötigt", "Estimated need", "Necesidad estimada", "Besoin estimé"],
    insufficient: ["Voraussichtlich zu wenig freier Speicher", "Likely insufficient free space", "Espacio libre probablemente insuficiente", "Espace libre probablement insuffisant"],
    storageUnknown: ["Speicherbedarf oder freier Platz nicht vollständig ermittelbar.", "Storage need or free space could not be fully determined.", "No se ha podido determinar todo el espacio necesario o libre.", "Besoin de stockage ou espace libre partiellement inconnu."],
    retention: ["Testclip-Aufbewahrung", "Test clip retention", "Conservación de clips", "Conservation des clips"],
    retentionNote: ["Optional: Nur alte Testvideos entfernen. Messwerte, Logs und Screenshots bleiben erhalten. Aktive und geschützte Analysen werden übersprungen. 0 Tage schaltet die automatische Bereinigung aus.", "Optional: remove old test videos only. Metrics, logs and screenshots are retained. Active and protected analyses are skipped. 0 days disables automatic cleanup.", "Opcional: eliminar solo vídeos de prueba antiguos. Se conservan métricas, registros y capturas. Se omiten análisis activos y protegidos. 0 días desactiva la limpieza automática.", "Facultatif : supprimer uniquement les anciennes vidéos de test. Mesures, journaux et captures conservés. Analyses actives et protégées ignorées. 0 jour désactive le nettoyage automatique."],
    days: ["Tage", "Days", "Días", "Jours"],
    save: ["Regel speichern", "Save rule", "Guardar regla", "Enregistrer la règle"],
    saved: ["Gespeichert", "Saved", "Guardado", "Enregistré"],
    previewCleanup: ["Bereinigung prüfen", "Preview cleanup", "Previsualizar limpieza", "Prévisualiser le nettoyage"],
    applyCleanup: ["Diese Testclips entfernen", "Remove these test clips", "Eliminar estos clips", "Supprimer ces clips"],
    cleanupConfirm: ["Die angezeigten Testvideos endgültig entfernen? Messwerte und Screenshots bleiben erhalten.", "Permanently remove the listed test videos? Metrics and screenshots are retained.", "¿Eliminar definitivamente los vídeos indicados? Se conservan métricas y capturas.", "Supprimer définitivement les vidéos affichées ? Mesures et captures conservées."],
    nothing: ["Keine passenden alten Testclips.", "No matching old test clips.", "No hay clips antiguos aplicables.", "Aucun ancien clip correspondant."],
    error: ["Fehler beim Laden oder Speichern.", "Failed to load or save.", "Error al cargar o guardar.", "Échec du chargement ou de l’enregistrement."],
    missingMetadata: ["Keine vollständigen Messdaten vorhanden.", "Complete measurement data unavailable.", "No hay datos de medición completos.", "Mesures complètes indisponibles."],
  };
  function t(key) {
    const i = {de:0, en:1, es:2, fr:3}[document.documentElement.lang] || 0;
    return TEXT[key] ? TEXT[key][i] : key;
  }
  function esc(s) { return String(s == null ? "—" : s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
  function bytes(v) { if (v == null) return "—"; let n = Number(v); if (!Number.isFinite(n)) return "—"; const u = ["B","KiB","MiB","GiB","TiB"]; let i = 0; while (Math.abs(n) >= 1024 && i < 4) { n /= 1024; i++; } return `${n.toFixed(1)} ${u[i]}`; }
  function value(v) { return v == null ? "—" : typeof v === "object" ? JSON.stringify(v) : typeof v === "boolean" ? t(v ? "on" : "off") : String(v); }
  function pair(key, val) { return `<div class="insight-pair"><dt>${esc(t(key))}</dt><dd>${esc(value(val))}</dd></div>`; }
  let context = {}, analysisData = null, analysisSeq = 0;
  async function api(url, body, method = "POST") {
    const r = await fetch(url, body === undefined ? undefined : {method,headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    const d = await r.json(); if (!r.ok || d.error) throw new Error(d.error || t("error")); return d;
  }
  function effectiveText(s) {
    const e = s.effective || s.processing || {}, tm = e.tonemap || {}, command = e.command || {};
    const parts = [s.kind === "job" ? t("job") : s.mode === "direct" ? "Direct-Play" : s.mode === "stream" ? e.video_mode === "none" ? "WebVTT" : "Video-Copy · MP4" : s.encoder === "copy" || e.video_mode === "copy" ? "HLS · Video-Copy" : "HLS · " + t("preview"),
      e.codec || s.codec, command.encoder || e.encoder || s.encoder, s.height ? s.height + "p" : "", tm.active ? `HDR → SDR · ${tm.engine || "cpu"}/${tm.algo || "hable"}` : "", s.kind === "job" && s.phase ? phaseLabel(s.phase) : "",
      ...Object.entries(e.picture || {}).filter(([,v]) => v && v !== "off").map(([k,v]) => `${k}: ${v}`)];
    return parts.filter(Boolean).join(" · ");
  }
  function previewHtml(s) {
    if (!s) return "";
    return `<div class="preview-provenance" data-no-i18n><strong>${esc(effectiveText(s))}</strong><div>${esc(t(s.mode === "direct" ? "directNote" : "previewNote"))}</div>`
      + (s.fallback || (s.effective || {}).fallback ? `<div class="warn">${esc(t("fallback"))}: ${esc((s.fallback_reasons || (s.effective || {}).fallback_reasons || []).join(" · "))}</div>` : "") + `</div>`;
  }
  function renderRuntime(data) {
    const root = $("runtime-list"); if (!root) return;
    const rows = data.sessions || data.items || [];
    const open = new Set([...root.querySelectorAll("[data-runtime-details][open]")].map(el => el.dataset.runtimeDetails));
    const html = rows.length ? rows.map(s => {
      const p = s.resources || s;
      return `<article class="runtime-row"><div><strong>${esc(t(s.owner || "job"))}${s.label ? " · " + esc(s.label) : ""}</strong>`
        + `<div class="muted">${esc(s.source_name || s.title || s.path || (s.source && s.source.path) || s.id)}</div>`
        + `<div>${esc(effectiveText(s))}</div><details data-runtime-details="${esc(s.id)}" ${open.has(s.id) ? "open" : ""}><summary>${esc(t("parameters"))}</summary><div>${esc(t("requested"))}</div><pre class="technical-json">${esc(JSON.stringify(s.requested || (s.effective || {}).requested || {},null,2))}</pre><div>${esc(t("effective"))}</div><pre class="technical-json">${esc(JSON.stringify(s.effective || {},null,2))}</pre></details><small>${esc(t(s.state || "running"))} · PID ${esc(s.pid || p.pid || "—")} · CPU ${p.cpu_percent == null ? "—" : esc(p.cpu_percent + "%")} · RAM ${bytes(p.rss_bytes == null ? p.memory_bytes : p.rss_bytes)}</small></div>`
        + (s.kind === "job" ? `<button class="btn btn-ghost btn-sm" data-runtime-job="${esc(s.id)}">${esc(t("parameters"))}</button>`
          : s.id && s.can_stop !== false && s.stoppable !== false ? `<button class="btn btn-ghost btn-sm" data-runtime-stop="${esc(s.id)}" ${s.state === "stopping" ? "disabled" : ""}>${esc(t(s.state === "stopping" ? "stopping" : "stop"))}</button>` : "") + `</article>`;
    }).join("") : `<p class="muted">${esc(t("empty"))}</p>`;
    // Keine fokussierten Stop-/Detailknöpfe bei jedem WS-Tick ersetzen.
    if (root.contains(document.activeElement)) return;
    root.innerHTML = html;
  }
  async function refreshRuntime() {
    try { renderRuntime(await api("/api/runtime")); } catch (e) { const el = $("runtime-list"); if (el) el.textContent = e.message; }
  }
  const PHASES = {
    "source.probe":["Quelle prüfen","Probe source","Analizar origen","Analyser la source"],
    "source.crop":["Bildbeschnitt erkennen","Detect crop","Detectar recorte","Détecter le recadrage"],
    "analysis.bitrate":["Szenen nach Bitrate suchen","Select bitrate scenes","Seleccionar escenas por bitrate","Choisir les scènes par débit"],
    "hdr.dv_preflight":["Dolby-Vision-Vorabtest","Dolby Vision preflight","Comprobación Dolby Vision","Précontrôle Dolby Vision"],
    "encode.pass1":["Zwei-Pass · Analyse","Two-pass · analysis","Dos pasadas · análisis","Deux passes · analyse"],
    "encode.pass2":["Zwei-Pass · Encoding","Two-pass · encoding","Dos pasadas · codificación","Deux passes · encodage"],
    "encode.main":["Encoding","Encoding","Codificación","Encodage"],
    "verify.vmaf":["Qualität der Ausgabe messen","Measure output quality","Medir calidad de salida","Mesurer la qualité de sortie"],
    "verify.integrity":["Stichproben decodieren und Dauer prüfen","Decode samples and check duration","Decodificar muestras y comprobar duración","Décoder les échantillons et vérifier la durée"],
    "verify.report":["Ausgabe-Eigenschaften abgleichen","Compare output properties","Comparar propiedades de salida","Comparer les propriétés de sortie"],
    "verify.caps":["Ausgabe-Grenzen prüfen","Check output limits","Comprobar límites de salida","Vérifier les limites de sortie"],
    "output.statistics":["Dateigröße und Ersparnis ermitteln","Read size and savings","Calcular tamaño y ahorro","Calculer taille et gain"],
    "output.postprocess":["Original sicher nachbehandeln","Safely process original","Procesar original de forma segura","Traiter l’original en sécurité"],
    "output.nfo":["NFO-Begleitdatei übernehmen","Copy NFO sidecar","Copiar archivo NFO","Copier le fichier NFO"],
    "hdr.dolby_vision":["Dolby Vision einfügen","Inject Dolby Vision","Insertar Dolby Vision","Injecter Dolby Vision"],
    "hdr.hdr10_plus":["HDR10+ einfügen","Inject HDR10+","Insertar HDR10+","Injecter HDR10+"],
    "remux.audio":["Ton optimieren / Video kopieren","Optimize audio / copy video","Optimizar audio / copiar vídeo","Optimiser l’audio / copier la vidéo"],
    "chunk.setup":["Segmentiertes Encoding vorbereiten","Prepare segmented encoding","Preparar codificación por segmentos","Préparer l’encodage segmenté"],
    "output.split":["Ausgaben aufteilen","Split outputs","Dividir salidas","Diviser les sorties"],
    "analysis.reference":["VMAF-Referenz vorbereiten","Prepare VMAF reference","Preparar referencia VMAF","Préparer la référence VMAF"],
    "analysis.candidate":["Teststufe encodieren und messen","Encode and score test tier","Codificar y evaluar nivel","Encoder et mesurer le niveau"],
    "chunk.encode":["Segment encodieren","Encode segment","Codificar segmento","Encoder un segment"],
    "chunk.analysis":["Segment-Analyse","Segment analysis","Análisis de segmentos","Analyse des segments"],
    "chunk.concat":["Videosegmente zusammenführen","Join video segments","Unir segmentos de vídeo","Assembler les segments vidéo"],
    "chunk.mux":["Ton und Untertitel einfügen","Mux audio and subtitles","Insertar audio y subtítulos","Intégrer audio et sous-titres"],
    "remux.edit":["Container und Spuren bearbeiten","Edit container and tracks","Editar contenedor y pistas","Modifier conteneur et pistes"],
    "remux.concat":["Dateien zusammenführen","Concatenate files","Unir archivos","Concaténer les fichiers"],
    "output.cut":["Ausschnitt schreiben","Write cut","Escribir recorte","Écrire l’extrait"]
  };
  function localized(list) { return list[({de:0,en:1,es:2,fr:3}[document.documentElement.lang] || 0)]; }
  function phaseLabel(phase) {
    if (!phase) return "—";
    const label = PHASES[phase.key] ? localized(PHASES[phase.key]) : (phase.name || phase.key || value(phase));
    const c = phase.context || {};
    const scene = /Szene\s+(\d+)/.exec(phase.name || "");
    const tier = phase.key === "analysis.candidate" ? " · " + (phase.name || "").replace("Zwei-Pass", localized(["Zwei-Pass","two-pass","dos pasadas","deux passes"])).replace("Zwischenwert", localized(["Zwischenwert","intermediate value","valor intermedio","valeur intermédiaire"])) : "";
    return label + tier + (c.scene != null ? ` · ${t("scenes")} ${Number(c.scene) + 1}` : scene ? ` · ${t("scenes")} ${scene[1]}` : "") + (c.chunk != null ? ` · ${c.chunk}/${c.chunks || "?"}` : "") + (c.label ? ` · ${c.label}` : "");
  }
  function queueDetailsHtml(it) {
    const s = it.settings || {}, p = it.progress || {}, detail = it.encoding_details || it.details || it.encoding || {};
    const filters = [s.tonemap ? `${s.tonemap_engine || "cpu"}/${s.tonemap_algo || "hable"}` : "", it.crop ? `Crop ${it.crop}` : "",
      ...["deband","denoise","sharpen","grain","deinterlace"].filter(k => s[k] && s[k] !== "off").map(k => `${k}: ${s[k]}`)].filter(Boolean).join(" · ") || t("off");
    const rate = s.rate_mode === "cq" || !s.rate_mode ? `CQ/CRF ${s.quality}` : `${s.rate_mode.toUpperCase()} ${s.quality} kbit/s`;
    const actual = detail.command || detail.effective || detail.current || {};
    const actualRate = Object.entries(actual.rate_control || {}).filter(([,v]) => v != null).map(([k,v]) => `${k}=${v}`).join(" · ");
    const options = actual.options || {};
    const native = ["-bf","-b_ref_mode","-spatial-aq","-temporal-aq","-aq-strength","-rc-lookahead","-tune","-pix_fmt","-g","-svtav1-params","-x265-params","-x264-params"].filter(k => options[k]).map(k => `${k} ${options[k].join(" / ")}`).join(" · ");
    const fields = [pair("requested", `${s.platform || "cpu"} · ${(s.codec || "").toUpperCase()} · ${rate} · ${s.encoder_speed || "balanced"}`),
      pair("phase", it.phase ? phaseLabel(it.phase) : it.message || it.status),
      pair("resolution", s.target_height ? `${s.target_height}p` : t("source")), pair("filters", filters),
      pair("audio", s.audio_mode === "none" ? t("none") : `${s.audio_mode === "copy" ? t("copy") : `${s.audio_codec} ${s.audio_bitrate} kbit/s`} · ${s.audio_tracks && s.audio_tracks.length ? s.audio_tracks.join(", ") : t("all")}`),
      pair("subtitles", s.keep_subtitles ? t("on") : t("off")), pair("chapters", s.keep_chapters ? t("on") : t("off")),
      pair("output", it.output_path || "—")];
    if (actual.encoder) fields.unshift(pair("effective", [actual.encoder, actual.backend, actual.preset != null ? `preset=${actual.preset}` : "", actual.pass ? `pass=${actual.pass}` : "", actual.multipass ? `multipass=${actual.multipass}` : ""].filter(Boolean).join(" · ")));
    if (actualRate) fields.push(pair("parameters", actualRate));
    if (native) fields.push(pair("parameters", native));
    if (actual.filters) fields.push(pair("effective", actual.filters));
    if (actual.hardware) fields.push(pair("effective", JSON.stringify(actual.hardware)));
    if (p.fps || p.speed || p.bitrate || p.frame) fields.push(pair("measured", [p.fps ? `${p.fps} FPS` : "", p.speed ? `${p.speed}×` : "", p.bitrate || "", p.frame ? `${p.frame} frames` : ""].filter(Boolean).join(" · ")));
    return `<details class="encoding-details" data-no-i18n><summary>${esc(t("parameters"))}</summary><dl class="insight-grid">${fields.join("")}</dl>`
      + (Object.keys(actual).length ? `<details><summary>${esc(t("effective"))}</summary><pre class="technical-json">${esc(JSON.stringify(actual,null,2))}</pre></details>` : "") + `</details>`;
  }
  const CHECK_KEYS = {video_codec:"codec",video_format:"codec",codec:"codec",resolution:"resolution",hdr:"hdr",hdr_state:"hdr",color:"hdr",audio:"audio",audio_tracks:"audio",subtitles:"subtitles",subtitle_tracks:"subtitles",chapters:"chapters",size:"size",output_size:"size",duration:"duration",integrity:"integrity",quality:"quality",vmaf:"quality"};
  const CHECK_TEXT = {
    "output.probe":["Ausgabe lesbar (ffprobe)","Output readable (ffprobe)","Salida legible (ffprobe)","Sortie lisible (ffprobe)"],
    "video.bit_depth":["Farbtiefe","Bit depth","Profundidad de bits","Profondeur de couleur"],
    "video.color_transfer":["Transferfunktion","Transfer function","Función de transferencia","Fonction de transfert"],
    "video.color_primaries":["Farbprimärwerte","Color primaries","Colores primarios","Primaires de couleur"],
    "video.color_space":["Farbmatrix","Color matrix","Matriz de color","Matrice de couleur"],
    "video.color_range":["Wertebereich","Color range","Rango de color","Plage de couleur"],
    "chapters.probe":["Kapitel auslesen","Read chapters","Leer capítulos","Lire les chapitres"],
    "chapters.count":["Kapitelanzahl","Chapter count","Número de capítulos","Nombre de chapitres"],
    "chapters.timeline":["Kapitelzeiten und Titel","Chapter times and titles","Tiempos y títulos de capítulos","Temps et titres des chapitres"],
    "output.size_estimate":["Größenprognose vs. Ergebnis","Size estimate vs. result","Estimación frente a tamaño real","Taille estimée et réelle"],
    "output.size_target":["Größenziel","Size target","Tamaño objetivo","Taille cible"],
    "output.postprocess":["Original-Nachbehandlung","Original post-processing","Procesamiento del original","Traitement de l’original"],
    "output.caps":["Größen-/Bitratengrenzen","Size / bitrate limits","Límites de tamaño / bitrate","Limites taille / débit"],
    "operation.warning":["Auftragshinweis","Job warning","Aviso del trabajo","Avertissement de tâche"],
    "report.errors":["Fehler bei der Prüfung","Audit errors","Errores de verificación","Erreurs de vérification"],
    "decode.full":["Vollständiger Decode des Films","Full-movie decode","Decodificación completa","Décodage intégral du film"],
    "audio.payload":["Vollständiger Inhalt aller Tonspuren","Full content of all audio tracks","Contenido completo de todas las pistas de audio","Contenu intégral des pistes audio"],
    "subtitles.payload":["Inhalt aller Untertitel","All subtitle content","Contenido de todos los subtítulos","Contenu de tous les sous-titres"],
    "hdr.dynamic_payload":["Dynamische HDR-Nutzdaten","Dynamic HDR payload","Datos HDR dinámicos","Données HDR dynamiques"],
    "attachments.payload":["Inhalt der Anhänge","Attachment contents","Contenido de adjuntos","Contenu des pièces jointes"],
    count:["Anzahl","Count","Cantidad","Nombre"],codec:["Codec","Codec","Códec","Codec"],
    language:["Sprache","Language","Idioma","Langue"],title:["Titel","Title","Título","Titre"],
    default:["Standard-Markierung","Default flag","Marca predeterminada","Drapeau par défaut"],
    forced:["Forced-Markierung","Forced flag","Marca forzada","Drapeau forcé"],
    channels:["Kanäle","Channels","Canales","Canaux"]
  };
  const DETAIL_TEXT = {
    original_file_postprocessing:["Nachbehandlung des Originals nach den Schutzprüfungen.","Original post-processing after safety checks.","Procesamiento del original tras las comprobaciones de seguridad.","Traitement de l’original après les contrôles de sécurité."],
    metadata_only:["Metadatenvergleich; kein vollständiger Inhaltstest.","Metadata comparison; not a full content test.","Comparación de metadatos; no prueba todo el contenido.","Comparaison de métadonnées ; pas un test intégral du contenu."],
    output_metadata_probe:["Container und Stream-Metadaten ausgelesen.","Container and stream metadata read.","Metadatos del contenedor y pistas leídos.","Métadonnées du conteneur et des pistes lues."],
    duration_tolerance_2s_or_1pct:["Toleranz: 2 Sekunden oder 1 % (größerer Wert).","Tolerance: 2 seconds or 1% (whichever is greater).","Tolerancia: 2 segundos o 1 % (el mayor).","Tolérance : 2 secondes ou 1 % (le plus grand)."],
    sample_decode_and_duration_not_full_decode:["Decode-Stichproben und Dauerprüfung, kein Voll-Decode.","Sample decode and duration check, not full decode.","Decodificación de muestras y duración, no completa.","Décodage d’échantillons et durée, pas intégral."],
    sample_vmaf_not_full_video:["VMAF der geprüften Stichproben, nicht des gesamten Films.","VMAF of checked samples, not the whole movie.","VMAF de las muestras, no de toda la película.","VMAF des échantillons, pas de tout le film."],
    estimate_not_guarantee:["Prognose, keine garantierte Dateigröße.","Estimate, not a guaranteed file size.","Estimación, no tamaño garantizado.","Estimation, pas une taille garantie."],
    size_target_not_cap:["Zielgröße, keine harte Obergrenze.","Target size, not a hard cap.","Tamaño objetivo, no límite estricto.","Taille cible, pas une limite stricte."],
    not_performed:["Diese Prüfung wurde nicht durchgeführt.","This check was not performed.","Esta comprobación no se realizó.","Ce contrôle n’a pas été effectué."],
    chapter_metadata_probe:["Kapitel-Metadaten ausgelesen.","Chapter metadata read.","Metadatos de capítulos leídos.","Métadonnées des chapitres lues."],
    chapter_metadata_tolerance_100ms:["Kapitelzeiten mit 100 ms Toleranz und Titel verglichen.","Chapter times compared with 100 ms tolerance, and titles checked.","Tiempos comparados con tolerancia de 100 ms y títulos comprobados.","Temps comparés avec une tolérance de 100 ms et titres vérifiés."],
    chapter_timeline_unavailable:["Kein verlässlicher Soll-Ist-Vergleich der Kapitel möglich.","Reliable planned/actual chapter comparison unavailable.","No es posible comparar capítulos de forma fiable.","Comparaison fiable des chapitres indisponible."],
    output_probe_unavailable:["Ausgabe-Metadaten nicht verfügbar.","Output metadata unavailable.","Metadatos de salida no disponibles.","Métadonnées de sortie indisponibles."],
    existing_cap_check:["Ergebnis der vorhandenen Größen-/Bitratenprüfung.","Result of the existing size/bitrate check.","Resultado de la comprobación de tamaño/bitrate.","Résultat du contrôle taille/débit existant."],
    existing_job_warning:["Hinweis des Verarbeitungsauftrags.","Warning from the processing job.","Aviso del trabajo de procesamiento.","Avertissement de la tâche de traitement."],
    audit_errors:["Die Prüfung konnte nicht vollständig ausgeführt werden.","The audit could not be completed.","La verificación no pudo completarse.","La vérification n’a pas pu être achevée."]
  };
  function checkLabel(key) {
    if (CHECK_TEXT[key]) return localized(CHECK_TEXT[key]);
    if (key === "video.codec") return t("codec");
    if (key === "video.resolution") return t("resolution");
    if (key === "video.dolby_vision") return "Dolby Vision";
    if (key === "video.hdr10_plus") return "HDR10+";
    if (key === "output.duration") return t("duration");
    if (key === "integrity.samples") return t("integrity");
    if (key === "quality.vmaf") return "VMAF · " + t("quality");
    const track = /^(audio|subtitles)\.(?:(\d+)\.)?(\w+)$/.exec(key);
    if (track) return `${t(track[1])}${track[2] == null ? "" : " " + (Number(track[2])+1)} · ${CHECK_TEXT[track[3]] ? localized(CHECK_TEXT[track[3]]) : track[3]}`;
    return t(CHECK_KEYS[key] || key || "unknown");
  }
  function reportHtml(report) {
    if (!report) return `<p class="muted" data-no-i18n>${esc(t("noReport"))}</p>`;
    const checks = report.checks || [];
    if (!checks.length) return `<p class="muted" data-no-i18n>${esc(t("noReport"))}</p>`;
    return `<section class="output-report" data-no-i18n><h4>${esc(t("report"))}</h4><p class="hint">${esc(t("reportNote"))}</p>`
      + checks.map(c => `<div class="report-check"><strong>${esc(checkLabel(c.key))}</strong><span class="metric-pill ${c.status === "ok" ? "good" : c.status === "fail" ? "bad" : c.status === "warn" ? "warn" : ""}">${esc(t(c.status || "not_checked"))}</span>`
        + `<div>${esc(t("planned"))}: ${esc(value(c.expected))}</div><div>${esc(t("measured"))}: ${esc(value(c.actual))}</div>`
        + (c.output_path ? `<div class="muted">${esc(c.output_path)}</div>` : "")
        + (c.detail ? `<small class="muted">${esc(DETAIL_TEXT[c.detail] ? localized(DETAIL_TEXT[c.detail]) : value(c.detail))}</small>` : "")
        + (c.error ? `<div class="bad">${esc(c.error)}</div>` : "") + `</div>`).join("") + `</section>`;
  }
  function storageHtml(storage) {
    if (!storage) return "";
    const volumes = storage.volumes || storage.filesystems || [];
    return `<section class="storage-plan" data-no-i18n><h4>${esc(t("storage"))}</h4><p class="hint">${esc(t("storageNote"))}</p>`
      + volumes.map(v => `<div class="report-check"><strong>${esc(v.path || v.mount || v.device || "—")}</strong><div>${esc(t("needed"))}: ${bytes(v.required_bytes == null ? v.estimated_bytes : v.required_bytes)} · ${esc(t("free"))}: ${bytes(v.free_bytes)}</div>`
        + (v.sufficient === false || v.status === "insufficient" ? `<div class="bad">${esc(t("insufficient"))}</div>` : "") + `</div>`).join("")
      + (storage.unknown || volumes.some(v => v.status === "unknown") || !volumes.length ? `<div class="warn">${esc(t("storageUnknown"))}</div>` : "") + `</section>`;
  }
  function coverageHtml(a, data) {
    const full = (data && data.coverage) || a.coverage || {};
    const cov = full.results && full.results.length ? full.results.find(r => (a.results[r.result_index] || {}).recommended) || full.results[0] : full;
    const starts = a.sample_starts || [];
    const seconds = cov.scored_seconds == null ? cov.seconds : cov.scored_seconds;
    const frames = cov.scored_frames == null ? cov.frame_count : cov.scored_frames;
    const parts = [`${cov.scenes ? cov.scenes.length : cov.scene_count == null ? starts.length : cov.scene_count} ${t("scenes")}`];
    if (seconds != null) parts.push(`${Number(seconds).toFixed(1)} ${t("seconds")}`);
    if (frames != null) parts.push(`${frames} ${t("frames")}`);
    if (cov.film_fraction != null) parts.push(`${(Number(cov.film_fraction) * 100).toFixed(2)} % ${t("movie")}`);
    else if (cov.movie_percent != null || cov.coverage_percent != null) parts.push(`${Number(cov.movie_percent == null ? cov.coverage_percent : cov.movie_percent).toFixed(2)} % ${t("movie")}`);
    return `<strong>${esc(t("coverage"))}</strong><div>${esc(parts.join(" · "))}</div><p class="hint">${esc(t("coverageNote"))}</p>`;
  }
  async function analysis(a, archived) {
    const el = $("analysis-insights"); if (!el) return;
    const seq = ++analysisSeq;
    if (!a || !a.results || !a.results.length) { el.hidden = true; return; }
    el.hidden = false;
    el.innerHTML = coverageHtml(a, archived);
    const sid = (archived && archived.session) || a.session || context.getState && context.getState().vmafSession;
    let data = archived;
    try { if (!data && sid) data = await api(`/api/vmaf/session/${encodeURIComponent(sid)}`); } catch (_) { /* Live-Analyse kann noch ungespeichert sein. */ }
    if (seq !== analysisSeq) return;
    analysisData = data;
    el.innerHTML = coverageHtml(a, data);
    if (!data) return;
    const prov = data.provenance || (data.analysis || {}).provenance || {};
    const verification = data.archive_context || data.verification || data.provenance_check || {};
    const warnings = [...new Map((data.warnings || verification.warnings || []).map(w => [typeof w === "string" ? w : w.code, w])).values()];
    const warningText = code => {
      const key = typeof code === "string" ? code : code.code || code.key;
      return t({source_unknown:"provenance_unknown",model_unknown:"provenance_unknown",model_missing:"model_changed",runtime_changed:"toolchain_changed",runtime_unknown:"provenance_unknown"}[key] || key || "provenance_unknown");
    };
    el.innerHTML += `<div class="analysis-warnings">${[...new Set(warnings.map(warningText))].map(text => `<p class="warn">${esc(text)}</p>`).join("")}</div>`
      + ((verification.source || {}).status === "unchanged" ? `<p class="muted">${esc(t("fingerprintMatch"))}</p>` : "")
      + `<details><summary>${esc(t("archive"))}</summary><pre class="technical-json">${esc(JSON.stringify(prov,null,2))}</pre></details>`
      + `<label class="check"><input type="checkbox" id="analysis-protected" ${data.protected ? "checked" : ""}>${esc(t("protect"))}</label>`
      + (data.source_available !== false && data.source_path && (verification.source || {}).status !== "changed" && data.params && data.params.test_values ? `<details class="extra-scene"><summary>${esc(t("extraScene"))}</summary><p class="hint">${esc(t("extraNote"))}</p><form id="extra-scene-form"><label>${esc(t("start"))}<input id="extra-scene-start" type="number" min="0" step="0.1" value="0" required></label><label>${esc(t("length"))}<input id="extra-scene-length" type="number" min="3" max="120" value="15" required></label><button class="btn btn-ghost btn-sm">${esc(t("enqueue"))}</button><span id="extra-scene-status" role="status"></span></form></details>` : "");
    const protect = $("analysis-protected");
    protect.addEventListener("change", async () => {
      protect.disabled = true;
      try { await api(`/api/vmaf/session/${encodeURIComponent(sid)}/protection`,{protected:protect.checked}); }
      catch (e) { protect.checked = !protect.checked; alert(e.message); }
      finally { protect.disabled = false; }
    });
    const form = $("extra-scene-form");
    if (form) form.addEventListener("submit", async e => {
      e.preventDefault(); const b = form.querySelector("button"), status = $("extra-scene-status"); b.disabled = true;
      try { await api("/api/vmaf/extra-scene",{session:sid,start:Number($("extra-scene-start").value),seconds:Number($("extra-scene-length").value)}); status.textContent = t("queued"); }
      catch (err) { status.textContent = err.message; } finally { b.disabled = false; }
    });
  }
  async function cleanupPreview() {
    const el = $("retention-preview"), b = $("retention-preview-btn"); if (!el) return; b.disabled = true;
    try {
      const days = Number($("retention-days").value);
      if (!(days >= 1)) { el.textContent = t("off"); return; }
      const d = await api("/api/data/retention/preview", {days});
      const files = d.files || d.candidates || [];
      el.innerHTML = files.length ? `<p>${files.length} · ${bytes(d.bytes == null ? d.total_bytes : d.bytes)}</p><ul class="cleanup-files">${files.map(f => `<li>${esc(f.path || f.file || f)}</li>`).join("")}</ul><button class="btn btn-ghost btn-sm" id="retention-apply">${esc(t("applyCleanup"))}</button>` : `<p>${esc(t("nothing"))}</p>`;
      const apply = $("retention-apply");
      if (apply) apply.addEventListener("click", async () => {
        if (Number($("retention-days").value) !== days) { apply.disabled = true; return; }
        if (!confirm(t("cleanupConfirm"))) return; apply.disabled = true;
        try { await api("/api/data/retention/apply", {days,token:d.token}); await cleanupPreview(); }
        catch(e) { el.textContent = e.message; }
      });
    } catch(e) { el.textContent = e.message; } finally { b.disabled = false; }
  }
  function init() {
    const head = $("runtime-heading"); if (head) head.textContent = t("runtime");
    const note = $("runtime-note"); if (note) note.textContent = t("runtimeNote");
    const refresh = $("runtime-refresh"); if (refresh) { refresh.textContent = t("refresh"); refresh.addEventListener("click",refreshRuntime); }
    const list = $("runtime-list"); if (list) list.addEventListener("click",async e => {
      const stop = e.target.closest("[data-runtime-stop]"), job = e.target.closest("[data-runtime-job]");
      if (job && context.openJob) context.openJob(job.dataset.runtimeJob);
      if (!stop) return; stop.disabled = true; stop.textContent = t("stopping");
      try { const r = await fetch(`/api/player/session/${encodeURIComponent(stop.dataset.runtimeStop)}`,{method:"DELETE"}); if(!r.ok) throw new Error(t("error")); await refreshRuntime(); }
      catch(err) { alert(err.message); stop.disabled = false; }
    });
    const retentionHead = $("retention-heading"); if (retentionHead) retentionHead.textContent = t("retention");
    const retention = $("retention-settings");
    if (retention) {
      retention.innerHTML = `<h4>${esc(t("retention"))}</h4><p class="hint">${esc(t("retentionNote"))}</p><div class="lib-actions"><label>${esc(t("days"))}<input id="retention-days" type="number" min="0" max="3650" step="1" value="0"></label><button class="btn btn-ghost btn-sm" id="retention-save">${esc(t("save"))}</button><button class="btn btn-ghost btn-sm" id="retention-preview-btn">${esc(t("previewCleanup"))}</button><span id="retention-status" role="status"></span></div><div id="retention-preview"></div>`;
      api("/api/settings").then(d => { $("retention-days").value = d.vmaf_clip_retention_days || 0; }).catch(() => {});
      $("retention-save").addEventListener("click",async () => {
        const b = $("retention-save"); b.disabled = true;
        try { const d = await api("/api/settings",{vmaf_clip_retention_days:Number($("retention-days").value)}); $("retention-days").value = d.vmaf_clip_retention_days; $("retention-status").textContent = t("saved"); }
        catch(e) { $("retention-status").textContent = e.message; } finally { b.disabled = false; }
      });
      $("retention-days").addEventListener("input", () => { $("retention-preview").innerHTML = ""; });
      $("retention-preview-btn").addEventListener("click",cleanupPreview);
    }
    refreshRuntime();
  }
  window.StudioInsights = {CHECK_TEXT, DETAIL_TEXT, PHASES, connect:c => {context=c;}, init, renderRuntime, refreshRuntime, queueDetailsHtml, reportHtml, storageHtml, analysis, previewHtml, phaseLabel, t, esc, TEXT};
  document.addEventListener("DOMContentLoaded",init);
})();
