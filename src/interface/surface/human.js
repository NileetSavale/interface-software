// Records what a person does while they hold control of the live session (docs/ARCHITECTURE.md §5, "capturing the
// human"). Installed in every frame next to locate.js. It describes the element at the moment of the event, in the
// same terms the recorder uses (role and name, frame path, labelled region, label/table position), because a click
// that submits a form navigates away before anything could ask the page about it afterwards.
(() => {
  if (window.__ifzHumanInstalled || typeof window.__ifzHuman !== "function") return;
  window.__ifzHumanInstalled = true;
  const ifz = () => window.__ifz;
  const norm = (s) => (s || "").replace(/\s+/g, " ").trim();

  function roleName(el) {
    const tag = el.tagName, type = (el.getAttribute("type") || "text").toLowerCase();
    const labelled = () => norm((el.labels && el.labels[0] && el.labels[0].innerText) || el.getAttribute("aria-label"));
    if (tag === "A") return ["link", norm(el.innerText)];
    if (tag === "BUTTON" || (tag === "INPUT" && ["submit", "button", "reset"].includes(type)))
      return ["button", norm(el.value || el.innerText)];
    if (tag === "SELECT") return ["combobox", labelled()];
    if (tag === "INPUT" && type === "checkbox") return ["checkbox", labelled()];
    if (tag === "INPUT" && type === "radio") return ["radio", labelled()];
    if (tag === "TEXTAREA" || tag === "INPUT") return ["textbox", labelled()];
    return [el.getAttribute("role") || "generic", norm(el.innerText).slice(0, 80)];
  }

  function framePath() {
    const path = [];
    for (let w = window; w !== w.top; w = w.parent) path.unshift(w.frameElement ? w.frameElement.name || "" : "");
    return path;
  }

  function report(kind, el) {
    const [role, name] = roleName(el);
    const value = kind === "change" ? (el.tagName === "SELECT" ? norm(el.selectedOptions[0]?.innerText) : el.value) : null;
    window.__ifzHuman({ kind, role, name, frame: framePath(), region: ifz() ? ifz().regionOf(el) : null,
                        places: ifz() ? ifz().describeValue(el) : [], value });
  }

  document.addEventListener("click", (e) => {
    const el = e.target.closest("a,button,input[type=submit],input[type=button],input[type=checkbox],input[type=radio],[role=button]");
    if (el) report("click", el);
  }, true);
  document.addEventListener("change", (e) => {
    const el = e.target;
    if (el.matches && el.matches("input:not([type=checkbox]):not([type=radio]),textarea,select")) report("change", el);
  }, true);
})();
