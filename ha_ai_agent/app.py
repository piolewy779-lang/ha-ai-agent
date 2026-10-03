App · PY
# AI Agent per Home Assistant.
#
# Chat via ingress -> OpenAI (Responses API) -> strumenti su Home Assistant.
# L'agente NON esegue mai azioni da solo: le propone e l'utente le conferma
# dalla pagina. Cancello, garage, allarmi, serrature e simili sono esclusi.
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
 
with open("/data/options.json", encoding="utf-8") as f:
    OPTIONS = json.load(f)
 
API_KEY = (OPTIONS.get("openai_api_key") or "").strip()
MODEL = (OPTIONS.get("model") or "gpt-6.1-sol").strip()
 
 
def _leggi_token():
    # Con s6-overlay le variabili d'ambiente possono non arrivare al processo
    # lanciato da CMD: in quel caso il token sta nei file di s6.
    t = os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN") or ""
    if not t:
        try:
            with open("/run/s6/container_environment/SUPERVISOR_TOKEN",
                      encoding="utf-8") as fh:
                t = fh.read().strip()
        except OSError:
            t = ""
    return t
 
 
SUP_TOKEN = _leggi_token()
HA_URL = "http://supervisor/core/api"
OPENAI_URL = "https://api.openai.com/v1/responses"
INGRESS_IP = "172.30.32.2"  # unico client ammesso: il gateway ingress del Supervisor
PENDING_TTL = 600  # secondi di validità di una proposta
 
# --- Politica di sicurezza -------------------------------------------------
ALLOWED_DOMAINS = {"light", "switch", "climate", "fan", "media_player",
                   "scene", "input_boolean", "vacuum", "cover"}
HIDDEN_DOMAINS = {"person", "device_tracker", "camera", "lock",
                  "alarm_control_panel", "update", "event", "button"}
BLOCK_WORDS = ("cancello", "garage", "portone", "allarme", "serratura",
               "sirena", "citofono")
BLOCKED_COVER_CLASSES = {"garage", "gate"}
FORBIDDEN_DATA_KEYS = {"entity_id", "area_id", "device_id", "floor_id", "label_id"}
 
SYSTEM = (
    "Sei l'assistente di una casa smart basata su Home Assistant. Rispondi sempre "
    "in italiano, in modo breve e chiaro. Per trovare le entità usa cerca_entita, "
    "per leggere lo stato usa leggi_stato. Non puoi eseguire azioni direttamente: "
    "per ogni modifica usa proponi_azione e poi di' all'utente che deve confermare "
    "con il pulsante. Se un'entità non compare nei risultati, non è controllabile: "
    "non insistere e non aggirare il blocco. Non inventare entity_id."
)
 
TOOLS = [
    {"type": "function", "name": "cerca_entita",
     "description": "Cerca entità per nome o entity_id. Restituisce al massimo 20 risultati.",
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string", "description": "Parole da cercare, es. 'luce salotto'"},
         "dominio": {"type": "string", "description": "Opzionale: light, switch, climate..."}},
         "required": ["query"]}},
    {"type": "function", "name": "leggi_stato",
     "description": "Legge stato e attributi principali di una entità.",
     "parameters": {"type": "object", "properties": {
         "entity_id": {"type": "string"}}, "required": ["entity_id"]}},
    {"type": "function", "name": "proponi_azione",
     "description": "Propone un'azione su UNA entità. Non viene eseguita finché l'utente non conferma.",
     "parameters": {"type": "object", "properties": {
         "dominio": {"type": "string", "description": "es. light"},
         "servizio": {"type": "string", "description": "es. turn_on"},
         "entity_id": {"type": "string"},
         "dati": {"type": "object", "description": "Parametri opzionali, es. {\"brightness_pct\": 50}"},
         "motivo": {"type": "string", "description": "Descrizione breve in italiano dell'azione"}},
         "required": ["dominio", "servizio", "entity_id", "motivo"]}},
]
 
