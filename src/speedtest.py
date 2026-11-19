"""Test de débit Internet depuis ce PC (serveurs speed.cloudflare.com)."""
import os
import ssl
import threading
import time
import urllib.request

DOWN_URL = "https://speed.cloudflare.com/__down?bytes={n}"
UP_URL = "https://speed.cloudflare.com/__up"
PING_URL = "https://speed.cloudflare.com/__down?bytes=0"
UA = {"User-Agent": "FreeboxDebit/1.0"}


def _ctx():
    return ssl.create_default_context()


def ping(samples=5):
    best = None
    for _ in range(samples):
        t = time.perf_counter()
        req = urllib.request.Request(PING_URL, headers=UA)
        with urllib.request.urlopen(req, timeout=10, context=_ctx()) as r:
            r.read()
        dt = (time.perf_counter() - t) * 1000
        best = dt if best is None else min(best, dt)
    return best


def _measure(worker, duration, streams, progress, label):
    total = [0]
    lock = threading.Lock()
    stop = time.perf_counter() + duration

    def add(n):
        with lock:
            total[0] += n

    threads = [threading.Thread(target=worker, args=(stop, add), daemon=True) for _ in range(streams)]
    start = time.perf_counter()
    for th in threads:
        th.start()
    while any(th.is_alive() for th in threads):
        time.sleep(0.25)
        el = time.perf_counter() - start
        if progress and el > 0:
            progress(label, min(el / duration, 1.0), total[0] * 8 / el)
        if el > duration + 5:
            break
    el = time.perf_counter() - start
    return total[0] * 8 / el if el else 0.0  # bits/s


def download(duration=8, streams=6, progress=None):
    def worker(stop, add):
        while time.perf_counter() < stop:
            try:
                req = urllib.request.Request(DOWN_URL.format(n=50_000_000), headers=UA)
                with urllib.request.urlopen(req, timeout=15, context=_ctx()) as r:
                    while time.perf_counter() < stop:
                        chunk = r.read(65536)
                        if not chunk:
                            break
                        add(len(chunk))
            except OSError:
                time.sleep(0.2)
    return _measure(worker, duration, streams, progress, "Réception")


def upload(duration=8, streams=4, progress=None):
    block = os.urandom(1_000_000)

    def worker(stop, add):
        while time.perf_counter() < stop:
            try:
                req = urllib.request.Request(UP_URL, data=block, headers=dict(UA, **{
                    "Content-Type": "application/octet-stream"}), method="POST")
                with urllib.request.urlopen(req, timeout=15, context=_ctx()) as r:
                    r.read()
                if time.perf_counter() < stop + 1:
                    add(len(block))
            except OSError:
                time.sleep(0.2)
    return _measure(worker, duration, streams, progress, "Envoi")
