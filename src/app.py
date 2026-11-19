"""Freebox Débit — qui consomme la bande passante ? (F4JCF)"""
import platform
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

import speedtest
from freebox_api import (APP_VERSION, FreeboxClient, FreeboxError, collect,
                         load_config, save_config)

REFRESH_S = 2.0
ALPHA = 0.5  # lissage des débits


def fmt_rate(bytes_per_s):
    return _fmt(bytes_per_s).replace(".", ",")


def _fmt(bytes_per_s):
    bits = bytes_per_s * 8
    if bits >= 1e9:
        return f"{bits / 1e9:.2f} Gbit/s"
    if bits >= 1e6:
        return f"{bits / 1e6:.1f} Mbit/s"
    if bits >= 1e3:
        return f"{bits / 1e3:.0f} kbit/s"
    return "0" if bits == 0 else f"{bits:.0f} bit/s"


def fmt_bits(bits):
    return fmt_rate(bits / 8)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"Freebox Débit {APP_VERSION} — qui consomme la bande passante ?")
        self.geometry("1020x600")
        self.minsize(820, 420)
        self.cfg = load_config()
        self.client = None
        self.q = queue.Queue()
        self.paused = False
        self.running = True
        self.smooth = {}
        self.peaks = {}
        self.last_snap = None
        self._build()
        self.after(200, self.start)
        self.after(300, self._drain)
        self.protocol("WM_DELETE_WINDOW", self._quit)

    # ---------------- interface ----------------
    def _build(self):
        style = ttk.Style(self)
        try:
            style.theme_use("vista" if platform.system() == "Windows" else "clam")
        except tk.TclError:
            pass
        style.configure("Big.TLabel", font=("Segoe UI", 14, "bold"))
        style.configure("Treeview", rowheight=24, font=("Segoe UI", 10))
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))

        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")
        ttk.Label(top, text="Internet", style="Big.TLabel").grid(row=0, column=0, rowspan=2, padx=(0, 16))
        self.lbl_down = ttk.Label(top, text="↓ —", font=("Segoe UI", 12))
        self.lbl_down.grid(row=0, column=1, sticky="w")
        self.bar_down = ttk.Progressbar(top, length=280, maximum=100)
        self.bar_down.grid(row=0, column=2, padx=8)
        self.lbl_up = ttk.Label(top, text="↑ —", font=("Segoe UI", 12))
        self.lbl_up.grid(row=1, column=1, sticky="w")
        self.bar_up = ttk.Progressbar(top, length=280, maximum=100)
        self.bar_up.grid(row=1, column=2, padx=8)
        self.lbl_wan = ttk.Label(top, text="", foreground="#555")
        self.lbl_wan.grid(row=0, column=3, rowspan=2, sticky="w", padx=8)

        btns = ttk.Frame(top)
        btns.grid(row=0, column=4, rowspan=2, sticky="e")
        top.columnconfigure(4, weight=1)
        ttk.Button(btns, text="Tester le débit", command=self.open_speedtest).pack(side="left", padx=3)
        self.btn_pause = ttk.Button(btns, text="Pause", command=self.toggle_pause)
        self.btn_pause.pack(side="left", padx=3)
        ttk.Button(btns, text="Réinit. pics", command=self.reset_peaks).pack(side="left", padx=3)
        ttk.Button(btns, text="Adresse box…", command=self.change_host).pack(side="left", padx=3)

        self.alert = ttk.Label(self, text="", font=("Segoe UI", 11, "bold"), foreground="#b00020", padding=(10, 0))
        self.alert.pack(fill="x")

        cols = ("name", "ip", "link", "down", "up", "share", "peak")
        heads = ("Appareil", "Adresse IP", "Connexion", "↓ Réception", "↑ Envoi", "Part ↓ Internet", "Pic ↓")
        widths = (250, 150, 130, 110, 110, 110, 110)
        frame = ttk.Frame(self, padding=(10, 4))
        frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(frame, columns=cols, show="headings")
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="w" if c in ("name", "ip", "link") else "e")
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("hog", background="#ffd6d6")
        self.tree.tag_configure("busy", background="#fff1c9")
        self.tree.tag_configure("idle", foreground="#888")
        self.tree.bind("<Double-1>", self.show_details)

        note = ("Wi-Fi : débit mesuré par appareil.  Ethernet : débit du port de la box "
                "(si un switch est branché sur un port, ses appareils sont regroupés).  "
                "Ces débits incluent aussi le trafic local (ex. PC ↔ NAS).")
        ttk.Label(self, text=note, foreground="#555", wraplength=980, padding=(10, 2)).pack(fill="x")
        self.status = ttk.Label(self, text="Démarrage…", relief="sunken", padding=4)
        self.status.pack(fill="x", side="bottom")

    # ---------------- connexion ----------------
    def start(self):
        host = self.cfg.get("host", "mafreebox.freebox.fr")
        self.client = FreeboxClient(host, self.cfg.get("app_token"))
        if not self.cfg.get("app_token"):
            self.authorize()
        else:
            threading.Thread(target=self._poll_loop, daemon=True).start()

    def authorize(self):
        win = tk.Toplevel(self)
        win.title("Autoriser l'application")
        win.transient(self)
        win.grab_set()
        msg = tk.StringVar(value=(
            "Première utilisation : la Freebox doit autoriser ce programme.\n\n"
            "Ce PC doit être branché sur le réseau de la Freebox.\n"
            "Cliquez sur « Demander l'autorisation », puis validez la demande\n"
            "sur l'écran du Freebox Server Delta (réponse « Oui »)."))
        ttk.Label(win, textvariable=msg, padding=16, justify="left").pack()
        btn = ttk.Button(win, text="Demander l'autorisation")
        btn.pack(pady=(0, 16))

        def worker():
            try:
                token, track = self.client.request_authorization(platform.node() or "PC")
                self.q.put(("authmsg", "➡ Validez maintenant sur l'écran de la Freebox…"))
                for _ in range(180):
                    status = self.client.authorization_status(track)
                    if status == "granted":
                        self.q.put(("granted", token))
                        return
                    if status in ("denied", "timeout", "unknown"):
                        self.q.put(("authmsg", f"Autorisation refusée ou expirée ({status}). Réessayez."))
                        self.q.put(("authretry", None))
                        return
                    time.sleep(1)
                self.q.put(("authmsg", "Délai dépassé. Réessayez."))
                self.q.put(("authretry", None))
            except FreeboxError as e:
                self.q.put(("authmsg", f"Erreur : {e}"))
                self.q.put(("authretry", None))

        def go():
            btn.state(["disabled"])
            msg.set("Envoi de la demande à la Freebox…")
            threading.Thread(target=worker, daemon=True).start()

        btn.configure(command=go)
        self._auth = (win, msg, btn)

    def _poll_loop(self):
        logged = False
        while self.running:
            if self.paused:
                time.sleep(0.3)
                continue
            try:
                if not logged:
                    self.client.login()
                    logged = True
                self.q.put(("snap", collect(self.client)))
            except FreeboxError as e:
                logged = False
                txt = str(e)
                if e.code in ("invalid_token", "pending_token", "denied_from_external_ip") \
                        or not self.client.app_token:
                    self.q.put(("needauth", txt))
                    return
                self.q.put(("error", txt))
                time.sleep(3)
            except Exception as e:  # sécurité : ne jamais tuer la boucle
                self.q.put(("error", f"{type(e).__name__} : {e}"))
                time.sleep(3)
            time.sleep(REFRESH_S)

    def _drain(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "snap":
                    self.render(data)
                elif kind == "error":
                    self.status.configure(text=f"⚠ {data}")
                elif kind == "authmsg" and hasattr(self, "_auth"):
                    self._auth[1].set(data)
                elif kind == "authretry" and hasattr(self, "_auth"):
                    self._auth[2].state(["!disabled"])
                elif kind == "granted":
                    self.cfg["app_token"] = data
                    self.cfg.setdefault("host", self.client.host)
                    save_config(self.cfg)
                    self.client.app_token = data
                    if hasattr(self, "_auth"):
                        self._auth[0].destroy()
                        del self._auth
                    messagebox.showinfo("Freebox Débit", "Autorisation accordée. Surveillance lancée.")
                    threading.Thread(target=self._poll_loop, daemon=True).start()
                elif kind == "needauth":
                    self.cfg.pop("app_token", None)
                    save_config(self.cfg)
                    self.client.app_token = None
                    self.status.configure(text="Autorisation révoquée : nouvelle demande nécessaire.")
                    self.authorize()
                elif kind == "speed":
                    data()
        except queue.Empty:
            pass
        self.after(200, self._drain)

    # ---------------- affichage ----------------
    def render(self, snap):
        self.last_snap = snap
        wan = snap["wan"]
        bw_d, bw_u = wan["bw_down"], wan["bw_up"]  # bit/s
        self.lbl_down.configure(text=f"↓ {fmt_rate(wan['down'])}")
        self.lbl_up.configure(text=f"↑ {fmt_rate(wan['up'])}")
        self.bar_down["value"] = min(100, wan["down"] * 8 * 100 / bw_d) if bw_d else 0
        self.bar_up["value"] = min(100, wan["up"] * 8 * 100 / bw_u) if bw_u else 0
        cap = f"Capacité ligne : ↓ {fmt_bits(bw_d)} / ↑ {fmt_bits(bw_u)}" if bw_d else ""
        self.lbl_wan.configure(text=f"{cap}\nIP publique : {wan['ipv4']}  ({wan['state']})")

        rows = []
        for d in snap["devices"]:
            k = d["key"]
            prev = self.smooth.get(k)
            if prev:
                d["down"] = ALPHA * d["down"] + (1 - ALPHA) * prev[0]
                d["up"] = ALPHA * d["up"] + (1 - ALPHA) * prev[1]
            self.smooth[k] = (d["down"], d["up"])
            self.peaks[k] = max(self.peaks.get(k, 0), d["down"])
            rows.append(d)
        rows.sort(key=lambda d: d["down"] + d["up"], reverse=True)

        self.tree.delete(*self.tree.get_children())
        wan_down = max(wan["down"], 1)
        hog = None
        for d in rows:
            share = min(100, d["down"] * 100 / wan_down) if wan["down"] > 0 else 0
            tag = "idle"
            if d["down"] + d["up"] > 125_000:  # > 1 Mbit/s
                tag = "busy"
            if (share >= 40 and d["down"] > 625_000) or (bw_d and d["down"] * 8 > 0.5 * bw_d):
                tag = "hog"
                hog = hog or (d, share)
            self.tree.insert("", "end", iid=d["key"], tags=(tag,), values=(
                d["name"], d["ip"] or "—", d["link"], fmt_rate(d["down"]), fmt_rate(d["up"]),
                f"{share:.0f} %" if wan["down"] > 0 and d["down"] > 0 else "", fmt_rate(self.peaks[d["key"]])))
        if hog:
            d, share = hog
            self.alert.configure(text=f"⚠ Gros consommateur : {d['name']}  ({d['ip'] or d['link']})  —  "
                                      f"{fmt_rate(d['down'])}, environ {share:.0f} % du débit Internet reçu")
        else:
            self.alert.configure(text="")
        msg = f"Mis à jour à {time.strftime('%H:%M:%S')} — {len(rows)} appareils actifs"
        if snap["warnings"]:
            msg += "   ⚠ " + " | ".join(snap["warnings"])
        self.status.configure(text=msg)

    def show_details(self, _evt):
        sel = self.tree.selection()
        if not sel or not self.last_snap:
            return
        d = next((x for x in self.last_snap["devices"] if x["key"] == sel[0]), None)
        if not d:
            return
        lines = [f"Appareil : {d['name']}", f"Adresse IP : {d['ip'] or '—'}", f"Connexion : {d['link']}"]
        if d.get("mac"):
            lines.append(f"Adresse MAC : {d['mac']}")
        if d.get("vendor"):
            lines.append(("Appareils : " if not d.get("exact") else "Fabricant : ") + d["vendor"])
        if d.get("signal") is not None:
            lines.append(f"Signal Wi-Fi : {d['signal']} dB")
        if not d.get("exact"):
            lines.append("\nPlusieurs appareils partagent ce port : le débit affiché est leur total.")
        messagebox.showinfo("Détails", "\n".join(lines))

    # ---------------- actions ----------------
    def toggle_pause(self):
        self.paused = not self.paused
        self.btn_pause.configure(text="Reprendre" if self.paused else "Pause")

    def reset_peaks(self):
        self.peaks.clear()

    def change_host(self):
        cur = self.cfg.get("host", "mafreebox.freebox.fr")
        new = simpledialog.askstring("Adresse de la Freebox",
                                     "Adresse de la box (par défaut mafreebox.freebox.fr ou 192.168.1.254) :",
                                     initialvalue=cur, parent=self)
        if new and new.strip() != cur:
            self.cfg["host"] = new.strip()
            save_config(self.cfg)
            messagebox.showinfo("Freebox Débit", "Adresse enregistrée. Relancez le programme.")

    def open_speedtest(self):
        win = tk.Toplevel(self)
        win.title("Test de débit")
        win.transient(self)
        info = tk.StringVar(value=("Mesure le débit réel entre ce PC et Internet (serveurs Cloudflare).\n"
                                   "Pour un résultat fiable : PC en Ethernet, autres appareils au repos.\n"
                                   "Pendant le test, regardez la fenêtre principale : si un autre appareil\n"
                                   "consomme déjà beaucoup, le résultat sera plus bas."))
        ttk.Label(win, textvariable=info, padding=12, justify="left").pack()
        pb = ttk.Progressbar(win, length=420, maximum=100)
        pb.pack(padx=12)
        live = ttk.Label(win, text="", font=("Segoe UI", 12, "bold"), padding=8)
        live.pack()
        res = ttk.Label(win, text="", font=("Segoe UI", 11), padding=8, justify="left")
        res.pack()
        btn = ttk.Button(win, text="Lancer le test")
        btn.pack(pady=10)

        def ui(f):
            self.q.put(("speed", f))

        def prog(label, frac, bps):
            base = 0 if label == "Réception" else 50
            ui(lambda: (pb.configure(value=base + frac * 50), live.configure(text=f"{label} : {fmt_bits(bps)}")))

        def worker():
            try:
                ui(lambda: live.configure(text="Latence…"))
                p = speedtest.ping()
                d = speedtest.download(progress=prog)
                u = speedtest.upload(progress=prog)
                wan = (self.last_snap or {}).get("wan", {})
                txt = f"Latence : {p:.0f} ms\nRéception : {fmt_bits(d)}\nEnvoi : {fmt_bits(u)}"
                if wan.get("bw_down"):
                    txt += (f"\n\nCapacité annoncée par la Freebox : ↓ {fmt_bits(wan['bw_down'])}"
                            f" / ↑ {fmt_bits(wan['bw_up'])}")
                txt += "\n(Un PC en Gigabit plafonne vers 940 Mbit/s, en Wi-Fi souvent moins.)"
                ui(lambda: (pb.configure(value=100), live.configure(text="Terminé"), res.configure(text=txt),
                            btn.state(["!disabled"])))
            except Exception as e:
                err = str(e)
                ui(lambda: (live.configure(text=f"Échec du test : {err}"), btn.state(["!disabled"])))

        def go():
            btn.state(["disabled"])
            res.configure(text="")
            threading.Thread(target=worker, daemon=True).start()

        btn.configure(command=go)

    def _quit(self):
        self.running = False
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
