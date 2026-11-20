"""HUD-Kalibrierung: Screenshot im Spiel machen und Bereiche mit der Maus markieren.

Läuft mit tkinter (bei Python für Windows und macOS dabei).
"""
from __future__ import annotations

import time

REGIONS = [
    ("score_ours", "Unser Score / our score"),
    ("score_enemy", "Gegner-Score / enemy score"),
    ("game_timer", "Spielzeit / game timer"),
    ("objective_timer", "Hill-/Objective-Timer"),
    ("killfeed", "Killfeed"),
    ("minimap", "Minimap (eigene oder Caster-Ansicht, genau den Kartenbereich)"),
]


def calibrate(cfg) -> None:
    import tkinter as tk

    import cv2
    from PIL import Image, ImageTk

    from .config import save_user_value
    from .vision.capture import ScreenCapture

    print("Wechsle ins Spiel (Match läuft, HUD sichtbar). Screenshot in 5 Sekunden ...")
    time.sleep(5)
    frame = ScreenCapture(cfg).grab()
    h, w = frame.shape[:2]
    scale = min(1.0, 1500 / w)
    img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)).resize((int(w * scale), int(h * scale)))
    vw, vh = img.size

    root = tk.Tk()
    root.title("Yvara AI – HUD-Kalibrierung")
    info = tk.Label(root, font=("Segoe UI", 13, "bold"))
    info.pack(fill="x")
    canvas = tk.Canvas(root, width=vw, height=vh, cursor="crosshair")
    canvas.pack()
    photo = ImageTk.PhotoImage(img)
    canvas.create_image(0, 0, anchor="nw", image=photo)
    state = {"i": 0, "start": None, "rect": None}

    def show():
        if state["i"] >= len(REGIONS):
            root.destroy()
            return
        info.config(text=f"Rahmen ziehen um: {REGIONS[state['i']][1]}   (Leertaste = überspringen)")

    def down(e):
        state["start"] = (e.x, e.y)
        state["rect"] = canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="#ff4655", width=2)

    def move(e):
        if state["rect"]:
            x0, y0 = state["start"]
            canvas.coords(state["rect"], x0, y0, e.x, e.y)

    def up(e):
        if not state["start"]:
            return
        x0, y0 = state["start"]
        x, y = min(x0, e.x), min(y0, e.y)
        rw, rh = abs(e.x - x0), abs(e.y - y0)
        state["start"] = None
        if rw > 3 and rh > 3:
            key = REGIONS[state["i"]][0]
            roi = [round(x / vw, 4), round(y / vh, 4), round(rw / vw, 4), round(rh / vh, 4)]
            save_user_value(cfg, f"hud.{key}", roi)
            print(f"  gespeichert: hud.{key} = {roi}")
            state["i"] += 1
            show()

    def skip(_e):
        print(f"  übersprungen: {REGIONS[state['i']][0]}")
        state["i"] += 1
        show()

    canvas.bind("<ButtonPress-1>", down)
    canvas.bind("<B1-Motion>", move)
    canvas.bind("<ButtonRelease-1>", up)
    root.bind("<space>", skip)
    show()
    root.mainloop()
    print("Fertig. Teste mit: python -m coach test-capture")
