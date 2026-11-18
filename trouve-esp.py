#!/usr/bin/env python3
"""
trouve-esp : retrouve sur le reseau local tes modules (LED, RGB 12 V,
panneaux), tes prises Shelly et Wemo, et tout objet qui y ressemble.

Aucune bibliotheque a installer (Python 3.8+ standard).

  trouve-esp                  balaye le reseau et affiche les objets reconnus
  trouve-esp --tout           affiche aussi les autres appareils (telephones...)
  trouve-esp --surveille      attend qu'un nouvel appareil se connecte
                              (lance-le, PUIS branche ton module)
  trouve-esp led-rgb panneau  resout directement ces noms (.local)
  trouve-esp --reseau 192.168.1.0/24   force le reseau a balayer

Comment il reconnait les appareils (sondes HTTP, sans rien modifier) :
  - modules de ce projet : GET /json/info  -> "ver": "chambre-..."
  - WLED officiel        : GET /json/info  -> "brand": "WLED"
  - Shelly               : GET /shelly     -> "gen", "model", "id"
  - Wemo                 : GET :4915x/setup.xml -> friendlyName
  - panneau 1            : GET /  -> redirection vers /login
  - panneau 2            : GET /  -> redirection vers http://<panneau 1>/
"""
import argparse
import concurrent.futures as cf
import ipaddress
import json
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

WEMO_PORTS = (49153, 49152, 49154, 49155)
CONNECT_TIMEOUT = 0.8
HTTP_TIMEOUT = 2.5
WORKERS = 96                 # macOS limite a 256 fichiers ouverts par defaut

USE_COLOR = sys.stdout.isatty()


def color(text, code):
    return "\033[%sm%s\033[0m" % (code, text) if USE_COLOR else text


