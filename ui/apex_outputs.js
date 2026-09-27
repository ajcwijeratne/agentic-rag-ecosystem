/* ==========================================================================
   Apex outputs  ·  ui/apex_outputs.js
   --------------------------------------------------------------------------
   The browser side of orchestrator/outputs.py: documents Apex drafts on
   request, saved as markdown notes in the vault (13_Command Centre/Outputs).

     ApexOutputs.detect(text)            is this a document request?
     ApexOutputs.draft(request, opts)    start one; resolves to its doc_start
     ApexOutputs.follow(id, onEvent)     stream its events until it ends
     ApexOutputs.stop(id) / save(id, content) / get(id) / list()
     ApexOutputs.md(markdown)            safe HTML for the preview

   Events from follow() go to ApexVoice.docEvent when it is loaded, so the
   Radial shows the draft being written and the page's pane gets the text
   through one path whether the request was spoken or typed.
   Plain JavaScript, no build step. Original WijerCo code.
   ========================================================================== */
(function () {
  "use strict";
  if (window.ApexOutputs) return;

  function apiBase() {
    let saved = "";
    try { saved = localStorage.getItem("cc_api_base") || ""; } catch (e) { /* private mode */ }
    if (saved) return saved.replace(/\/$/, "");
    const preview = new Set(["5179", "5500", "5501"]);
    if (location.protocol === "file:" || preview.has(location.port)) return "http://localhost:8000";
    return location.origin;
  }
  const url = (p) => apiBase() + p;
  const enc = encodeURIComponent;

  async function json(res) {
    let body = null;
    try { body = await res.json(); } catch (e) { body = null; }
    if (!res.ok) {
      const err = new Error((body && body.detail) || ("HTTP " + res.status));
      err.status = res.status;
      throw err;
    }
    return body;
  }

  function relay(ev) {
    if (window.ApexVoice && window.ApexVoice.docEvent) window.ApexVoice.docEvent(ev);
  }

  const ApexOutputs = {
    version: "1.0.0",

    /** {document: true, kind, title, words} or {document: false}. Never throws. */
    async detect(text) {
      try {
        const r = await window.fetch(url("/outputs/detect"), {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text: String(text || "").slice(0, 3900) }),
        });
        if (!r.ok) return { document: false };
        return await r.json();
      } catch (e) { return { document: false }; }
    },

    /** Start a draft. Resolves to its doc_start event (id, title, path...). */
    async draft(request, opts) {
      const o = opts || {};
      const r = await window.fetch(url("/outputs/draft"), {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request, session_id: o.session_id || "", source: o.source || "chat",
                               title: o.title || "", kind: o.kind || "" }),
      });
      return json(r);
    },

    /**
     * Follow a draft's events until it ends. Each event goes to onEvent, or to
     * ApexVoice.docEvent when no handler is given. Resolves to the last event.
     */
    async follow(id, onEvent, signal) {
      const handle = onEvent || relay;
      const r = await window.fetch(url("/outputs/" + enc(id) + "/stream"), { signal });
      if (!r.ok || !r.body) throw new Error("stream " + r.status);
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = "", last = null;
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const parts = buf.split("\n\n");
        buf = parts.pop() || "";
        for (const part of parts) {
          if (!part.startsWith("data: ")) continue;
          let ev;
          try { ev = JSON.parse(part.slice(6)); } catch (e) { continue; }
          last = ev;
          try { handle(ev); } catch (e) { /* a view must not stop the stream */ }
          if (ev.type === "doc_done" || ev.type === "doc_stopped" || ev.type === "doc_error") {
            try { reader.cancel(); } catch (e) { /* ignore */ }
            return ev;
          }
        }
      }
      return last;
    },

    /** Detect, and if it is a document request, draft and follow it. */
    async run(text, opts) {
      const d = await ApexOutputs.detect(text);
      if (!d || !d.document) return null;
      const start = await ApexOutputs.draft(text, opts);
      ApexOutputs.follow(start.id, (opts && opts.onEvent) || relay).catch((e) => {
        relay({ type: "doc_error", id: start.id, message: String(e && e.message || e) });
      });
      return start;
    },

    async stop(id) {
      return json(await window.fetch(url("/outputs/" + enc(id) + "/stop"), { method: "POST" }));
    },
    async save(id, content, title) {
      return json(await window.fetch(url("/outputs/" + enc(id)), {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content, title: title || "" }),
      }));
    },
    async get(id) { return json(await window.fetch(url("/outputs/" + enc(id)))); },
    async list(limit) { return json(await window.fetch(url("/outputs?limit=" + (limit || 30)))); },
    downloadUrl(id) { return url("/outputs/" + enc(id) + "/download"); },

    /** Download through fetch, so the API key header goes with it. */
    async download(id, filename) {
      const r = await window.fetch(ApexOutputs.downloadUrl(id));
      if (!r.ok) throw new Error("download " + r.status);
      const blob = await r.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = filename || (id + ".md");
      document.body.appendChild(a);
      a.click();
      setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
    },

    md: renderMarkdown,
  };

  /* ------------------------------------------------------------- markdown
     Enough GitHub-flavoured markdown for documents: headings, paragraphs,
     lists (nested by indent), quotes, code, tables, rules, bold, italic,
     inline code and links. Everything is escaped first, so a draft can never
     inject markup into the page. */
  function esc(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function inline(s) {
    let t = esc(s);
    const codes = [];
    t = t.replace(/`([^`]+)`/g, (m, c) => { codes.push(c); return "\u0000" + (codes.length - 1) + "\u0000"; });
    t = t.replace(/\[([^\]]+)\]\(((?:https?:|obsidian:|mailto:)[^)\s]+)\)/g,
      (m, label, href) => `<a href="${href}" target="_blank" rel="noopener">${label}</a>`);
    t = t.replace(/(\*\*|__)(?=\S)(.+?\S)\1/g, "<strong>$2</strong>");
    t = t.replace(/(^|[^\w*])\*(?=\S)(.+?\S)\*(?!\w)/g, "$1<em>$2</em>");
    t = t.replace(/(^|[^\w_])_(?=\S)(.+?\S)_(?!\w)/g, "$1<em>$2</em>");
    t = t.replace(/\[(To confirm:[^\]]*)\]/g, '<mark class="doc-tbc">[$1]</mark>');
    t = t.replace(/\u0000(\d+)\u0000/g, (m, i) => "<code>" + codes[+i] + "</code>");
    return t;
  }
  function renderMarkdown(src) {
    const lines = String(src || "").replace(/\r\n?/g, "\n").split("\n");
    const out = [];
    let i = 0;
    const isTableSep = (l) => /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/.test(l);
    const cells = (l) => l.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i++; continue; }
      if (i === 0 && line.trim() === "---") {           // frontmatter, if any slipped through
        let j = 1;
        while (j < lines.length && lines[j].trim() !== "---") j++;
        if (j < lines.length) { i = j + 1; continue; }
      }
      let m;
      if ((m = /^```(\w*)\s*$/.exec(line))) {
        const buf = [];
        i++;
        while (i < lines.length && !/^```\s*$/.test(lines[i])) buf.push(lines[i++]);
        i++;
        out.push("<pre><code>" + esc(buf.join("\n")) + "</code></pre>");
        continue;
      }
      if ((m = /^(#{1,6})\s+(.*)$/.exec(line))) {
        const n = m[1].length;
        out.push(`<h${n}>${inline(m[2].replace(/\s+#+\s*$/, ""))}</h${n}>`);
        i++; continue;
      }
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { out.push("<hr>"); i++; continue; }
      if (line.includes("|") && i + 1 < lines.length && isTableSep(lines[i + 1])) {
        const head = cells(line);
        i += 2;
        const rows = [];
        while (i < lines.length && lines[i].includes("|") && lines[i].trim()) rows.push(cells(lines[i++]));
        out.push("<table><thead><tr>" + head.map((c) => "<th>" + inline(c) + "</th>").join("") + "</tr></thead><tbody>"
          + rows.map((r) => "<tr>" + r.map((c) => "<td>" + inline(c) + "</td>").join("") + "</tr>").join("") + "</tbody></table>");
        continue;
      }
      if (/^\s*>/.test(line)) {
        const buf = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ""));
        out.push("<blockquote>" + renderMarkdown(buf.join("\n")) + "</blockquote>");
        continue;
      }
      if (/^\s*(?:[-*+•]|\d+[.)])\s+/.test(line)) {
        out.push(list());
        continue;
      }
      const buf = [];
      while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|```|\s*>|\s*(?:[-*+•]|\d+[.)])\s+)/.test(lines[i])
             && !(lines[i].includes("|") && i + 1 < lines.length && isTableSep(lines[i + 1]))) {
        buf.push(lines[i++].trim());
      }
      out.push("<p>" + inline(buf.join(" ")) + "</p>");
    }
    return out.join("\n");

    function list() {
      const indentOf = (l) => (/^(\s*)/.exec(l)[1].replace(/\t/g, "    ").length);
      const base = indentOf(lines[i]);
      const ordered = /^\s*\d+[.)]\s+/.test(lines[i]);
      const items = [];
      while (i < lines.length) {
        const l = lines[i];
        if (!l.trim()) {
          // A blank line ends the list unless the next line carries on with it.
          const nxt = lines[i + 1] || "";
          const sameKind = /^\s*\d+[.)]\s+/.test(nxt) === ordered;
          if (/^\s*(?:[-*+•]|\d+[.)])\s+/.test(nxt) && (indentOf(nxt) > base || (indentOf(nxt) === base && sameKind))) { i++; continue; }
          break;
        }
        const ind = indentOf(l);
        const im = /^\s*(?:[-*+•]|\d+[.)])\s+(.*)$/.exec(l);
        if (im && ind === base && /^\s*\d+[.)]\s+/.test(l) !== ordered) break;
        if (im && ind === base) { items.push({ text: im[1], sub: "" }); i++; continue; }
        if (im && ind > base && items.length) { items[items.length - 1].sub += list(); continue; }
        if (!im && ind > base && items.length) { items[items.length - 1].text += " " + l.trim(); i++; continue; }
        break;
      }
      const tag = ordered ? "ol" : "ul";
      return `<${tag}>` + items.map((it) => "<li>" + inline(it.text) + it.sub + "</li>").join("") + `</${tag}>`;
    }
  }

  window.ApexOutputs = ApexOutputs;
})();
