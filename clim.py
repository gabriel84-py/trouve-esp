#!/usr/bin/env python3
"""
clim : pilote la climatisation via le Sensibo Sky (API officielle Sensibo).

Le Sensibo Sky n'a pas d'API locale : il recoit ses ordres par le cloud
Sensibo, puis les envoie au climatiseur en infrarouge, comme la
telecommande. Il faut donc une cle d'API (gratuite) :
  https://home.sensibo.com/me/api  -> "Add API Key"
puis, une seule fois :
  clim cle TA_CLE_API          (enregistree dans ~/.config/sensibo/cle, lisible par toi seul)

Exemples :
  clim                 etat : marche/arret, mode, consigne, temperature et humidite de la piece
  clim on | off        allume / eteint
  clim 22              allume et regle la consigne a 22 degres
  clim froid 24        mode + consigne en une commande (froid, chaud, auto, seche, ventil)
  clim ventilo auto    vitesse du ventilateur (les valeurs dependent du climatiseur :
                       "clim modes" les affiche)
  clim modes           ce que ton climatiseur accepte (modes, consignes, ventilation)
  clim --piece Salon on   choisit l'appareil si tu en as plusieurs

Aucune bibliotheque a installer (Python 3.8+ standard).
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://home.sensibo.com/api/v2"
KEY_FILE = os.path.expanduser("~/.config/sensibo/cle")
MODES = {"froid": "cool", "chaud": "heat", "auto": "auto", "seche": "dry", "sec": "dry",
         "ventil": "fan", "ventilation": "fan",
         "cool": "cool", "heat": "heat", "dry": "dry", "fan": "fan"}
MODES_FR = {"cool": "froid", "heat": "chaud", "auto": "auto", "dry": "seche", "fan": "ventilation"}


class ApiError(Exception):
    pass


# ---------------------------------------------------------------------------
#  Cle d'API
# ---------------------------------------------------------------------------
def load_key():
    key = os.environ.get("SENSIBO_API_KEY", "").strip()
    if key:
        return key
    try:
        with open(KEY_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        sys.exit("Pas de cle d'API. Cree-la sur https://home.sensibo.com/me/api puis : clim cle TA_CLE")


def save_key(key):
    os.makedirs(os.path.dirname(KEY_FILE), mode=0o700, exist_ok=True)
    fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key.strip() + "\n")
    print("Cle enregistree dans", KEY_FILE)


# ---------------------------------------------------------------------------
#  Appels a l'API
# ---------------------------------------------------------------------------
def call(method, path, key, params=None, body=None):
    q = {"apiKey": key}
    q.update(params or {})
    url = API + path + "?" + urllib.parse.urlencode(q)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            reply = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            reason = json.loads(e.read().decode()).get("reason", "")
        except ValueError:
            reason = ""
        if e.code in (401, 403):
            raise ApiError("cle d'API refusee (%s). Verifie-la sur https://home.sensibo.com/me/api" % e.code)
        if e.code == 429:
            raise ApiError("trop de requetes, reessaie dans une minute")
        raise ApiError("HTTP %s %s" % (e.code, reason))
    except urllib.error.URLError as e:
        raise ApiError("pas de connexion a Sensibo (%s)" % e.reason)
    if reply.get("status") != "success":
        raise ApiError(reply.get("reason", "reponse inattendue"))
    return reply.get("result")


def pods(key):
    fields = "id,room,acState,measurements,remoteCapabilities,connectionStatus"
    return call("GET", "/users/me/pods", key, {"fields": fields})


def pick(all_pods, room):
    if not all_pods:
        raise ApiError("aucun Sensibo sur ce compte")
    if room is None:
        if len(all_pods) > 1:
            names = ", ".join(p["room"]["name"] for p in all_pods)
            print("Plusieurs appareils (%s) : %s pris par defaut, --piece pour choisir." % (names, all_pods[0]["room"]["name"]))
        return all_pods[0]
    for p in all_pods:
        if p["room"]["name"].lower() == room.lower():
            return p
    raise ApiError("piece introuvable : " + room)


def set_state(key, pod, **changes):
    """Envoie l'etat complet : un seul ordre infrarouge, meme pour plusieurs changements."""
    state = dict(pod.get("acState") or {})
    state.update(changes)
    keep = ("on", "mode", "targetTemperature", "temperatureUnit", "fanLevel", "swing", "horizontalSwing", "light")
    state = {k: v for k, v in state.items() if k in keep and v is not None}
    # Chaque mode a ses propres reglages : en changeant de mode, on retire ce
    # que le nouveau mode ne connait pas (sinon Sensibo refuse l'ordre).
    cap = ((pod.get("remoteCapabilities") or {}).get("modes", {}) or {}).get(state.get("mode"))
    if cap:
        temps = (cap.get("temperatures") or {}).get(state.get("temperatureUnit", "C"), {}).get("values", [])
        if not temps:
            state.pop("targetTemperature", None)
            state.pop("temperatureUnit", None)
        elif state.get("targetTemperature") not in temps:
            t = state.get("targetTemperature", temps[len(temps) // 2])
            state["targetTemperature"] = min(temps, key=lambda v: abs(v - t))
        for field, plural in (("fanLevel", "fanLevels"), ("swing", "swing"), ("horizontalSwing", "horizontalSwing")):
            allowed = cap.get(plural)
            if allowed is None:
                state.pop(field, None)
            elif state.get(field) not in allowed:
                state[field] = "auto" if "auto" in allowed else allowed[0]
    call("POST", "/pods/%s/acStates" % pod["id"], key, body={"acState": state})
    pod["acState"] = state
    return state


# ---------------------------------------------------------------------------
#  Affichage
# ---------------------------------------------------------------------------
def describe(pod):
    s = pod.get("acState") or {}
    m = pod.get("measurements") or {}
    unit = s.get("temperatureUnit", "C")
    line = "%s : %s" % (pod["room"]["name"], "MARCHE" if s.get("on") else "arret")
    if s.get("on"):
        line += ", %s" % MODES_FR.get(s.get("mode"), s.get("mode"))
        if s.get("targetTemperature") is not None:
            line += ", consigne %s %s" % (s["targetTemperature"], unit)
        if s.get("fanLevel"):
            line += ", ventilation %s" % s["fanLevel"]
    print(line)
    if m.get("temperature") is not None:
        print("Piece : %.1f C, %s %% d'humidite" % (m["temperature"], m.get("humidity", "?")))
    conn = pod.get("connectionStatus") or {}
    if conn and not conn.get("isAlive", True):
        print("Attention : le Sensibo semble hors ligne (verifie son Wi-Fi).")


def capabilities(pod):
    modes = (pod.get("remoteCapabilities") or {}).get("modes", {})
    if not modes:
        print("Capacites inconnues pour cet appareil.")
        return
    for mode, cap in modes.items():
        temps = (cap.get("temperatures") or {}).get("C", {}).get("values", [])
        rng = "%s a %s C" % (temps[0], temps[-1]) if temps else "pas de consigne"
        fans = ", ".join(cap.get("fanLevels", [])) or "-"
        print("%-12s %-16s ventilation : %s" % (MODES_FR.get(mode, mode), rng, fans))


def check_temp(pod, mode, temp):
    cap = ((pod.get("remoteCapabilities") or {}).get("modes", {}) or {}).get(mode)
    if not cap:
        if (pod.get("remoteCapabilities") or {}).get("modes"):
            raise ApiError("ton climatiseur n'a pas le mode %s (clim modes)" % MODES_FR.get(mode, mode))
        return
    values = (cap.get("temperatures") or {}).get("C", {}).get("values", [])
    if temp is not None and values and temp not in values:
        raise ApiError("consigne %s impossible en mode %s : de %s a %s C" % (temp, MODES_FR.get(mode, mode), values[0], values[-1]))


# ---------------------------------------------------------------------------
def main(argv):
    room = None
    if len(argv) >= 2 and argv[0] == "--piece":
        room, argv = argv[1], argv[2:]
    if argv and argv[0] in ("-h", "--help", "aide"):
        print(__doc__)
        return 0
    if len(argv) == 2 and argv[0] == "cle":
        save_key(argv[1])
        return 0

    key = load_key()
    pod = pick(pods(key), room)
    cur = pod.get("acState") or {}
    args = [a.lower() for a in argv]

    if not args or args == ["etat"]:
        describe(pod)
        return 0
    if args == ["modes"]:
        capabilities(pod)
        return 0
    if args == ["on"]:
        set_state(key, pod, on=True)
    elif args == ["off"]:
        set_state(key, pod, on=False)
    elif len(args) == 1 and args[0].replace(".", "", 1).isdigit():
        t = int(float(args[0]))
        check_temp(pod, cur.get("mode", "cool"), t)
        set_state(key, pod, on=True, targetTemperature=t, temperatureUnit="C")
    elif args[0] in MODES and len(args) <= 2:
        mode = MODES[args[0]]
        t = int(float(args[1])) if len(args) == 2 else None
        check_temp(pod, mode, t)
        changes = {"on": True, "mode": mode}
        if t is not None:
            changes.update(targetTemperature=t, temperatureUnit="C")
        set_state(key, pod, **changes)
    elif args[0] == "ventilo" and len(args) == 2:
        levels = (((pod.get("remoteCapabilities") or {}).get("modes", {}) or {}).get(cur.get("mode"), {}) or {}).get("fanLevels", [])
        if levels and args[1] not in levels:
            raise ApiError("ventilation possible : " + ", ".join(levels))
        set_state(key, pod, fanLevel=args[1])
    else:
        print(__doc__)
        return 2
    describe(pod)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except ApiError as e:
        sys.exit("Erreur : %s" % e)
    except KeyboardInterrupt:
        sys.exit(130)
