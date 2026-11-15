// In-page helpers for the strategies the browser has no built-in locator for: a value next to a label cell
// ("Name:" | "Jane Doe"), and a cell picked by row and column values in a legacy table. Installed in every frame
// as window.__ifz. The recorder uses describeValue to propose strategies; replay uses anchor/tableCell to resolve
// them; both sides run the same code, so what was recorded is what gets found.
(() => {
  if (window.__ifz) return;
  const norm = (s) => (s || "").replace(/\s+/g, " ").trim();
  const textOf = (el) => norm(el.innerText ?? el.textContent);
  // m is {exact: "..."} or {regex: [source, flags]}
  const matches = (text, m) => (m.exact !== undefined ? text === norm(m.exact) : new RegExp(m.regex[0], m.regex[1]).test(text));

  function tableLabel(table) {
    if (table.caption) return textOf(table.caption);
    let p = table.previousElementSibling;
    while (p && p.tagName === "BR") p = p.previousElementSibling;
    return p && p.tagName !== "TABLE" ? textOf(p) : null;
  }

  function headerRow(table) {
    return Array.from(table.rows).find((r) => Array.from(r.cells).some((c) => c.tagName === "TH")) || null;
  }

  function control(cell, role) {
    if (!role) return cell;
    return cell.querySelector("input:not([type=hidden]),select,textarea,button,a") || null;
  }

  function anchor(anchorM, relation, role) {
    const hits = Array.from(document.querySelectorAll("td,th,label,b,span,font")).filter((el) => matches(textOf(el), anchorM));
    const innermost = hits.filter((el) => !hits.some((o) => o !== el && el.contains(o)));
    const out = [];
    for (const a of innermost) {
      const cell = a.closest("td,th");
      if (!cell) continue;
      let target = null;
      if (relation === "right-of") target = cell.nextElementSibling;
      else if (relation === "left-of") target = cell.previousElementSibling;
      else {
        const row = relation === "below" ? cell.parentElement.nextElementSibling : cell.parentElement.previousElementSibling;
        target = row ? row.cells[cell.cellIndex] : null;
      }
      const el = target && control(target, role);
      if (el) out.push(el);
    }
    return out;
  }

  function tableCell(tableM, column, rowColumn, rowEquals, role) {
    const out = [];
    for (const table of document.querySelectorAll("table")) {
      const label = tableLabel(table);
      const header = headerRow(table);
      if (!label || !header || !matches(label, tableM)) continue;
      const heads = Array.from(header.cells).map(textOf);
      const ci = heads.indexOf(column), ri = heads.indexOf(rowColumn);
      if (ci < 0 || ri < 0) continue;
      for (const row of table.rows) {
        const cell = row !== header && row.cells[ri] && textOf(row.cells[ri]) === norm(rowEquals) && row.cells[ci];
        const el = cell && control(cell, role);
        if (el) out.push(el);
      }
    }
    return out;
  }

  // A label is static text. Anything with a digit is treated as data (member and account numbers, amounts, dates):
  // anchoring on it would break for the next record and would write that data into the artifact.
  const isLabel = (text) => !!text && !/\d/.test(text);

  // Strategies that describe where a value sits, never what it says: the value changes per record, its place doesn't
  function describeValue(el) {
    const cell = el.closest("td,th");
    if (!cell) return [];
    const out = [];
    const table = cell.closest("table");
    const header = table && headerRow(table);
    const label = table && tableLabel(table);
    if (header && label && cell.parentElement !== header) {
      const heads = Array.from(header.cells).map(textOf);
      const row = cell.parentElement;
      if (heads[cell.cellIndex] && heads[0] && cell.cellIndex !== 0 && isLabel(textOf(row.cells[0]))) {
        out.push({ kind: "tableCell", tableAnchor: label, column: heads[cell.cellIndex],
                   rowWhere: { column: heads[0], equals: textOf(row.cells[0]) } });
      }
    }
    const prev = cell.previousElementSibling;
    if (prev && isLabel(textOf(prev))) out.push({ kind: "anchor", anchorText: textOf(prev), relation: "right-of" });
    return out;
  }

  // The nearest labelled region around an element: what tells two same-named controls apart (scope, not index).
  // A fieldset is named by its legend, a dialog or landmark by aria-label. Names with digits are data, so skipped.
  function regionOf(el) {
    for (let p = el.parentElement; p; p = p.parentElement) {
      let role = null, name = null;
      if (p.tagName === "FIELDSET" && p.querySelector(":scope > legend")) {
        role = "group"; name = textOf(p.querySelector(":scope > legend"));
      } else if (p.tagName === "DIALOG") {
        role = "dialog"; name = p.getAttribute("aria-label");
      } else if (p.getAttribute("role") && p.getAttribute("aria-label")) {
        role = p.getAttribute("role"); name = p.getAttribute("aria-label");
      }
      if (role && isLabel(name)) return { role, name: norm(name) };
    }
    return null;
  }

  // Blacks out PII before a screenshot is saved, by the same detectors the text masking uses (interfaze.pii, passed
  // in): values matching a pattern, the cell after a sensitive label, every cell of a sensitive column, and every
  // filled text field. on=false restores the page. Returns the texts it masked, so a test can check them.
  function maskPII(patterns, labels, on) {
    if (!on) {
      for (const el of document.querySelectorAll("[data-ifz-masked]")) {
        el.style.cssText = el.dataset.ifzMasked; delete el.dataset.ifzMasked;
      }
      return [];
    }
    const pats = patterns.map((p) => new RegExp(p));
    const labs = labels.map((l) => new RegExp(l.replace(/^\(\?i\)/, ""), "i"));
    const sensitive = (label) => labs.some((r) => r.test(label));
    const masked = [];
    const hide = (el) => {
      if (el.dataset.ifzMasked !== undefined) return;
      el.dataset.ifzMasked = el.style.cssText;
      el.style.setProperty("background", "#000", "important");
      el.style.setProperty("color", "#000", "important");
      masked.push(textOf(el) || el.value || "");
    };
    const leaves = Array.from(document.querySelectorAll("td,th,span,b,font,a,li,p,label,div,dd"));
    for (const el of leaves) {
      const t = textOf(el);
      if (!t || !pats.some((r) => r.test(t))) continue;
      if (Array.from(el.querySelectorAll("*")).some((c) => pats.some((r) => r.test(textOf(c))))) continue; // innermost
      hide(el);
    }
    for (const cell of document.querySelectorAll("td")) {
      const prev = cell.previousElementSibling;
      if (prev && textOf(prev).endsWith(":") && sensitive(textOf(prev))) hide(cell);
    }
    for (const table of document.querySelectorAll("table")) {
      const header = headerRow(table);
      if (!header) continue;
      const cols = Array.from(header.cells).map((c, i) => (sensitive(textOf(c)) ? i : -1)).filter((i) => i >= 0);
      for (const row of table.rows) if (row !== header) for (const i of cols) if (row.cells[i]) hide(row.cells[i]);
    }
    for (const input of document.querySelectorAll("input:not([type=hidden]):not([type=submit]):not([type=button]),textarea")) {
      if (input.value) hide(input);
    }
    return masked;
  }

  window.__ifz = { anchor, tableCell, describeValue, regionOf, textOf, maskPII };
})();