# --- Stato in memoria --------------------------------------------------------
SESSIONS = {}   # sid -> id dell'ultima risposta OpenAI
PENDING = {}    # id proposta -> dict
LOCK = threading.Lock()
 
 
def ha(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        HA_URL + path, data=data, method=method,
        headers={"Authorization": f"Bearer {SUP_TOKEN}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read()
        return json.loads(raw) if raw else None
 
 
def is_blocked(state):
    eid = state["entity_id"]
    attrs = state.get("attributes", {})
    domain = eid.split(".")[0]
    if domain in HIDDEN_DOMAINS:
        return True
    hay = (eid + " " + str(attrs.get("friendly_name", ""))).lower()
    if any(w in hay for w in BLOCK_WORDS):
        return True
    if domain == "cover" and attrs.get("device_class") in BLOCKED_COVER_CLASSES:
        return True
    return False
 
 
def tool_cerca(query, dominio=None):
    tokens = query.lower().split()
    out = []
    for s in ha("GET", "/states"):
        eid = s["entity_id"]
        domain = eid.split(".")[0]
        if dominio and domain != dominio:
            continue
        if is_blocked(s):
            continue
        name = s["attributes"].get("friendly_name", "")
        hay = (eid + " " + name).lower()
        if all(t in hay for t in tokens):
            out.append({"entity_id": eid, "nome": name, "stato": s["state"],
                        "controllabile": domain in ALLOWED_DOMAINS})
            if len(out) >= 20:
                break
    return out
 
 
def tool_stato(entity_id):
    s = ha("GET", f"/states/{entity_id}")
    if is_blocked(s):
        return {"errore": "entità non accessibile"}
    attrs = json.dumps(s["attributes"], ensure_ascii=False)[:1500]
    return {"entity_id": entity_id, "stato": s["state"], "attributi": attrs}
 
 
def check_action(dominio, servizio, entity_id, dati):
    if dominio not in ALLOWED_DOMAINS:
        return f"dominio '{dominio}' non consentito"
    if not re.fullmatch(r"[a-z_]+", servizio or ""):
        return "nome servizio non valido"
    if not re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", entity_id or "") \
            or entity_id.split(".")[0] != dominio:
        return "entity_id non valido per questo dominio"
    if FORBIDDEN_DATA_KEYS & set((dati or {}).keys()):
        return "parametri di targeting non consentiti"
    try:
        s = ha("GET", f"/states/{entity_id}")
    except Exception:
        return "entità non trovata"
    if is_blocked(s):
        return "entità non controllabile"
    return None
 
 
def tool_proponi(sid, new_pending, dominio, servizio, entity_id, motivo, dati=None):
    err = check_action(dominio, servizio, entity_id, dati)
    if err:
        return {"errore": err}
    pid = uuid.uuid4().hex[:8]
    item = {"id": pid, "sid": sid, "dominio": dominio, "servizio": servizio,
            "entity_id": entity_id, "dati": dati or {}, "motivo": motivo,
            "created": time.time()}
    with LOCK:
        PENDING[pid] = item
    new_pending.append({k: item[k] for k in ("id", "dominio", "servizio",
                                             "entity_id", "dati", "motivo")})
    return {"stato": "in attesa di conferma dell'utente", "id": pid}
 
 
def dispatch(name, args, sid, new_pending):
    if name == "cerca_entita":
        return tool_cerca(args.get("query", ""), args.get("dominio"))
    if name == "leggi_stato":
        return tool_stato(args.get("entity_id", ""))
    if name == "proponi_azione":
        return tool_proponi(sid, new_pending, args.get("dominio"), args.get("servizio"),
                            args.get("entity_id"), args.get("motivo", ""), args.get("dati"))
    return {"errore": "strumento sconosciuto"}
 
 
def openai(body):
    req = urllib.request.Request(
        OPENAI_URL, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:400]
        raise RuntimeError(f"OpenAI ha risposto {e.code}: {detail}")
 
 
def run_chat(sid, message):
    new_pending = []
    inp = [{"role": "user", "content": message}]
    for _ in range(6):
        body = {"model": MODEL, "instructions": SYSTEM, "input": inp, "tools": TOOLS}
        prev = SESSIONS.get(sid)
        if prev:
            body["previous_response_id"] = prev
        r = openai(body)
        SESSIONS[sid] = r["id"]
        calls = [o for o in r.get("output", []) if o.get("type") == "function_call"]
        if not calls:
            text = "".join(c.get("text", "")
                           for o in r.get("output", []) if o.get("type") == "message"
                           for c in o.get("content", []) if c.get("type") == "output_text")
            return text or "(nessuna risposta)", new_pending
        inp = []
        for c in calls:
            try:
                res = dispatch(c["name"], json.loads(c.get("arguments") or "{}"),
                               sid, new_pending)
            except Exception as e:  # l'errore torna al modello, non all'utente
                res = {"errore": str(e)[:200]}
            inp.append({"type": "function_call_output", "call_id": c["call_id"],
                        "output": json.dumps(res, ensure_ascii=False)})
    return "Troppi passaggi: prova con una richiesta più semplice.", new_pending
 
 
def take_pending(pid, sid):
    with LOCK:
        item = PENDING.pop(pid, None)
    if not item or item["sid"] != sid:
        return None, "Proposta non trovata o già gestita."
    if time.time() - item["created"] > PENDING_TTL:
        return None, "Proposta scaduta: richiedila di nuovo."
    return item, None
 
 
def confirm(pid, sid):
    item, err = take_pending(pid, sid)
    if err:
        return {"ok": False, "messaggio": err}
    # Ricontrollo la politica al momento dell'esecuzione (stato cambiato, blocchi, ecc.)
    err = check_action(item["dominio"], item["servizio"], item["entity_id"], item["dati"])
    if err:
        return {"ok": False, "messaggio": f"Annullata: {err}."}
    try:
        ha("POST", f"/services/{item['dominio']}/{item['servizio']}",
           {"entity_id": item["entity_id"], **item["dati"]})
    except urllib.error.HTTPError as e:
        return {"ok": False, "messaggio": f"Home Assistant ha risposto {e.code}."}
    except Exception as e:
        return {"ok": False, "messaggio": f"Errore: {str(e)[:150]}"}
    return {"ok": True, "messaggio": "Eseguito: " + item["motivo"]}
 
 
def cleanup_loop():
    while True:
        time.sleep(60)
        now = time.time()
        with LOCK:
            for k in [k for k, v in PENDING.items() if now - v["created"] > PENDING_TTL]:
                PENDING.pop(k, None)
 
 
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"),
          "rb") as f:
    INDEX = f.read()
 
 
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload, ctype="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(
            payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
 
    def _allowed(self):
        if self.client_address[0] != INGRESS_IP:
            self._send(403, {"errore": "accesso negato"})
            return False
        return True
 
    def _json(self):
        n = min(int(self.headers.get("Content-Length") or 0), 65536)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return {}
 
    def do_GET(self):
        if not self._allowed():
            return
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send(200, INDEX, "text/html")
        elif path.endswith("/api/status"):
            self._send(200, {"modello": MODEL, "chiave_presente": bool(API_KEY), "token_ha": bool(SUP_TOKEN)})
        else:
            self._send(404, {"errore": "non trovato"})
 
    def do_POST(self):
        if not self._allowed():
            return
        path = self.path.split("?")[0]
        data = self._json()
        sid = str(data.get("sid", ""))[:64] or "default"
        if path.endswith("/api/chat"):
            if not API_KEY:
                return self._send(200, {"risposta": "Manca la chiave OpenAI: inseriscila nella "
                                        "scheda Configurazione dell'add-on e riavvia.", "proposte": []})
            msg = str(data.get("messaggio", "")).strip()[:2000]
            if not msg:
                return self._send(400, {"errore": "messaggio vuoto"})
            try:
                text, pend = run_chat(sid, msg)
                self._send(200, {"risposta": text, "proposte": pend})
            except Exception as e:
                self._send(200, {"risposta": f"Errore: {e}", "proposte": []})
        elif path.endswith("/api/confirm"):
            self._send(200, confirm(str(data.get("id", "")), sid))
        elif path.endswith("/api/cancel"):
            _, err = take_pending(str(data.get("id", "")), sid)
            self._send(200, {"ok": err is None, "messaggio": err or "Proposta annullata."})
        else:
            self._send(404, {"errore": "non trovato"})
 
    def log_message(self, fmt, *args):
        pass  # niente log delle richieste: evita rumore e dati sensibili
 
 
if __name__ == "__main__":
    threading.Thread(target=cleanup_loop, daemon=True).start()
    print(f"AI Agent avviato. Modello: {MODEL}. Chiave presente: {bool(API_KEY)}. Token HA presente: {bool(SUP_TOKEN)}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8099), Handler).serve_forever()
