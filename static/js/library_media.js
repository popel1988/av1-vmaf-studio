/* Media catalogue: exact file paths are the unit of every manual decision. */
(() => {
  "use strict";
  const PAGE = 24;
  const CATALOG = "/api/library/catalog";
  const t = text => window.I18N ? window.I18N.t(text) : text;
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));
  const literal = value => `<span data-no-i18n>${esc(value)}</span>`;
  const keyOf = (...parts) => JSON.stringify(parts);
  const attr = (action, key = "") => `data-lm="${action}" data-key="${esc(key)}"`;
  const button = (action, text, key = "", attrs = "") => `<button type="button" class="btn btn-sm" ${attr(action, key)} ${attrs}>${esc(text)}</button>`;
  const field = (action, label, value, key = "", type = "text") => `<label class="lm-field"><span>${esc(label)}</span><input type="${type}" ${attr(action, key)} value="${esc(value)}" autocomplete="off"></label>`;
  const numberLabel = (label, n) => `${label} (${n})`;
  const kindNames = () => ({film:t("Film"), doku:t("Doku"), serie:t("Serie"), sendung:t("Sendung"), video:t("Video")});
  const own = (object, key) => object && Object.prototype.hasOwnProperty.call(object, key) ? object[key] : null;
  const compare = (a, b) => a.localeCompare(b, undefined, {numeric:true, sensitivity:"base"});
  const pageCount = count => Math.max(1, Math.ceil(count / PAGE));
  const ordinal = value => value !== null && value !== undefined && value !== "" && Number.isInteger(Number(value)) && Number(value) >= 0 ? Number(value) : null;
  let active;

  function seriesScope(path, m) {
    if (m.kind !== "serie" || !String(m.show || "").trim()) return "";
    const parts = path.replace(/\\/g, "/").split("/");
    parts.pop();
    // Only strip a directory which unambiguously denotes a season. Never use
    // the display label or a catch-all "unknown series" as a shared identity.
    if (/^(?:(?:season|staffel)[ ._-]*|s)\d{1,3}$/i.test(parts.at(-1) || "")) parts.pop();
    return keyOf(m.show, m.show_year ?? m.year ?? "", parts.join("/"));
  }

  function indexRows(rows, catalog) {
    const entries = new Map();
    for (const row of rows) {
      const path = row?.path;
      if (typeof path !== "string" || !path || entries.has(path)) continue;
      const m = row.media || {};
      const decision = own(catalog.decisions, path) || {};
      const title = decision.title || m.title || row.name || path.split(/[/\\]/).pop();
      const show = seriesScope(path, m);
      const season = ordinal(m.season), episode = ordinal(m.episode);
      let key = keyOf("file", path);
      if (decision.status === "version" && decision.group) key = keyOf("manual", decision.group);
      else if (!decision.status && m.identity) {
        key = m.kind === "serie" ? keyOf("episode", season, episode, m.identity) : keyOf("identity", m.identity);
      }
      entries.set(path, {path, row, m, decision, title, key, show, season, episode,
        collection:own(catalog.assignments, path)?.collection_id || "",
        search:[title, m.show, m.year, path].filter(Boolean).join("\n").toLocaleLowerCase()});
    }
    return entries;
  }

  function project(s) {
    const works = new Map(), shows = new Map(), top = [], matching = new Set();
    const query = s.search.trim().toLocaleLowerCase();
    for (const file of s.entries.values()) {
      if ((s.collection && file.collection !== s.collection) || (s.kind && file.m.kind !== s.kind) || (query && !file.search.includes(query))) continue;
      matching.add(file.path);
      if (!works.has(file.key)) works.set(file.key, {type:"work", key:file.key, title:file.title, files:[]});
      works.get(file.key).files.push(file);
    }
    for (const work of works.values()) {
      const file = work.files[0];
      work.cover = work.files.find(f => f.m.cover === true);
      if (!file.show || work.files.some(f => f.show !== file.show || f.season !== file.season)) { top.push(work); continue; }
      if (!shows.has(file.show)) {
        const show = {type:"show", key:file.show, title:file.m.show, file, cover:null, seasons:new Map()};
        shows.set(file.show, show); top.push(show);
      }
      const show = shows.get(file.show), season = String(file.season ?? "");
      show.cover ||= work.cover;
      if (!show.seasons.has(season)) show.seasons.set(season, {type:"season", key:season, works:[]});
      show.seasons.get(season).works.push(work);
    }
    const byTitle = (a, b) => compare(a.title, b.title);
    top.sort(byTitle);
    for (const show of shows.values()) for (const season of show.seasons.values()) {
      season.works.sort((a,b) => (a.files[0].episode ?? Infinity) - (b.files[0].episode ?? Infinity) || byTitle(a,b));
    }
    for (const path of s.selected) if (!matching.has(path)) s.selected.delete(path);
    s.tree = {top, shows};
  }

  function poster(file, title) {
    // No remote poster URLs or path-derived filesystem URLs are accepted.
    return `<div class="lm-poster" aria-hidden="true"><span data-no-i18n>${esc(Array.from(String(title).trim())[0] || "—")}</span>${file ? `<img alt="" loading="lazy" decoding="async" src="/api/library/media-cover?path=${encodeURIComponent(file.path)}">` : ""}</div>`;
  }
  const seasonName = key => key === "" ? t("Staffel unbekannt") : `${t("Staffel")} ${key}`;
  const sourceLabel = file => file.decision.status ? t("Manuell festgelegt") : file.m.source === "nfo" ? t("Lokale NFO") : t("Namensvorschlag");

  function fileLine(s, file) {
    const {path, row} = file;
    const hdr = row.dolby_vision ? "Dolby Vision" : row.hdr_type || (row.is_hdr ? "HDR" : row.is_hdr === false ? "SDR" : "");
    const resolution = row.resolution || (row.width && row.height ? `${row.width}×${row.height}` : "");
    const tech = [resolution, hdr, row.codec].filter(Boolean).join(" · ");
    const collection = s.catalog.collections.find(c => c.id === file.collection);
    return `<li><label class="lm-file"><input type="checkbox" ${attr("select-file", path)} ${s.selected.has(path) ? "checked" : ""}><span class="lm-path" data-no-i18n>${esc(path)}</span></label>
      <div class="lm-file-meta">${literal(tech || "—")}${collection ? ` · ${literal(collection.name)}` : ""} · ${esc(sourceLabel(file))}</div>
      ${button("edit", t("Titel bearbeiten"), path, `aria-expanded="${s.editor?.path === path}"`)}${s.editor?.path === path ? editorHtml(s) : ""}</li>`;
  }

  function pageButtons(page, total, prev, next, key = "") {
    if (total <= 1) return "";
    return `<div class="lm-pager">${button(prev, t("Zurück"), key, page ? "" : "disabled")}<span>${esc(t("Seite"))} ${page + 1} / ${total}</span>${button(next, t("Weiter"), key, page + 1 < total ? "" : "disabled")}</div>`;
  }

  function workCard(s, work) {
    const file = work.files[0], {m} = file, open = s.open.has(work.key);
    const kind = kindNames()[m.kind] || t("Video");
    const eyebrow = [m.kind === "serie" && file.episode !== null ? `${t("Folge")} ${file.episode}` : kind, m.year || ""].filter(Boolean).join(" · ");
    let list = "";
    if (open) {
      const total = pageCount(work.files.length), page = Math.min(total - 1, s.filePages.get(work.key) || 0);
      const files = work.files.slice(page * PAGE, (page + 1) * PAGE);
      for (const f of files) s.visible.add(f.path);
      list = `<ul class="lm-files">${files.map(f => fileLine(s, f)).join("")}</ul>${pageButtons(page,total,"files-prev","files-next",work.key)}`;
    }
    return `<article class="lm-card"><div class="lm-overview">${poster(work.cover, work.title)}<div class="lm-copy"><div class="lm-kicker">${esc(eyebrow)}</div><h3>${literal(work.title)}</h3>
      <span class="lm-source">${esc(sourceLabel(file))}</span>${m.description ? `<p class="lm-description${open ? " lm-full" : ""}" data-no-i18n>${esc(m.description)}</p>` : ""}</div></div>
      <div class="lm-foot">${button("files", work.files.length > 1 ? `${work.files.length} ${t("Fassungen")}` : t("Datei anzeigen"), work.key, `aria-expanded="${open}"`)}</div>${list}</article>`;
  }

  function showCard(show) {
    const {file} = show;
    return `<article class="lm-card"><div class="lm-overview">${poster(show.cover, show.title)}<div class="lm-copy"><div class="lm-kicker">${esc(t("Serie"))}${file.m.show_year ? ` · ${esc(file.m.show_year)}` : ""}</div>
      <h3>${literal(show.title)}</h3><span class="lm-source">${esc(sourceLabel(file))}</span></div></div><div class="lm-foot">${button("show", numberLabel(t("Staffeln"),show.seasons.size),show.key)}</div></article>`;
  }

  function select(action, label, options, current) {
    return `<label class="lm-field"><span>${esc(label)}</span><select ${attr(action)}>${options.map(([value,text])=>`<option data-no-i18n value="${esc(value)}"${value === current ? " selected" : ""}>${esc(text)}</option>`).join("")}</select></label>`;
  }
  function filters(s) {
    const cols = s.catalog.collections.map(c => [c.id, c.name]);
    return `<div class="lm-toolbar">${field("search",t("Bibliothek durchsuchen"),s.search,"","search")}
      ${select("kind-filter", t("Medienart"), [["",t("Alle Medienarten")],...Object.entries(kindNames())], s.kind)}
      ${select("collection-filter", t("Sammlung"), [["",t("Alle Sammlungen")],...cols], s.collection)}
      ${button("manage",t("Sammlungen verwalten"),"",`aria-expanded="${s.manage}"`)}</div>`;
  }

  function editorHtml(s) {
    const e = s.editor, k = e.path;
    return `<div class="lm-editor">${field("title",t("Angezeigter Titel"),e.title,k)}
      <div class="lm-actions">${button("confirm",t("Treffer bestätigen"),k)}${button("standalone",t("Eigenständig lassen"),k)}
      ${s.entries.get(k).decision.status ? button("reset",t("Manuelle Entscheidung zurücksetzen"),k) : ""}</div>
      <details ${e.online ? "open" : ""}><summary>${esc(t("Anderen Titel suchen"))}</summary>
      <p class="hint">${esc(t("Auf Knopfdruck wird nur der eingegebene Suchtext an Wikidata gesendet."))}</p>
      ${field("query",t("Suchtext für Wikidata"),e.query,k)} ${button("lookup",t("Online suchen"),k)}
      <ul>${e.hits.map((h,i)=>`<li>${button("candidate",h.title,String(i))} ${literal(h.description)}</li>`).join("")}</ul></details></div>`;
  }

  function managerHtml(s) {
    return `<div class="lm-editor">${field("new-name",t("Name der Sammlung"),s.newName || "")}
      ${select("new-kind",t("Art der Sammlung"),["sammlung","film","serie","sendung"].map((v,i)=>[v,t(["Sammlung","Film","Serie","Sendung"][i])]),s.newKind || "sammlung")}
      <div class="lm-actions">${button("create",t("Sammlung anlegen"))}${button("delete",t("Gewählte Sammlung löschen"),"",s.collection ? "" : "disabled")}</div></div>`;
  }

  function selectionHtml(s) {
    const cols = s.catalog.collections;
    if (!cols.some(c=>c.id === s.target)) s.target = "";
    return `<div class="lm-editor"><details><summary>${esc(numberLabel(t("Markierte Dateien"),s.selected.size))}</summary>
      <ul>${[...s.selected].map(p=>`<li>${literal(p)}</li>`).join("")}</ul></details><div class="lm-actions">
      ${select("target",t("Sammlung"),[["",t("Sammlung wählen …")],...cols.map(c=>[c.id,c.name])],s.target)}
      ${button("assign",t("Zur Sammlung hinzufügen"),"",s.target ? "" : "disabled")}${button("unassign",t("Zuordnung lösen"))}
      ${button("merge",t("Weitere Fassung"),"",s.selected.size < 2 ? "disabled" : "")}${button("clear",t("Auswahl leeren"))}</div>
      ${s.merge ? `${field("group-title",t("Gemeinsamer Titel"),s.groupTitle)} ${button("version",t("Als Fassungen verbinden"))}` : ""}</div>`;
  }

  async function mutate(s, url, body, method = "POST") {
    if (s.saving) return;
    s.saving = true;
    const options = {method};
    if (body) Object.assign(options, {headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    try {
      const res = await fetch(url, options);
      if (!res.ok) throw new Error((await res.json()).error || t("Speichern fehlgeschlagen."));
      s.status = t("Gespeichert."); s.editor = null; s.merge = false; s.newName = ""; s.selected.clear();
      document.dispatchEvent(new CustomEvent("library-catalog-changed"));
    } catch (err) { s.status = err.message; }
    finally { s.saving = false; if (active === s) draw(s); }
  }

  async function lookup(s) {
    const e = s.editor, query = e.query.trim();
    if (query.length < 2) { s.status = t("Mindestens zwei Zeichen eingeben."); draw(s); return; }
    const current = () => s === active && s.editor === e && query === e.query.trim();
    e.online = true; e.hits = []; s.status = t("Lade …"); draw(s);
    try {
      const res = await fetch(`/api/library/title-search?q=${encodeURIComponent(query)}`);
      const data = await res.json();
      if (!current()) return;
      if (!res.ok) throw new Error(data.error || t("Titelsuche fehlgeschlagen."));
      e.hits = data.results; s.status = e.hits.length ? "" : t("Keine Treffer.");
    } catch (err) { if (!current()) return; s.status = err.message; }
    draw(s);
  }

  function saveFocus(el) {
    const n = document.activeElement;
    if (!n?.dataset?.lm || !el.contains(n)) return null;
    return [n.dataset.lm, n.dataset.key, n.selectionStart, n.selectionEnd];
  }
  function restoreFocus(el, focus) {
    if (!focus) return;
    const n = Array.from(el.querySelectorAll("[data-lm]")).find(n => n.dataset.lm === focus[0] && n.dataset.key === focus[1]);
    if (!n || n.disabled) return;
    n.focus({preventScroll:true});
    if (focus[2] != null && n.setSelectionRange) n.setSelectionRange(focus[2],focus[3]);
  }

  function draw(s, targetFocus) {
    const focus = targetFocus || saveFocus(s.el);
    const show = s.tree.shows.get(s.show), season = show?.seasons.get(s.season);
    if (!show) s.show = null;
    if (!season) s.season = null;
    const items = season ? season.works : show ? [...show.seasons.values()].sort((a,b)=>compare(a.key,b.key)) : s.tree.top;
    const total = pageCount(items.length); s.page = Math.min(total - 1, Math.max(0,s.page));
    s.visible = new Set();
    const cards = items.slice(s.page * PAGE, (s.page + 1) * PAGE).map(item => {
      if (item.type === "show") return showCard(item);
      if (item.type === "season") return `<article class="lm-card lm-season"><h3>${esc(seasonName(item.key))}</h3>${button("season",numberLabel(t("Folgen"),item.works.length),item.key)}</article>`;
      return workCard(s,item);
    }).join("");
    if (s.editor && !s.visible.has(s.editor.path)) s.editor = null;
    if (s.selected.size < 2) s.merge = false;
    const nav = show ? `<nav class="lm-crumbs" aria-label="${esc(t("Seriennavigation"))}">${button("home", t("Alle Medien"))} / ${season ? `${button("show",show.title,show.key)} / ${esc(seasonName(season.key))}` : literal(show.title)}</nav>` : "";
    s.el.innerHTML = `${filters(s)}${s.manage ? managerHtml(s) : ""}${nav}${s.selected.size ? selectionHtml(s) : ""}
      <p role="status" class="lm-status">${esc(s.status)}</p><div class="lm-grid">${cards || `<p class="lm-empty">${esc(s.entries.size ? t("Keine Medien für diesen Filter.") : t("Dieser Scan enthält keine Medien."))}</p>`}</div>${pageButtons(s.page,total,"prev","next")}`;
    restoreFocus(s.el,focus);
  }

  function onControl(s, event) {
    const node = event.target.closest?.("[data-lm]");
    if (!node || !s.el.contains(node) || s !== active) return;
    const {lm:action, key} = node.dataset;
    if (event.type === "input" && action === "search") { s.search = node.value; s.page = 0; s.show = null; project(s); }
    else if (event.type === "change" && ["kind-filter","collection-filter"].includes(action)) {
      s[action === "kind-filter" ? "kind" : "collection"] = node.value; s.page = 0; s.show = null; project(s);
    } else if (event.type === "input") {
      if (action === "new-name") s.newName = node.value;
      if (action === "group-title") s.groupTitle = node.value;
      if (s.editor?.path === key && action === "title") s.editor.title = node.value;
      if (s.editor?.path === key && action === "query") { s.editor.query = node.value; s.editor.online = true; }
      return;
    } else if (event.type === "change" && action === "select-file" && s.visible.has(key)) {
      if (node.checked) s.selected.add(key); else s.selected.delete(key);
    } else if (event.type === "change" && action === "target") s.target = node.value;
    else if (event.type === "change" && action === "new-kind") s.newKind = node.value;
    else if (event.type === "click") {
      const paths = [...s.selected];
      switch (action) {
        case "manage": s.manage = !s.manage; break;
        case "create": return mutate(s,CATALOG,{name:s.newName || "",kind:s.newKind || "sammlung"});
        case "delete": {
          const col = s.catalog.collections.find(c => c.id === s.collection);
          if (col && window.confirm(`${t("Sammlung löschen")} – ${col.name}?`)) return mutate(s,`${CATALOG}/${encodeURIComponent(col.id)}`,null,"DELETE");
          return;
        }
        case "assign": case "unassign":
          if (paths.length && (action === "unassign" || s.target)) return mutate(s,`${CATALOG}/assign`,{paths,collection_id:action === "assign" ? s.target : ""});
          return;
        case "edit": s.editor = s.editor?.path === key ? null : {path:key,title:s.entries.get(key).title,query:"",online:false,hits:[]}; break;
        case "lookup": return lookup(s);
        case "candidate": s.editor.title = s.editor.hits[Number(key)].title; s.editor.online = false; break;
        case "confirm": case "standalone": case "reset":
          return mutate(s,`${CATALOG}/decision`,{action,paths:[key],title:s.editor.title.trim()});
        case "clear": s.selected.clear(); break;
        case "merge": if (paths.length >= 2) { s.merge = !s.merge; s.groupTitle = s.entries.get(paths[0]).title; } break;
        case "version": if (s.merge && paths.length >= 2) return mutate(s,`${CATALOG}/decision`,{action,paths,title:s.groupTitle.trim()}); return;
        case "files": s.open.has(key) ? s.open.delete(key) : s.open.add(key); break;
        case "show": s.show = key; s.season = null; s.page = 0; break;
        case "season": s.season = key; s.page = 0; break;
        case "home": s.show = null; s.season = null; s.page = 0; break;
        case "prev": s.page--; break;
        case "next": s.page++; break;
        case "files-prev": case "files-next": s.filePages.set(key,Math.max(0,(s.filePages.get(key)||0) + (action === "files-next" ? 1 : -1))); break;
        default: return;
      }
    } else return;
    draw(s);
  }

  function render(rows, catalog, root) {
    const el = document.getElementById("lib-media-browser");
    if (!el) return;
    if (!active || active.el !== el || active.root !== root) {
      if (active?.el !== el) for (const type of ["click","input","change"]) {
        el.addEventListener(type,e=>{ if (active?.el === el) onControl(active,e); });
      }
      active = {el,root, search:"", kind:"", collection:"", show:null, season:null, page:0,
        selected:new Set(), open:new Set(), filePages:new Map(), editor:null, status:"", manage:false};
    }
    const s = active;
    s.catalog = {...catalog,collections:Array.isArray(catalog?.collections) ? catalog.collections : []};
    if (!s.catalog.collections.some(c=>c.id === s.collection)) s.collection = "";
    s.entries = indexRows(Array.isArray(rows) ? rows : [], s.catalog);
    project(s); draw(s);
  }
  window.LibraryMedia = {render};
})();
