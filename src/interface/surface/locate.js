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

  // The text just before a table names it. Legacy layouts often wrap a table in more layout tables, so the search
  // climbs through any wrapper that holds nothing but this table, and stops at the first one with other content.
  function tableLabel(table) {
    if (table.caption) return textOf(table.caption);
    const own = textOf(table);
    for (let el = table; el && el !== document.body; el = el.parentElement) {
      if (el !== table && textOf(el) !== own) return null;
      let p = el.previousElementSibling;
      while (p && (p.tagName === "BR" || !textOf(p))) p = p.previousElementSibling;
      if (p) return p.tagName !== "TABLE" ? textOf(p) : null;
    }
    return null;
  }

  function headerRow(table) {
    return Array.from(table.rows).find((r) => Array.from(r.cells).some((c) => c.tagName === "TH")) || null;
  }

  function control(cell, role) {
    if (!role) return cell;
    return cell.querySelector("input:not([type=hidden]),select,textarea,button,a") || null;
  }

  // Form controls by role, for "the <role> after this text"; without a role, any control
  const CONTROLS = "input:not([type=hidden]),select,textarea,button";
  const BY_ROLE = {
    textbox: "input:not([type]),input[type=text],input[type=password],input[type=email],input[type=search]," +
             "input[type=tel],input[type=url],input[type=number],textarea",
    button: "button,input[type=submit],input[type=button],input[type=reset],input[type=image]",
    combobox: "select", checkbox: "input[type=checkbox]", radio: "input[type=radio]",
  };
  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "OPTION", "SELECT", "TEXTAREA", "BUTTON", "TITLE"]);

  const blockOf = (node) => {
    for (let a = node.parentElement; a && a !== document.body; a = a.parentElement) {
      if (getComputedStyle(a).display !== "inline") return a;
    }
    return document.body;
  };

  // The static text a reader meets just before an element, read as a line: "Username" in the paragraph over a box,
  // "Find by Amount:" on the line holding a field (or the line before its button). On the control's own line, only
  // the text since the previous control on it counts ("Between [ ] and [ ]": the second box is the one after "and").
  function textBefore(el) {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    walker.currentNode = el;
    for (let node = walker.previousNode(); node; node = walker.previousNode()) {
      if (!norm(node.data) || el.contains(node)) continue;
      let skipped = false;
      for (let a = node.parentElement; a; a = a.parentElement) if (SKIP.has(a.tagName)) { skipped = true; break; }
      if (skipped || node.parentElement?.closest("[hidden],[aria-hidden=true]")) continue;
      const block = blockOf(node), range = document.createRange();
      range.selectNodeContents(block);
      const earlier = Array.from(block.querySelectorAll(CONTROLS)).filter(
        (c) => c !== el && !c.contains(node) && (c.compareDocumentPosition(node) & Node.DOCUMENT_POSITION_FOLLOWING));
      if (block.contains(el)) {
        if (earlier.length) range.setStartAfter(earlier[earlier.length - 1]);
        range.setEndBefore(el);
      }
      return norm(range.toString());
    }
    return "";
  }

  function anchor(anchorM, relation, role) {
    if (relation === "after") {
      // Every control of the role whose nearest preceding text is the anchor; the caller insists on exactly one
      return Array.from(document.querySelectorAll(BY_ROLE[role] || CONTROLS))
        .filter((c) => c.getClientRects().length && matches(textBefore(c), anchorM));
    }
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

  // The rows after the header that hold a full set of cells: the results, not a footer or an empty placeholder
  const dataRows = (table, header) => Array.from(table.rows).filter(
    (r) => r !== header && r.cells.length === header.cells.length && Array.from(r.cells).some((c) => textOf(c)));

  function tableCell(tableM, column, rowColumn, rowEquals, role, first) {
    const out = [];
    for (const table of document.querySelectorAll("table")) {
      const label = tableLabel(table);
      const header = headerRow(table);
      if (!label || !header || !matches(label, tableM)) continue;
      const heads = Array.from(header.cells).map(textOf);
      const ci = heads.indexOf(column), ri = heads.indexOf(rowColumn);
      if (first) {
        // D27: the first result, for reading; a table with no results finds nothing
        const row = ci >= 0 && table.getClientRects().length ? dataRows(table, header)[0] : null;
        if (row) out.push(row.cells[ci]);
        continue;
      }
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
  function describeValue(el, firstResult) {
    const out = [];
    // Last, below the cell-based ones: a control under or after a caption ("the textbox after Username")
    const after = () => {
      const before = el.matches(CONTROLS) ? textBefore(el) : "";
      if (isLabel(before) && before.length <= 60) out.push({ kind: "anchor", anchorText: before, relation: "after" });
      return out;
    };
    const cell = el.closest("td,th");
    if (!cell) return after();
    const table = cell.closest("table");
    const header = table && headerRow(table);
    const label = table && tableLabel(table);
    if (header && label && cell.parentElement !== header) {
      const heads = Array.from(header.cells).map(textOf);
      const row = cell.parentElement;
      // Only when the goal asks for the first result, for a value (not a control), in the first data row
      if (firstResult && !el.matches(CONTROLS) && heads[cell.cellIndex] && dataRows(table, header)[0] === row) {
        out.push({ kind: "tableCell", tableAnchor: label, column: heads[cell.cellIndex], row: "first" });
      }
      if (heads[cell.cellIndex] && heads[0] && cell.cellIndex !== 0 && isLabel(textOf(row.cells[0]))) {
        out.push({ kind: "tableCell", tableAnchor: label, column: heads[cell.cellIndex],
                   rowWhere: { column: heads[0], equals: textOf(row.cells[0]) } });
      }
    }
    const prev = cell.previousElementSibling;
    if (prev && isLabel(textOf(prev))) out.push({ kind: "anchor", anchorText: textOf(prev), relation: "right-of" });
    return after();
  }

  // Where an element sits in a labelled table with a header: the table's label, its column, and the text of every
  // cell in its row by column, so the recorder can pick the row by the cell that equals an input (row where Date of
  // Birth = {{inputs.dob}}) when no static label names the row
  function rowContext(el) {
    const cell = el.closest("td,th");
    const table = cell && cell.closest("table");
    const header = table && headerRow(table);
    const label = table && tableLabel(table);
    if (!header || !label || cell.parentElement === header) return null;
    const heads = Array.from(header.cells).map(textOf);
    if (!heads[cell.cellIndex]) return null;
    return { tableAnchor: label, column: heads[cell.cellIndex],
             cells: Array.from(cell.parentElement.cells).map((c, i) => ({ column: heads[i], text: textOf(c) })).filter((c) => c.column) };
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

  // Blacks out PII before a screenshot is saved, by the same detectors the text masking uses (interface.pii, passed
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

  window.__ifz = { anchor, tableCell, describeValue, rowContext, regionOf, textOf, maskPII };
})();
