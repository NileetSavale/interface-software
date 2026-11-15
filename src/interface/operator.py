"""The operator channel: where a person sees an intervention and takes, uses and hands back control (§5).

One local implementation, built for the brief: the browser runs headed and the person works in that same window (the
same context, cookies and session), while this page at localhost:4200 shows the request, a live screenshot refreshed
every 1.5 s and the Take control / Hand back / Abort buttons. In production the same interface would sit in front of
a headless browser with a CDP screencast and input relay; that's described in REPORT, not built.

The page runs in the same event loop as the automation, so its buttons act on the live Session directly.
"""
import asyncio
from dataclasses import asdict
from html import escape

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response

from interface.session import ControlError, Intervention, Session

OPERATOR_PORT = 4200


class OperatorHub:
    def __init__(self):
        self.open: dict[str, tuple[Intervention, Session, object]] = {}
        self.closed: dict[str, dict] = {}
        self.app = self._build_app()
        self._server: uvicorn.Server | None = None
        self.on_publish = None  # tests and the CLI hook in here to hear about new interventions

    def publish(self, intervention: Intervention, session: Session, surface) -> None:
        self.open[intervention.id] = (intervention, session, surface)
        if self.on_publish:
            self.on_publish(intervention)

    def close(self, intervention_id: str, how: str) -> None:
        if entry := self.open.pop(intervention_id, None):
            self.closed[intervention_id] = {**_public(entry[0]), "closed": how}

    def url(self, intervention_id: str, port: int = OPERATOR_PORT) -> str:
        return f"http://127.0.0.1:{port}/interventions/{intervention_id}"

    async def start(self, port: int = OPERATOR_PORT) -> None:
        self._server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=port, log_level="warning"))
        asyncio.create_task(self._server.serve())
        while not self._server.started:
            await asyncio.sleep(0.05)

    async def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
            await asyncio.sleep(0.2)

    def _entry(self, intervention_id: str):
        if intervention_id not in self.open:
            raise HTTPException(404, "no open intervention with that id")
        return self.open[intervention_id]

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        @app.get("/", response_class=HTMLResponse)
        def index():
            rows = "".join(f'<li><a href="/interventions/{i}">{escape(e[0].subject)}: {escape(e[0].reason)}</a></li>'
                           for i, e in self.open.items()) or "<li>Nothing needs a person right now.</li>"
            return f"<h1>Interventions</h1><ul>{rows}</ul>"

        @app.get("/interventions/{intervention_id}", response_class=HTMLResponse)
        def page(intervention_id: str):
            if intervention_id in self.closed:
                return f"<p>This intervention is closed ({escape(self.closed[intervention_id]['closed'])}).</p>"
            intervention, _, _ = self._entry(intervention_id)
            return PAGE.replace("{{id}}", escape(intervention_id)) \
                       .replace("{{subject}}", escape(intervention.subject)) \
                       .replace("{{step}}", escape(f"{intervention.step_id}: {intervention.intent}")) \
                       .replace("{{reason}}", escape(intervention.reason)) \
                       .replace("{{snapshot}}", escape(intervention.snapshot[-4000:]))

        @app.get("/interventions/{intervention_id}/state")
        def state(intervention_id: str):
            if intervention_id in self.closed:
                return {"state": "closed", **self.closed[intervention_id]}
            intervention, session, _ = self._entry(intervention_id)
            return {**session.summary(), **_public(intervention)}

        @app.get("/interventions/{intervention_id}/screenshot")
        async def screenshot(intervention_id: str):
            # The live view for the person handling it. Shown, never stored.
            _, _, surface = self._entry(intervention_id)
            return Response(await surface.page.screenshot(), media_type="image/png")

        def control(intervention_id: str, verb: str, operator: str):
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

        for verb in ("claim", "heartbeat", "handback", "abort"):
            app.post(f"/interventions/{{intervention_id}}/{verb}")(
                lambda intervention_id, operator, _verb=verb: control(intervention_id, _verb, operator))
        return app


def _public(intervention: Intervention) -> dict:
    data = asdict(intervention)
    data.pop("screenshot", None)  # a local path; the page has the live view instead
    return data


PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Intervention {{id}}</title>
<style>
 body{font:14px system-ui;margin:16px;max-width:1100px} .row{display:flex;gap:16px;flex-wrap:wrap}
 img{max-width:640px;width:100%;border:1px solid #ccc} pre{background:#f5f5f5;padding:8px;max-height:300px;overflow:auto}
 button{font-size:14px;padding:6px 14px;margin-right:6px} #state{font-weight:600}
</style></head><body>
<h1>Needs a person</h1>
<p><b>{{subject}}</b><br>Step <code>{{step}}</code><br>Reason: {{reason}}</p>
<p>Control: <span id="state">…</span></p>
<p><input id="op" placeholder="your name" size="14">
 <button onclick="send('claim')">Take control</button>
 <button onclick="send('handback')">Hand back</button>
 <button onclick="send('abort')">Abort run</button></p>
<p>While you hold control, work in the automation's browser window. What you do there is recorded.</p>
<div class="row"><div><h3>Live session</h3><img id="shot"></div>
<div><h3>What you've done</h3><ol id="actions"></ol><h3>Screen when it paused</h3><pre>{{snapshot}}</pre></div></div>
<script>
const id = "{{id}}", op = document.getElementById("op");
try { op.value = localStorage.getItem("operator") || ""; } catch (e) {}
async function send(verb) {
  if (!op.value) { alert("Enter your name first"); return; }
  try { localStorage.setItem("operator", op.value); } catch (e) {}
  const r = await fetch(`/interventions/${id}/${verb}?operator=${encodeURIComponent(op.value)}`, {method: "POST"});
  if (!r.ok) alert((await r.json()).detail);
  refresh();
}
async function refresh() {
  const s = await (await fetch(`/interventions/${id}/state`)).json();
  document.getElementById("state").textContent = s.state === "closed" ? `closed (${s.closed})` : `${s.state}, held by ${s.holder}`;
  document.getElementById("actions").innerHTML = (s.human_actions || []).map(a => `<li>${a.action} ${a.role} “${a.name}”</li>`).join("");
  if (s.state !== "closed") document.getElementById("shot").src = `/interventions/${id}/screenshot?t=${Date.now()}`;
  if (s.state === "human" && op.value && s.holder === `operator:${op.value}`) fetch(`/interventions/${id}/heartbeat?operator=${encodeURIComponent(op.value)}`, {method: "POST"});
}
refresh(); setInterval(refresh, 1500);
</script></body></html>"""