# ---------------------------------------------------------------------------
#  Reseau
# ---------------------------------------------------------------------------
def local_ip():
    """IP de ce Mac sur le reseau local (aucun paquet n'est envoye)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        return s.getsockname()[0]
    finally:
        s.close()


def tcp_state(ip, port, timeout=CONNECT_TIMEOUT):
    """'open', 'closed' (l'hote a repondu : il existe) ou None (silence)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        err = s.connect_ex((ip, port))
    except OSError:
        return None
    finally:
        s.close()
    if err == 0:
        return "open"
    if err in (61, 111, 10061):            # ECONNREFUSED (macOS, Linux, Windows)
        return "closed"
    return None


def arp_table():
    """{ip: mac} lu dans la table ARP du systeme (rempli par le balayage)."""
    try:
        out = subprocess.run(["arp", "-an"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    table = {}
    for ip, mac in re.findall(r"\(([\d.]+)\) at ([0-9a-fA-F:]+)", out):
        parts = mac.lower().split(":")
        if len(parts) == 6:
            table[ip] = ":".join(p.zfill(2) for p in parts)
    return table


def reverse_name(ip):
    try:
        return socket.gethostbyaddr(ip)[0]
    except OSError:
        return ""


# ---------------------------------------------------------------------------
#  Identification
# ---------------------------------------------------------------------------
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(NoRedirect)


def http_get(url):
    """(code, en-tetes, corps) ; ne suit pas les redirections."""
    req = urllib.request.Request(url, headers={"User-Agent": "trouve-esp"})
    try:
        with _opener.open(req, timeout=HTTP_TIMEOUT) as r:
            return r.status, r.headers, r.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.headers, ""
    except (OSError, ValueError):
        return None, None, ""


def get_json(url):
    code, _, body = http_get(url)
    if code != 200:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


def identify(ip, open_ports):
    """Renvoie (categorie, nom, detail) ou None si rien de connu."""
    if 80 in open_ports:
        info = get_json("http://%s/json/info" % ip)
        if isinstance(info, dict):
            ver = str(info.get("ver", ""))
            name = str(info.get("name", ""))
            if ver.startswith("chambre-rgb12v"):
                return "Module RGB 12 V", name, ver
            if ver.startswith("chambre-led"):
                count = (info.get("leds") or {}).get("count", "?")
                return "Module LED WS2812B", name, "%s, %s LED" % (ver, count)
            if info.get("brand") == "WLED":
                return "WLED", name, "version " + ver
        sh = get_json("http://%s/shelly" % ip)
        if isinstance(sh, dict) and ("gen" in sh or "type" in sh):
            gen = sh.get("gen", 1)
            model = sh.get("model") or sh.get("type") or "?"
            name = sh.get("name") or sh.get("id") or ""
            app = sh.get("app", "")
            return "Shelly Gen%s" % gen, str(name), "%s %s" % (model, app)
        code, headers, body = http_get("http://%s/" % ip)
        loc = (headers.get("Location", "") if headers else "") or ""
        if code in (301, 302, 303, 307) and loc.rstrip("/").endswith("/login"):
            return "Panneau 1 (principal)", "", "interface : http://%s/" % ip
        if code in (301, 302, 303, 307) and loc.startswith("http://"):
            return "Panneau 2 (satellite)", "", "renvoie vers " + loc
        if code == 200 and body.startswith(("Module LED", "Module RGB")):
            return "Module LED", body.split(" - ")[0], ""
        if code is not None:
            m = re.search(r"<title>([^<]{1,60})</title>", body, re.I)
            return "Serveur web", m.group(1).strip() if m else "", "HTTP %s" % code
    for port in WEMO_PORTS:
        if port in open_ports:
            code, _, body = http_get("http://%s:%d/setup.xml" % (ip, port))
            if code == 200 and "Belkin" in body:
                fn = re.search(r"<friendlyName>([^<]*)</friendlyName>", body)
                mn = re.search(r"<modelName>([^<]*)</modelName>", body)
                return ("Wemo", fn.group(1) if fn else "",
                        "%s, port %d" % (mn.group(1) if mn else "Belkin", port))
    return None


# ---------------------------------------------------------------------------
#  Balayage
# ---------------------------------------------------------------------------
def sweep(network, with_wemo=True):
    """Hotes vivants -> ports ouverts. Phase 1 : port 80 sur tout le reseau.
    Phase 2 : ports Wemo, seulement sur les hotes qui ont repondu."""
    hosts = [str(h) for h in network.hosts()]
    host_set = set(hosts)
    alive = {}
    with cf.ThreadPoolExecutor(WORKERS) as ex:
        for ip, st in zip(hosts, ex.map(lambda h: tcp_state(h, 80), hosts)):
            if st:
                alive[ip] = {80} if st == "open" else set()
        if with_wemo:
            # un Wemo ne repond pas toujours sur le port 80 : on ajoute les
            # hotes vus par la table ARP (remplie par la phase 1)
            for ip in arp_table():
                if ip in host_set and ip not in alive:
                    alive[ip] = set()
            jobs = {ex.submit(tcp_state, ip, p): (ip, p) for ip in alive for p in WEMO_PORTS}
            for f in cf.as_completed(jobs):
                ip, p = jobs[f]
                if f.result() == "open":
                    alive[ip].add(p)
    return alive


def describe(ips, ports, macs, ex):
    """Identifie et nomme une liste d'hotes en parallele."""
    ids = dict(zip(ips, ex.map(lambda ip: identify(ip, ports.get(ip, set())), ips)))
    names = dict(zip(ips, ex.map(reverse_name, ips)))
    rows = []
    for ip in ips:
        rows.append({"ip": ip, "id": ids[ip], "dns": names[ip], "mac": macs.get(ip, "")})
    return rows


def ip_key(ip):
    return tuple(int(x) for x in ip.split("."))


def print_rows(rows, show_all):
    known = [r for r in rows if r["id"] and r["id"][0] != "Serveur web"]
    others = [r for r in rows if not (r["id"] and r["id"][0] != "Serveur web")]
    if known:
        print(color("Objets reconnus", "1"))
        for r in sorted(known, key=lambda r: ip_key(r["ip"])):
            cat, name, detail = r["id"]
            label = name or r["dns"].split(".")[0]
            print("  %-15s  %-22s  %-20s  %s" % (r["ip"], color(cat, "36"), label, color(detail, "2")))
            if r["mac"]:
                print("  %-15s  %s" % ("", color("MAC " + r["mac"] + ("   " + r["dns"] if r["dns"] else ""), "2")))
    else:
        print("Aucun objet reconnu sur ce reseau.")
    if show_all and others:
        print()
        print(color("Autres appareils", "1"))
        for r in sorted(others, key=lambda r: ip_key(r["ip"])):
            extra = "%s %s" % (r["id"][0], r["id"][1]) if r["id"] else ""
            print("  %-15s  %-17s  %-28s  %s" % (r["ip"], r["mac"] or "-", r["dns"] or "-", color(extra, "2")))


def notify(title, text):
    """Notification macOS (silencieux ailleurs)."""
    if sys.platform != "darwin":
        return
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    subprocess.run(["osascript", "-e", 'display notification "%s" with title "%s" sound name "Glass"'
                    % (esc(text), esc(title))], capture_output=True)


def watch(network):
    print("Surveillance de %s. Branche ton module maintenant (Ctrl+C pour arreter)." % network)
    known = set(sweep(network, with_wemo=False)) | set(arp_table())
    print(color("%d appareils deja presents, on attend les nouveaux..." % len(known), "2"))
    with cf.ThreadPoolExecutor(16) as ex:
        while True:
            time.sleep(3)
            ports = sweep(network, with_wemo=False)
            macs = arp_table()
            current = set(ports) | {ip for ip in macs if ipaddress.ip_address(ip) in network}
            new = sorted(current - known, key=ip_key)
            if not new:
                continue
            for ip in new:                 # laisse le temps au serveur web de demarrer
                ports.setdefault(ip, set())
            time.sleep(2)
            for ip in new:
                for p in (80,) + WEMO_PORTS:
                    if tcp_state(ip, p) == "open":
                        ports[ip].add(p)
            for r in describe(new, ports, arp_table(), ex):
                cat, name, detail = r["id"] or ("Appareil", "", "aucun service web reconnu")
                stamp = time.strftime("%H:%M:%S")
                print("%s  %s  %-15s  %s  %s  %s" % (color(stamp, "2"), color("NOUVEAU", "32;1"), r["ip"],
                                                    color(cat, "36"), name or r["dns"], color(r["mac"], "2")))
                notify("Nouvel appareil : " + cat, "%s  %s" % (r["ip"], name or r["mac"]))
            known |= set(new)


def resolve(names):
    code = 0
    for n in names:
        host = n if "." in n else n + ".local"
        try:
            ip = socket.gethostbyname(host)
            print("%-24s %s" % (host, ip))
        except OSError:
            print("%-24s %s" % (host, color("introuvable", "31")))
            code = 1
    return code


def main():
    ap = argparse.ArgumentParser(description="Retrouve tes objets connectes sur le reseau local.")
    ap.add_argument("noms", nargs="*", help="noms a resoudre directement (ex : led-rgb panneau)")
    ap.add_argument("--tout", action="store_true", help="affiche aussi les appareils non reconnus")
    ap.add_argument("--surveille", action="store_true", help="signale les appareils qui arrivent")
    ap.add_argument("--reseau", help="reseau a balayer, ex : 192.168.1.0/24")
    a = ap.parse_args()

    if a.noms:
        return resolve(a.noms)

    if a.reseau:
        network = ipaddress.ip_network(a.reseau, strict=False)
    else:
        network = ipaddress.ip_network(local_ip() + "/24", strict=False)
    if network.num_addresses > 1024:
        sys.exit("Reseau trop grand (%s) : precise --reseau en /24." % network)

    try:
        if a.surveille:
            watch(network)
            return 0
        t0 = time.time()
        print(color("Balayage de %s..." % network, "2"))
        ports = sweep(network)
        macs = arp_table()
        ips = sorted(set(ports) | ({ip for ip in macs if ipaddress.ip_address(ip) in network} if a.tout else set()),
                     key=ip_key)
        with cf.ThreadPoolExecutor(32) as ex:
            rows = describe(ips, ports, macs, ex)
        print_rows(rows, a.tout)
        print(color("\n%d appareils actifs, %.1f s." % (len(ips), time.time() - t0), "2"))
    except KeyboardInterrupt:
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
