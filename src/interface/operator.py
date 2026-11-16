"""The operator channel: where a person sees an intervention and takes, uses and hands back control (§5).

One local implementation, built for the brief: the browser runs headed and the person works in that same window (the
same context, cookies and session), while this page at localhost:4200 shows the request, a live screenshot refreshed
every 1.5 s and the Take control / Hand back / Abort buttons. In production the same interface would sit in front of
a headless browser with a CDP screencast and input relay; that's described in REPORT, not built.

The page runs in the same event loop as the automation, so its buttons act on the live Session directly.

Who may use it (D25): each intervention has its own random key, handed out only in the link the run prints, after
a `#` so the browser never sends it to a server log. Everything about an intervention (what it is, the live
screenshot, every button) needs that key in a header, which a page on another site can't add without a CORS grant
this server never gives. The page itself is an empty shell. Only 127.0.0.1 / localhost are accepted as the Host,
nothing is cached or framed, and a wrong key is logged on the run. The operator's name is still what they type; a
bank's sign-on would supply it.
"""
import asyncio
import hmac
import secrets
from dataclasses import asdict

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from starlette.middleware.trustedhost import TrustedHostMiddleware

from interface.session import ControlError, Intervention, Session

OPERATOR_PORT = 4200
KEY_HEADER = "X-Intervention-Key"


