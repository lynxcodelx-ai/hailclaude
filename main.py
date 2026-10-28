"""Self-Healing AI Automation System (Code E Fiesta 2026, PS4).

A mock order pipeline (webhook -> validate -> CRM) that breaks on demand.
When a run fails: observe -> diagnose -> synthesize patch -> verify + retry,
with a human approval gate for risky fixes.

Run:  pip install -r requirements.txt
      export ANTHROPIC_API_KEY=...      (optional: without it, a rule-based healer is used)
      uvicorn main:app --reload
"""
import itertools, json, os, random, re, time
from pathlib import Path
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse

MODEL = os.getenv("HEAL_MODEL", "claude-sonnet-5-5")
STEP_DELAY = float(os.getenv("STEP_DELAY", "0.8"))  # pause between stages so the dashboard shows each one
LLM_ON = bool(os.getenv("ANTHROPIC_API_KEY")) and os.getenv("HEAL_MODE", "llm") != "rules"
if LLM_ON:
    try:
        from anthropic import Anthropic
        client = Anthropic()
    except Exception as e:
        print("Anthropic SDK unavailable, using rules:", e)
        LLM_ON = False

EXPECTED = {"order_id": str, "customer_name": str, "amount": float, "currency": str}
ERROR_TYPES = ["schema_mismatch", "bad_type", "missing_field", "rate_limit"]
SCENARIOS = {"healthy", "schema_drift", "bad_type", "missing_field", "rate_limit"}
DEFAULT_CFG = {"max_retries": 0, "backoff_s": 0.0}
ALIASES = {
    "order_id": ["id", "orderId", "order.id"],
    "customer_name": ["client.fullName", "customerName", "customer.name", "name"],
    "amount": ["total", "price"],
    "currency": ["curr", "ccy"],
}
NAMES = ["Asha Verma", "Rohan Iyer", "Meera Nair", "Kabir Singh"]

CRM = {"throttle": 0, "records": []}  # the fake downstream API
INCIDENTS, STATS, COUNTER = {}, {"healed": 0, "escalated": 0}, itertools.count(1)


class PipelineError(Exception):
    def __init__(self, msg, field=None):
        super().__init__(msg)
        self.field = field


# ---------- the workflow being protected ----------
def flatten(d, prefix=""):
    out = {}
    for k, v in d.items():
        out.update(flatten(v, f"{prefix}{k}.") if isinstance(v, dict) else {f"{prefix}{k}": v})
    return out


def pop_path(d, path):
    *parents, last = path.split(".")
    for p in parents:
        d = d[p]
    return d.pop(last)


def validate(p):
    missing = [k for k in EXPECTED if k not in p]
    if missing:
        raise PipelineError(f"Missing required fields: {', '.join(missing)}")
    for k, t in EXPECTED.items():
        v = p[k]
        ok = isinstance(v, (int, float)) and not isinstance(v, bool) if t is float else isinstance(v, t)
        if not ok:
            raise PipelineError(f"Field '{k}' expected {t.__name__}, got {type(v).__name__}: {v!r}", k)


def execute(payload, cfg):
    validate(payload)
    attempt = 0
    while True:
        if CRM["throttle"] > 0:
            CRM["throttle"] -= 1
            if attempt >= cfg["max_retries"]:
                raise PipelineError("CRM API returned 429 Too Many Requests")
            attempt += 1
            time.sleep(cfg["backoff_s"] * 2 ** (attempt - 1))
            continue
        CRM["records"].append(payload)
        return


def make_payload(scenario):
    amt, name = round(random.uniform(500, 5000), 2), random.choice(NAMES)
    base = {"order_id": f"ORD-{random.randint(1000, 9999)}", "customer_name": name, "amount": amt, "currency": "INR"}
    CRM["throttle"] = 3 if scenario == "rate_limit" else 0
    if scenario == "schema_drift":
        return {"id": base["order_id"], "client": {"fullName": name}, "amount": amt, "currency": "INR"}
    if scenario == "bad_type":
        base["amount"] = f"₹{amt:,.2f}"
    if scenario == "missing_field":
        del base["currency"]
    return base


# ---------- rule-based fallback (also keeps the demo alive without an API key) ----------
def rules_diagnose(err, payload):
    msg = str(err)
    if "429" in msg:
        t, why, conf = "rate_limit", "The downstream CRM is throttling requests (HTTP 429).", 0.95
    elif "expected" in msg:
        t, why, conf = "bad_type", f"Field '{err.field}' arrived in a format the pipeline cannot parse.", 0.92
    else:
        flat = flatten(payload)
        missing = [k for k in EXPECTED if k not in payload]
        found = all(any(a in flat for a in ALIASES[k]) for k in missing)
        t = "schema_mismatch" if found else "missing_field"
        why = ("Upstream changed the payload shape; the data exists under new names." if found
               else f"Field(s) {', '.join(missing)} are absent and have no equivalent in the payload.")
        conf = 0.9 if found else 0.6
    return {"error_type": t, "root_cause": why, "confidence": conf, "source": "rules"}


