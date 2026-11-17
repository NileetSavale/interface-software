"""The native client: the same screens as the canvas page, drawn in a Tk window, from the same server.

    uv run python -m apps.mock_gui.desktop --server http://127.0.0.1:4400

Each window is a fresh session, as reopening a desktop client is. It draws the server's spec (apps/mock_gui/app.py)
on a Tk canvas: no widgets with names, nothing an accessibility API could read, only pixels. Once the window is on
screen it prints "READY x y w h", where the drawing area is in screen points, for whoever is going to drive it.
"""
import argparse
import json
import sys
import tkinter as tk
import tkinter.font as tkfont
import urllib.request


class Client:
    def __init__(self, server: str):
        self.server = server.rstrip("/")
        self.root = tk.Tk()
        self.root.title("Core Banking")
        self.root.resizable(False, False)
        self.canvas = tk.Canvas(self.root, width=960, height=720, highlightthickness=0, bd=0)
        self.canvas.pack()
        self.sid = self._call("/api/session", {})["sid"]
        self.version, self.spec, self.values, self.focus, self.select_all = -1, None, {}, 0, False
        self.canvas.bind("<Button-1>", self.click)
        self.root.bind("<Key>", self.key)
        for combo in ("<Control-a>", "<Command-a>"):
            self.root.bind(combo, self.select)
        self.root.geometry("+60+60")
        self.root.after(100, self.poll)
        self.root.after(500, self.ready)

    def _call(self, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.server + path, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read())

    def ready(self):
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.focus_force()
        self.root.update_idletasks()
        print(f"READY {self.canvas.winfo_rootx()} {self.canvas.winfo_rooty()} 960 720", flush=True)

    def poll(self):
        try:
            screen = self._call(f"/api/screen?sid={self.sid}")
            if screen["version"] != self.version:
                self.version, self.spec = screen["version"], screen["spec"]
                self.values = {f["name"]: f["value"] for f in self.spec["fields"]}
                self.focus, self.select_all = 0, False
                self.draw()
        except OSError:
            pass
        self.root.after(100, self.poll)

    def draw(self):
        t, c = self.spec["theme"], self.canvas
        font = tkfont.Font(family=t["font"], size=-t["size"])
        c.delete("all")
        c.create_rectangle(0, 0, 960, 720, fill=t["bg"], width=0)
        c.create_rectangle(0, 0, 960, 42, fill=t["band"], width=0)
        for x in self.spec["texts"]:
            colour = t["bandText"] if x.get("band") else t["message"] if x.get("message") else t["text"]
            c.create_text(x["x"], x["y"], text=x["text"], anchor="nw", font=font, fill=colour)
        for i, f in enumerate(self.spec["fields"]):
            c.create_rectangle(f["x"], f["y"], f["x"] + f["w"] - 1, f["y"] + f["h"] - 1, fill=t["field"],
                               outline="#000000" if i == self.focus else t["fieldBorder"])
            v = self.values.get(f["name"], "")
            c.create_text(f["x"] + 6, f["y"] + 5, text="*" * len(v) if f["hidden"] else v, anchor="nw", font=font,
                          fill=t["text"])
        for b in self.spec["buttons"]:
            c.create_rectangle(b["x"], b["y"], b["x"] + b["w"] - 1, b["y"] + b["h"] - 1, fill=t["button"],
                               outline=t["buttonBorder"])
            c.create_text(b["x"] + 18, b["y"] + 9, text=b["label"], anchor="nw", font=font, fill=t["buttonText"])

    def press(self, key: str):
        self._call("/api/key", {"sid": self.sid, "key": key, "fields": self.values})

    def click(self, event):
        inside = lambda r: r["x"] <= event.x < r["x"] + r["w"] and r["y"] <= event.y < r["y"] + r["h"]  # noqa: E731
        for i, f in enumerate(self.spec["fields"]):
            if inside(f):
                self.focus, self.select_all = i, False
                self.draw()
                return
        for b in self.spec["buttons"]:
            if inside(b):
                self.press(b["key"])
                return

    def select(self, event):
        self.select_all = True
        return "break"

    def key(self, event):
        fields = self.spec["fields"] if self.spec else []
        f = fields[self.focus] if fields else None
        if event.keysym in ("ISO_Left_Tab", "Tab") and fields:
            back = event.keysym == "ISO_Left_Tab" or event.state & 0x1
            self.focus = (self.focus + (len(fields) - 1 if back else 1)) % len(fields)
            self.select_all = False
        elif event.keysym == "Return" or (event.keysym.startswith("F") and event.keysym[1:].isdigit()):
            name = "Enter" if event.keysym == "Return" else event.keysym
            if any(b["key"] == name for b in self.spec["buttons"]):
                self.press(name)
            return "break"
        elif event.keysym == "BackSpace" and f:
            self.values[f["name"]] = "" if self.select_all else self.values.get(f["name"], "")[:-1]
            self.select_all = False
        elif event.char and event.char.isprintable() and f and not event.state & 0x4:
            current = "" if self.select_all else self.values.get(f["name"], "")
            if len(current) < f["max"]:
                self.values[f["name"]] = current + event.char
            self.select_all = False
        else:
            return None
        self.draw()
        return "break"


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="http://127.0.0.1:4400")
    args = parser.parse_args(argv)
    Client(args.server).root.mainloop()


if __name__ == "__main__":
    sys.exit(main())