class OperatorHub:
    def __init__(self):
        self.open: dict[str, tuple[Intervention, Session, object]] = {}
        self.closed: dict[str, dict] = {}
        self._keys: dict[str, str] = {}  # intervention id -> its key; memory only, never on the Intervention
        self.app = self._build_app()
        self._server: uvicorn.Server | None = None
        self.on_publish = None  # tests and the CLI hook in here to hear about new interventions

    def publish(self, intervention: Intervention, session: Session, surface) -> None:
        self._keys[intervention.id] = secrets.token_urlsafe(32)
        self.open[intervention.id] = (intervention, session, surface)
        if self.on_publish:
            self.on_publish(intervention)

    def close(self, intervention_id: str, how: str) -> None:
        if entry := self.open.pop(intervention_id, None):
            self.closed[intervention_id] = {**_public(entry[0]), "closed": how}

    def url(self, intervention_id: str, port: int = OPERATOR_PORT) -> str:
        return f"http://127.0.0.1:{port}/interventions/{intervention_id}#key={self._keys[intervention_id]}"

    def auth(self, intervention_id: str) -> dict[str, str]:
        """The header the page sends; for code that plays the operator (tests, the evidence script)."""
        return {KEY_HEADER: self._keys[intervention_id]}

    async def start(self, port: int = OPERATOR_PORT) -> None:
        self._server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=port, log_level="warning"))
        asyncio.create_task(self._server.serve())
        while not self._server.started:
            await asyncio.sleep(0.05)

    async def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
            await asyncio.sleep(0.2)

    def _authorized(self, intervention_id: str, request: Request) -> None:
        """403 unless the request carries this intervention's key. Unknown and wrong look the same from outside."""
        key = self._keys.get(intervention_id, "")
        given = request.headers.get(KEY_HEADER, "")
        if key and hmac.compare_digest(given.encode(), key.encode()):
            return
        if entry := self.open.get(intervention_id):
            entry[1].log.event("operator_rejected", intervention=intervention_id, path=request.url.path,
                               had_key=bool(given))
        raise HTTPException(403, "this link isn't valid for that intervention")

    def _entry(self, intervention_id: str):
        if intervention_id not in self.open:
            raise HTTPException(404, "no open intervention with that id")
        return self.open[intervention_id]

    def _build_app(self) -> FastAPI:
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])  # no DNS rebinding

        @app.middleware("http")
        async def no_cache_no_frames(request: Request, call_next):
            response = await call_next(request)
            response.headers.update({"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                     "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"})
            return response

        @app.get("/", response_class=HTMLResponse)
        def index():
            return "<p>Open an intervention from the link the run printed. Interventions aren't listed here.</p>"

        @app.get("/interventions/{intervention_id}", response_class=HTMLResponse)
        def page(intervention_id: str):
            return PAGE  # a shell: everything about the intervention is fetched with its key

        @app.get("/interventions/{intervention_id}/state")
        def state(intervention_id: str, request: Request):
            self._authorized(intervention_id, request)
            if intervention_id in self.closed:
                return {"state": "closed", **self.closed[intervention_id]}
            intervention, session, _ = self._entry(intervention_id)
            return {**session.summary(), **_public(intervention)}

        @app.get("/interventions/{intervention_id}/screenshot")
        async def screenshot(intervention_id: str, request: Request):
            # The live view for the person handling it. Shown, never stored.
            self._authorized(intervention_id, request)
            _, _, surface = self._entry(intervention_id)
            return Response(await surface.page.screenshot(), media_type="image/png")

        def control(intervention_id: str, verb: str, operator: str, request: Request):
            self._authorized(intervention_id, request)
            _, session, _ = self._entry(intervention_id)
            try:
                match verb:
                    case "claim": session.claim(operator)
                    case "heartbeat": session.heartbeat(operator)
                    case "handback": session.hand_back(operator)
                    case "abort": session.abort(operator)
            except ControlError as e:
                raise HTTPException(409, str(e))
            return session.summary()

        def route(verb: str):
            def endpoint(intervention_id: str, operator: str, request: Request):
                return control(intervention_id, verb, operator, request)
            return endpoint

        for verb in ("claim", "heartbeat", "handback", "abort"):
            app.post(f"/interventions/{{intervention_id}}/{verb}")(route(verb))
        return app


def _public(intervention: Intervention) -> dict:
    data = asdict(intervention)
    data.pop("screenshot", None)  # a local path; the page has the live view instead
    return data


PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Intervention</title>
<style>
 body{font:14px system-ui;margin:16px;max-width:1100px} .row{display:flex;gap:16px;flex-wrap:wrap}
 img{max-width:640px;width:100%;border:1px solid #ccc} pre{background:#f5f5f5;padding:8px;max-height:300px;overflow:auto}
 button{font-size:14px;padding:6px 14px;margin-right:6px} #state{font-weight:600}
</style></head><body>
<h1>Needs a person</h1>
<p><b id="subject">…</b><br>Step <code id="step"></code><br>Reason: <span id="reason"></span></p>
<p>Control: <span id="state">…</span></p>
<p><input id="op" placeholder="your name" size="14">
 <button onclick="send('claim')">Take control</button>
 <button onclick="send('handback')">Hand back</button>
 <button onclick="send('abort')">Abort run</button></p>
<p>While you hold control, work in the automation's browser window. What you do there is recorded.</p>
<div class="row"><div><h3>Live session</h3><img id="shot" alt=""></div>
<div><h3>What you've done</h3><ol id="actions"></ol><h3>Screen when it paused</h3><pre id="snapshot"></pre></div></div>
<script>
const id = location.pathname.split("/").pop(), op = document.getElementById("op"), $ = (x) => document.getElementById(x);
// The key arrives after the #, which is never sent to a server; keep it for this tab and take it out of the address bar
let key = new URLSearchParams(location.hash.slice(1)).get("key") || "";
try { if (key) sessionStorage.setItem("key:" + id, key); else key = sessionStorage.getItem("key:" + id) || ""; } catch (e) {}
if (location.hash) history.replaceState(null, "", location.pathname);
try { op.value = localStorage.getItem("operator") || ""; } catch (e) {}
const call = (path, method = "GET") => fetch(`/interventions/${id}/${path}`, {method, headers: {"X-Intervention-Key": key}});
async function send(verb) {
  if (!op.value) { alert("Enter your name first"); return; }
  try { localStorage.setItem("operator", op.value); } catch (e) {}
  const r = await call(`${verb}?operator=${encodeURIComponent(op.value)}`, "POST");
  if (!r.ok) alert((await r.json()).detail);
  refresh();
}
let shown = null;
async function refresh() {
  const r = await call("state");
  if (!r.ok) { $("state").textContent = (await r.json()).detail; return; }
  const s = await r.json();
  $("subject").textContent = s.subject; $("step").textContent = `${s.step_id}: ${s.intent}`; $("reason").textContent = s.reason;
  $("snapshot").textContent = (s.snapshot || "").slice(-4000);
  $("state").textContent = s.state === "closed" ? `closed (${s.closed})` : `${s.state}, held by ${s.holder}`;
  $("actions").replaceChildren(...(s.human_actions || []).map((a) => Object.assign(document.createElement("li"), {textContent: `${a.action} ${a.role} “${a.name}”`})));
  if (s.state !== "closed") {
    const img = await call("screenshot");
    if (img.ok) { if (shown) URL.revokeObjectURL(shown); shown = URL.createObjectURL(await img.blob()); $("shot").src = shown; }
  }
  if (s.state === "human" && op.value && s.holder === `operator:${op.value}`) call(`heartbeat?operator=${encodeURIComponent(op.value)}`, "POST");
}
refresh(); setInterval(refresh, 1500);
</script></body></html>"""
