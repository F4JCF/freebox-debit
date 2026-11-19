"""Client minimal pour l'API Freebox OS (bibliothèque standard uniquement)."""
import hashlib
import hmac
import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.request

APP_ID = "fr.f4jcf.freeboxdebit"
APP_NAME = "Freebox Debit"
APP_VERSION = "1.0.1"


class FreeboxError(Exception):
    def __init__(self, msg, code=""):
        super().__init__(msg)
        self.code = code


def config_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "FreeboxDebit")
    os.makedirs(path, exist_ok=True)
    return path


def load_config():
    try:
        with open(os.path.join(config_dir(), "config.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    with open(os.path.join(config_dir(), "config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


class FreeboxClient:
    def __init__(self, host="mafreebox.freebox.fr", app_token=None, timeout=5):
        self.host = host
        self.app_token = app_token
        self.timeout = timeout
        self.session_token = None
        self.base = None  # ex: http://mafreebox.freebox.fr/api/v8/
        # Pas de proxy : la Freebox est sur le réseau local.
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # certificat auto-signé de la box (réseau local)
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=ctx),
        )

    # ---------- bas niveau ----------
    def _raw(self, url, data=None, method=None, auth=True):
        headers = {"Content-Type": "application/json"}
        if auth and self.session_token:
            headers["X-Fbx-App-Auth"] = self.session_token
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode("utf-8"))
            except ValueError:
                raise FreeboxError(f"HTTP {e.code} sur {url}")
        except (urllib.error.URLError, socket.timeout, OSError) as e:
            raise FreeboxError(f"Freebox injoignable ({self.host}) : {e}")

    def discover(self):
        last = None
        for scheme in ("http", "https"):
            try:
                info = self._raw(f"{scheme}://{self.host}/api_version", auth=False)
                major = str(info.get("api_version", "4.0")).split(".")[0]
                base_url = info.get("api_base_url", "/api/")
                self.base = f"{scheme}://{self.host}{base_url}v{major}/"
                self.box_model = info.get("box_model_name") or info.get("device_name") or "Freebox"
                return info
            except FreeboxError as e:
                last = e
        raise last or FreeboxError("Freebox introuvable")

    def call(self, path, data=None, method=None, auth=True, retry=True):
        if self.base is None:
            self.discover()
        res = self._raw(self.base + path, data, method, auth)
        if not res.get("success"):
            code = res.get("error_code", "")
            if retry and code in ("auth_required", "invalid_session") and self.app_token:
                self.login()
                return self.call(path, data, method, auth, retry=False)
            raise FreeboxError(f"{path} : {res.get('msg') or code or 'erreur'}", code)
        return res.get("result")

    # ---------- authentification ----------
    def request_authorization(self, device_name):
        """Demande un app_token. L'utilisateur doit valider sur l'écran de la box."""
        r = self.call("login/authorize/", {
            "app_id": APP_ID, "app_name": APP_NAME,
            "app_version": APP_VERSION, "device_name": device_name,
        }, auth=False)
        return r["app_token"], r["track_id"]

    def authorization_status(self, track_id):
        return self.call(f"login/authorize/{track_id}", auth=False).get("status")

    def login(self):
        if not self.app_token:
            raise FreeboxError("Application non autorisée")
        challenge = self.call("login/", auth=False, retry=False)["challenge"]
        password = hmac.new(self.app_token.encode(), challenge.encode(), hashlib.sha1).hexdigest()
        self.session_token = None
        r = self.call("login/session/", {
            "app_id": APP_ID, "app_version": APP_VERSION, "password": password,
        }, auth=False, retry=False)
        self.session_token = r["session_token"]
        return r.get("permissions", {})


# ---------------- collecte ----------------

BANDS = {"2d4g": "Wi-Fi 2,4 GHz", "5g": "Wi-Fi 5 GHz", "6g": "Wi-Fi 6 GHz", "60g": "Wi-Fi 60 GHz"}


def _host_ip(host):
    best = ""
    for c in _as_list(host.get("l3connectivities")):
        if c.get("af") == "ipv4" and c.get("addr"):
            if c.get("active") or not best:
                best = c["addr"]
    return best


def _as_list(v):
    """L'API renvoie parfois un objet seul au lieu d'une liste."""
    if isinstance(v, dict):
        return [v]
    if isinstance(v, list):
        return [x for x in v if isinstance(x, dict)]
    return []


def _d(v):
    return v if isinstance(v, dict) else {}