def rules_heal(err, payload):
    t = rules_diagnose(err, payload)["error_type"]
    if t == "rate_limit":
        ops = [{"op": "set_retry", "max_retries": 3, "backoff_s": 0.5}]
    elif t == "bad_type":
        ops = [{"op": "cast", "field": err.field, "to": "float" if EXPECTED[err.field] is float else "str"}]
    else:
        flat, ops = flatten(payload), []
        for k in EXPECTED:
            if k in payload:
                continue
            src = next((a for a in ALIASES[k] if a in flat), None)
            ops.append({"op": "rename", "from": src, "to": k} if src
                       else {"op": "default", "field": k, "value": "INR" if k == "currency" else ""})
    return {"ops": ops, "explanation": "Deterministic fallback patch.", "source": "rules"}


# ---------- LLM agents ----------
DIAG_SYS = """You are the root-cause diagnostic agent of a self-healing workflow system.
Given a failed run (error, payload, expected schema), classify it. Reply with ONLY a JSON object:
{"error_type": "schema_mismatch|bad_type|missing_field|rate_limit", "root_cause": "<one sentence>", "confidence": <0-1>}
schema_mismatch = upstream renamed or nested fields (the data still exists); bad_type = right field, wrong format;
missing_field = field absent with no equivalent anywhere; rate_limit = downstream HTTP 429."""

HEAL_SYS = """You are the healing synthesizer of a self-healing workflow system. Propose a minimal patch.
Reply with ONLY a JSON object: {"ops": [...], "explanation": "<one sentence>"}
Allowed ops (nothing else):
{"op":"rename","from":"dotted.path.in.payload","to":"expected_field"}
{"op":"cast","field":"expected_field","to":"float"|"str"}
{"op":"default","field":"expected_field","value":<any>}
{"op":"set_retry","max_retries":<int<=5>,"backoff_s":<number<=2>}
Prefer rename/cast. Use default only as a last resort, because it invents data."""


def llm_json(system, data):
    r = client.messages.create(model=MODEL, max_tokens=700, system=system,
                               messages=[{"role": "user", "content": json.dumps(data, ensure_ascii=False)}])
    text = r.content[0].text
    return json.loads(text[text.index("{"): text.rindex("}") + 1])


def sanitize(ops, payload):
    """Whitelist + validate every op so a model can never run arbitrary code or touch unknown fields."""
    flat, clean = flatten(payload), []
    for o in ops if isinstance(ops, list) else []:
        try:
            k = o.get("op")
            if k == "rename" and o["from"] in flat and o["to"] in EXPECTED:
                clean.append({"op": k, "from": o["from"], "to": o["to"]})
            elif k == "cast" and o["field"] in EXPECTED and o["to"] in ("float", "str"):
                clean.append({"op": k, "field": o["field"], "to": o["to"]})
            elif k == "default" and o["field"] in EXPECTED:
                clean.append({"op": k, "field": o["field"], "value": o["value"]})
            elif k == "set_retry":
                clean.append({"op": k, "max_retries": min(int(o["max_retries"]), 5), "backoff_s": min(float(o["backoff_s"]), 2.0)})
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return clean


def diagnose(err, payload):
    if LLM_ON:
        try:
            d = llm_json(DIAG_SYS, {"error": str(err), "payload": payload,
                                    "expected_schema": {k: t.__name__ for k, t in EXPECTED.items()}})
            if d.get("error_type") in ERROR_TYPES:
                return {"error_type": d["error_type"], "root_cause": str(d.get("root_cause", "")),
                        "confidence": float(d.get("confidence", 0.5)), "source": "llm"}
        except Exception as e:
            print("LLM diagnosis failed, using rules:", e)
    return rules_diagnose(err, payload)


def synthesize(diag, err, payload):
    if LLM_ON:
        try:
            h = llm_json(HEAL_SYS, {"diagnosis": diag, "error": str(err), "payload": payload,
                                    "expected_schema": {k: t.__name__ for k, t in EXPECTED.items()}})
            ops = sanitize(h.get("ops"), payload)
            if ops:
                return {"ops": ops, "explanation": str(h.get("explanation", "")), "source": "llm"}
        except Exception as e:
            print("LLM patch failed, using rules:", e)
    h = rules_heal(err, payload)
    h["ops"] = sanitize(h["ops"], payload)
    return h


