"""Dedicated durable Via Verde delivery. No mail, source writes or shared Yescapa token."""
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from haven_queue import HavenQueue

def enabled():
    return os.getenv("VV_HAVEN_ENABLED") == "true"

def queue_path():
    directory = Path(os.environ.get("HAVEN_QUEUE_DIR", ""))
    volume = Path(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", ""))
    if not os.environ.get("HAVEN_QUEUE_DIR") or not os.environ.get("RAILWAY_VOLUME_MOUNT_PATH"):
        raise ValueError("Via Verde delivery requires persistent volume")
    resolved, root = directory.resolve(), volume.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("Via Verde queue outside persistent volume")
    return str(resolved / "via_verde_fila.json")

def passage_row(plate, passage):
    plate = re.sub(r"[^A-Z0-9]", "", plate.upper())
    if len(plate) != 6:
        raise ValueError("Invalid plate")
    plate = "-".join([plate[:2], plate[2:4], plate[4:]])
    day = passage["date"].isoformat()
    hour = passage.get("hora", "")
    location = str(passage["location"]).strip()
    cents = Decimal(str(passage["amount"])) * 100
    if (not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", hour)
            or not location or len(location) > 200
            or any(ord(c) < 32 or ord(c) == 127 for c in location)
            or not cents.is_finite() or cents != cents.to_integral_value()
            or not 1 <= cents <= 100000):
        raise ValueError("Invalid source passage")
    row = {"matricula": plate, "data": day, "hora": hour, "local": location, "valor_cents": int(cents)}
    # An amount correction keeps its source identity. Desk refuses changed data
    # for an existing identity and requests reconciliation instead of double charging.
    identity = json.dumps({k:v for k,v in row.items() if k != "valor_cents"}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {"record_id": "vv:" + hashlib.sha256(identity.encode()).hexdigest(), **row}

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def send(envelope):
    if not enabled():
        return {"skipped": True, "reason": "Via Verde delivery disabled"}
    url, token = os.getenv("VV_HAVEN_URL", ""), os.getenv("VV_HAVEN_TOKEN", "")
    target = urllib.parse.urlsplit(url)
    if target.scheme != "https" or not target.hostname or target.username or target.password or target.query or target.fragment or not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", token):
        return {"skipped": True, "reason": "Via Verde configuration incomplete"}
    request = urllib.request.Request(url, data=json.dumps(envelope, separators=(",", ":")).encode(), headers={"Content-Type":"application/json","Authorization":"Bearer "+token}, method="POST")
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=25) as response:
            body = json.loads(response.read(65537))
            if response.status == 200 and body.get("ok") is True:
                return {"http":200, "response":{"code":"accepted"}}
            return {"error_code":"unconfirmed_response"}
    except urllib.error.HTTPError as error:
        # A lost acknowledgement retries the original event and never changes its identity.
        return {"http":503 if error.code == 409 else error.code, "response":{"code":"via_verde_unconfirmed"}}
    except Exception:
        return {"error_code":"via_verde_transport_error"}

def flush():
    if not enabled():
        return {"disabled": True}
    return HavenQueue(queue_path(), snapshot_field="captured_at").process(send, max_requests=3, time_budget_s=60)

def deliver(passages_by_plate, date_from, date_to):
    if not enabled():
        return {"disabled": True}
    rows = {}
    for plate, passages in passages_by_plate.items():
        for passage in passages:
            row = passage_row(plate, passage)
            if not date_from.isoformat() <= row["data"] <= date_to.isoformat():
                raise ValueError("Passage outside capture interval")
            rows[row["record_id"]] = row
    if not 1 <= len(passages_by_plate) <= 100 or len(rows) > 5000 or date_from > date_to:
        raise ValueError("Invalid complete capture")
    plates = sorted("-".join([re.sub(r"[^A-Z0-9]", "", p.upper())[i:i+2] for i in (0,2,4)]) for p in passages_by_plate)
    envelope = {"version":1,"event_id":str(uuid.uuid4()),"captured_at":datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00","Z"),
        "coverage":{"from":date_from.isoformat(),"to":date_to.isoformat(),"plates":plates,"complete":True},
        "rows":sorted(rows.values(), key=lambda row:row["record_id"])}
    queue = HavenQueue(queue_path(), snapshot_field="captured_at")
    queue.enqueue(envelope)
    return queue.process(send, max_requests=3, time_budget_s=60)