def _host_mac(host):
    for l2 in _as_list(host.get("l2ident")):
        if l2.get("id"):
            return str(l2["id"]).upper()
    hid = host.get("id", "")
    return hid.split("-", 1)[-1].upper() if hid else ""


def _num(v):
    try:
        v = int(v)
        return v if v > 0 else 0
    except (TypeError, ValueError):
        return 0


def collect(client):
    """Retourne un instantané : {'wan': {...}, 'devices': [...], 'warnings': [...]}.
    Débits en octets/seconde. 'down' = vers l'appareil, 'up' = depuis l'appareil."""
    warnings = []
    snap = {"time": time.time(), "devices": [], "warnings": warnings}

    conn = _d(client.call("connection/"))
    snap["wan"] = {
        "down": _num(conn.get("rate_down")), "up": _num(conn.get("rate_up")),
        "bw_down": _num(conn.get("bandwidth_down")), "bw_up": _num(conn.get("bandwidth_up")),
        "ipv4": conn.get("ipv4", ""), "state": conn.get("state", ""), "media": conn.get("media", ""),
    }

    hosts = {}
    try:
        for h in _as_list(client.call("lan/browser/pub/")):
            mac = _host_mac(h)
            if mac:
                hosts[mac] = {
                    "name": h.get("primary_name") or h.get("default_name") or mac,
                    "ip": _host_ip(h), "vendor": h.get("vendor_name", ""),
                    "type": h.get("host_type", ""), "active": h.get("active", False),
                }
    except Exception as e:
        warnings.append(f"Liste des appareils indisponible : {e}")

    def describe(mac, fallback=""):
        h = hosts.get((mac or "").upper(), {})
        return (h.get("name") or fallback or mac or "?"), h.get("ip", ""), h.get("vendor", "")

    # Wi-Fi : débit par appareil
    try:
        for ap in _as_list(client.call("wifi/ap/")):
            band = _d(ap.get("config")).get("band", "")
            label = BANDS.get(band, "Wi-Fi")
            try:
                stations = _as_list(client.call(f"wifi/ap/{ap.get('id')}/stations/"))
            except FreeboxError:
                stations = []
            for st in stations:
                mac = (st.get("mac") or "").upper()
                name, ip, vendor = describe(mac, st.get("hostname") or _d(st.get("host")).get("primary_name"))
                if not ip and _d(st.get("host")):
                    ip = _host_ip(st["host"])
                snap["devices"].append({
                    "key": "wifi:" + mac, "name": name, "ip": ip, "mac": mac, "vendor": vendor,
                    "link": label,
                    "down": _num(st.get("tx_rate")), "up": _num(st.get("rx_rate")),
                    "signal": st.get("signal"), "exact": True,
                })
    except Exception as e:
        warnings.append(f"Wi-Fi indisponible : {e}")

    # Ethernet : débit par port du switch (pas par appareil)
    try:
        for port in _as_list(client.call("switch/status/")):
            if port.get("link") != "up":
                continue
            pid = port.get("id")
            try:
                st = _d(client.call(f"switch/port/{pid}/stats/"))
            except FreeboxError:
                st = {}
            ml = _as_list(port.get("mac_list"))
            macs = [str(m.get("mac") or "").upper() for m in ml]
            # rx = reçu par la box (envoyé par l'appareil), tx = envoyé vers l'appareil
            down, up = _num(st.get("tx_bytes_rate")), _num(st.get("rx_bytes_rate"))
            label = f"Ethernet port {pid}"
            if len(macs) == 1:
                fb = ml[0].get("hostname") or ml[0].get("name")
                name, ip, vendor = describe(macs[0], fb)
                snap["devices"].append({
                    "key": f"eth:{pid}", "name": name, "ip": ip, "mac": macs[0], "vendor": vendor,
                    "link": label, "down": down, "up": up, "exact": True,
                })
            else:
                names = [describe(m)[0] for m in macs]
                ips = [describe(m)[1] for m in macs if describe(m)[1]]
                title = f"{len(macs)} appareils (switch)" if macs else "Appareil inconnu"
                snap["devices"].append({
                    "key": f"eth:{pid}", "name": title, "ip": ", ".join(ips), "mac": "",
                    "vendor": ", ".join(names), "link": label, "down": down, "up": up,
                    "exact": False,
                })
    except Exception as e:
        warnings.append(f"Ports Ethernet indisponibles : {e}")

    return snap