def apply_patch(payload, cfg, ops):
    p, c = json.loads(json.dumps(payload)), dict(cfg)
    for o in ops:
        k = o["op"]
        if k == "rename":
            p[o["to"]] = pop_path(p, o["from"])
        elif k == "cast":
            v = p[o["field"]]
            p[o["field"]] = float(re.sub(r"[^\d.\-]", "", str(v))) if o["to"] == "float" else str(v)
        elif k == "default":
            p.setdefault(o["field"], o["value"])
        elif k == "set_retry":
            c.update(max_retries=o["max_retries"], backoff_s=o["backoff_s"])
    return p, c


# ---------- the self-healing loop ----------
def log(inc, stage, msg):
    inc["status"] = stage
    inc["timeline"].append({"t": time.strftime("%H:%M:%S"), "stage": stage, "msg": msg})
    time.sleep(STEP_DELAY)


def heal(inc):
    patch = inc["patch"]
    log(inc, "patched", f"Applying {len(patch['ops'])} patch operation(s) from the {patch['source']} healer and retrying the run.")
    try:
        payload, cfg = apply_patch(inc["payload"], inc["cfg"], patch["ops"])
        execute(payload, cfg)
    except (PipelineError, ValueError, KeyError) as e:
        STATS["escalated"] += 1
        log(inc, "escalated", f"The patched run still failed ({e}). Admin alerted with the full incident report.")
        return
    inc["payload_after"] = payload
    STATS["healed"] += 1
    log(inc, "healed", "Retry succeeded. Output validated against the schema and delivered to the CRM.")


def reject(inc):
    STATS["escalated"] += 1
    log(inc, "escalated", "Patch rejected by a human. Admin alerted with the full incident report.")


def run(scenario):
    payload, cfg = make_payload(scenario), dict(DEFAULT_CFG)
    try:
        execute(payload, cfg)
        return  # healthy run, nothing to heal
    except PipelineError as e:
        err = e
    inc = {"id": f"INC-{next(COUNTER):03d}", "scenario": scenario, "status": "detected", "timeline": [],
           "error": str(err), "payload": payload, "payload_after": None, "diagnosis": None,
           "patch": None, "cfg": cfg, "approval_needed": False}
    INCIDENTS[inc["id"]] = inc
    log(inc, "detected", f"Run failed: {err}")
    inc["diagnosis"] = diag = diagnose(err, payload)
    log(inc, "diagnosed", f"{diag['error_type']} ({round(diag['confidence'] * 100)}% confident): {diag['root_cause']}")
    inc["patch"] = patch = synthesize(diag, err, payload)
    if not patch["ops"]:
        STATS["escalated"] += 1
        log(inc, "escalated", "No safe patch could be generated. Admin alerted.")
        return
    risky = any(o["op"] == "default" for o in patch["ops"])
    if risky or diag["confidence"] < 0.75:
        inc["approval_needed"] = True
        why = "This patch fills in data nobody supplied" if risky else "Diagnosis confidence is low"
        log(inc, "awaiting_approval", f"{why}. Waiting for a human to approve or reject.")
        return
    heal(inc)


# ---------- API ----------
app = FastAPI()


@app.get("/")
def index():
    return FileResponse(Path(__file__).with_name("index.html"))


@app.get("/api/state")
def state():
    return {"mode": f"AI healer: {MODEL}" if LLM_ON else "Rule-based healer (no API key)",
            "stats": {**STATS, "delivered": len(CRM["records"])},
            "incidents": sorted(INCIDENTS.values(), key=lambda i: i["id"], reverse=True)}


@app.post("/api/inject/{scenario}")
def inject(scenario: str, bg: BackgroundTasks):
    if scenario not in SCENARIOS:
        raise HTTPException(404, "Unknown scenario")
    bg.add_task(run, scenario)
    return {"ok": True}


@app.post("/api/incidents/{iid}/{decision}")
def decide(iid: str, decision: str, bg: BackgroundTasks):
    inc = INCIDENTS.get(iid)
    if not inc or not inc["approval_needed"] or decision not in ("approve", "reject"):
        raise HTTPException(400, "Nothing to decide")
    inc["approval_needed"] = False
    bg.add_task(heal if decision == "approve" else reject, inc)
    return {"ok": True}


@app.post("/api/reset")
def reset():
    INCIDENTS.clear(); CRM["records"].clear(); CRM["throttle"] = 0
    STATS.update(healed=0, escalated=0)
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
