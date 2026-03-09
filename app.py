"""
86 Dumpster - Angi Lead Automation
====================================
Standalone Flask app that receives Angi lead emails via webhook
(from Power Automate) and creates CRM customers in DRS.

Deploy to Render as a new Web Service connected to its own GitHub repo.

Endpoints:
  POST /webhook/angi-lead     -- Receives email body from Power Automate
  GET  /status                -- Simple dashboard showing recent leads processed
  GET  /health                -- Health check for Render
"""

from flask import Flask, request, jsonify, render_template_string
import requests as http_requests
import json, re, os, logging
from datetime import datetime

# ─── Configuration ──────────────────────────────────────────────────────────────

API_TOKEN = os.environ.get("DRS_API_TOKEN", "86dumpster_4cb831580f46963847a9b2c7223fbcad")
DEV_KEY = os.environ.get("DRS_DEV_KEY", "3215acc664a747c4f56ae2785608236e3997dbb47bf584d1570519eed1ecae5cb2d2ce1332f19bfa872800a04ebc2a47c39d0cbf6c84616de1103c0b8dab696a$c2bca267")
BASE_URL = "https://86dumpster.ourers.com/api"

# Simple auth token for the webhook (set in Power Automate headers)
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "86dumpster-angi-2026")

# Dumpster size item IDs (for future quote creation)
DUMPSTER_ITEMS = {
    "10": "73895", "12": "73889", "15": "73869",
    "20": "73870", "25": "73872", "30": "73938",
}

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# In-memory log of recent leads (resets on deploy, that's fine)
recent_leads = []
MAX_LOG = 50

# City cache (loaded once at startup)
city_cache = {}


# ─── DRS API ────────────────────────────────────────────────────────────────────

def drs_post(endpoint, extra_data=None):
    data = {"key": DEV_KEY, "token": API_TOKEN}
    if extra_data:
        data.update(extra_data)
    try:
        resp = http_requests.post(f"{BASE_URL}/{endpoint}", data=data, timeout=15)
        return resp.json()
    except Exception as e:
        logging.error(f"DRS API error ({endpoint}): {e}")
        return {"error": str(e)}


def load_cities():
    global city_cache
    result = drs_post("read/cities/")
    if isinstance(result, dict) and "rows" in result:
        for row in result["rows"]:
            name = row.get("name", "").strip()
            city_cache[name.lower()] = {
                "id": row["id"],
                "name": name,
                "stateid": row.get("stateid", "4764")
            }
    logging.info(f"Loaded {len(city_cache)} cities from DRS")


# ─── Parsing ────────────────────────────────────────────────────────────────────

def parse_angi_lead(text):
    """Parse Angi lead email into structured data."""
    lead = {
        "firstname": "", "lastname": "", "phone": "", "email": "",
        "address": "", "city": "", "state": "", "zip": "",
        "job_number": "", "lead_type": "", "comments": "",
    }

    lines = [l.strip() for l in text.strip().split("\n") if l.strip()]

    # ── Name: first non-junk line after "Customer Information" ──
    for i, line in enumerate(lines):
        if "customer information" in line.lower():
            for j in range(i + 1, min(i + 5, len(lines))):
                c = lines[j]
                if (c and not c.startswith("(") and "@" not in c
                        and "send" not in c.lower() and "view" not in c.lower()
                        and "lead" not in c.lower() and "http" not in c.lower()):
                    parts = c.split()
                    if 1 <= len(parts) <= 5:
                        lead["firstname"] = parts[0]
                        lead["lastname"] = " ".join(parts[1:]) if len(parts) > 1 else ""
                        break
            break

    # ── Phone: first phone number that isn't 877 (Angi support) ──
    for m in re.finditer(r'\(?\d{3}\)?[\s\-\.]?\d{3}[\s\-\.]?\d{4}', text):
        phone = m.group().strip()
        if not phone.startswith("(877") and not phone.startswith("877"):
            lead["phone"] = phone
            break

    # ── Email: first non-Angi email ──
    for m in re.finditer(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', text):
        e = m.group()
        if "angi.com" not in e.lower() and "homeadvisor" not in e.lower():
            lead["email"] = e
            break

    # ── Address: look for patterns ending in "ST 12345" (state + zip) ──
    addr_match = re.search(
        r'(\b[A-Z0-9][^,\n]*(?:,\s*[^,\n]+)*),\s*([A-Z]{2})\s+(\d{5})',
        text
    )
    if addr_match:
        full_before_state = addr_match.group(1).strip()
        lead["state"] = addr_match.group(2)
        lead["zip"] = addr_match.group(3)
        parts = [p.strip() for p in full_before_state.split(",")]
        if len(parts) >= 2:
            lead["address"] = parts[0]
            lead["city"] = parts[-1]
        elif len(parts) == 1:
            lead["address"] = parts[0]

    # ── Job Number ──
    job_match = re.search(r'Job\s*#[:\s]*(\d+)', text, re.IGNORECASE)
    if job_match:
        lead["job_number"] = job_match.group(1)
    else:
        angi_match = re.search(r'Angi\s*#\s*(\d+)', text, re.IGNORECASE)
        if angi_match:
            lead["job_number"] = angi_match.group(1)

    # ── Lead Type ──
    type_match = re.search(r'Lead\s+Type[:\s]+(\w+)', text, re.IGNORECASE)
    if type_match:
        lead["lead_type"] = type_match.group(1)

    # ── Comments ──
    comments_lines = []
    in_comments = False
    for line in lines:
        if "comments:" in line.lower():
            in_comments = True
            after = line.split(":", 1)
            if len(after) > 1 and after[1].strip():
                comments_lines.append(after[1].strip())
            continue
        if in_comments:
            if any(x in line.lower() for x in [
                "view lead", "tips from", "thank you", "lead type",
                "job information", "service description"
            ]):
                break
            comments_lines.append(line)
    lead["comments"] = " ".join(comments_lines).strip()

    return lead


def parse_dumpster_size(comments):
    if not comments:
        return None
    text = comments.lower()
    for pattern in [
        r'(\d{2})\s*[-\s]?\s*(?:yard|yd|yds)',
        r'(\d{2})\s*(?:[-\s]\s*\d{2})?\s*(?:yard|yd|yds)',
    ]:
        m = re.search(pattern, text)
        if m and m.group(1) in ["10", "12", "15", "20", "25", "30"]:
            return m.group(1)
    return None


def match_city(city_text):
    if not city_text:
        return None
    city_lower = city_text.strip().lower()
    if city_lower in city_cache:
        return city_cache[city_lower]
    for key, val in city_cache.items():
        if key in city_lower or city_lower in key:
            return val
    if "other" in city_cache:
        return city_cache["other"]
    return None


# ─── CRM Creation ───────────────────────────────────────────────────────────────

def create_crm_customer(lead, city_info):
    customer = {
        "firstname": lead["firstname"],
        "lastname": lead["lastname"],
        "company_name": "Angi Lead",
        "email": lead["email"],
        "phone": lead["phone"],
        "billing_address": lead["address"],
        "billing_zip": lead["zip"],
        "alternate_contact_name": lead.get("job_number", ""),
        "reference": "28",  # "from Angi"
    }
    if city_info:
        customer["billing_city"] = city_info["id"]
        customer["billing_state"] = city_info["stateid"]

    result = drs_post("create/crm_customer/", {
        "customer": json.dumps(customer),
        "customer_tags": json.dumps(["Angi Lead"])
    })
    return result


# ─── Webhook Endpoint ───────────────────────────────────────────────────────────

@app.route("/webhook/angi-lead", methods=["POST"])
def webhook_angi_lead():
    """Receive Angi lead email from Power Automate and create CRM customer."""

    # Simple auth check
    auth = request.headers.get("X-Webhook-Secret", "")
    if auth != WEBHOOK_SECRET:
        logging.warning(f"Webhook auth failed. Got: {auth[:10]}...")
        return jsonify({"error": "Unauthorized"}), 401

    # Get email body from request
    data = request.get_json(silent=True) or {}
    email_body = data.get("body", "") or data.get("email_body", "") or data.get("content", "")

    # Also accept plain text body
    if not email_body and request.content_type and "text" in request.content_type:
        email_body = request.get_data(as_text=True)

    if not email_body:
        logging.warning("Webhook received empty body")
        return jsonify({"error": "No email body provided"}), 400

    # Check if this is a "New Opportunity" (no customer details)
    if "new opportunity" in email_body.lower() and "customer information" not in email_body.lower():
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "type": "skipped",
            "reason": "New Opportunity email (no customer details)",
            "name": "N/A",
        }
        recent_leads.insert(0, log_entry)
        if len(recent_leads) > MAX_LOG:
            recent_leads.pop()
        logging.info("Skipped: New Opportunity email")
        return jsonify({"status": "skipped", "reason": "New Opportunity - no customer details"}), 200

    # Parse the lead
    lead = parse_angi_lead(email_body)
    size = parse_dumpster_size(lead["comments"])
    city_info = match_city(lead["city"])

    # Validate minimum fields
    if not lead["firstname"]:
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "type": "error",
            "reason": "Could not parse customer name",
            "name": "Unknown",
        }
        recent_leads.insert(0, log_entry)
        if len(recent_leads) > MAX_LOG:
            recent_leads.pop()
        logging.warning("Could not parse customer name from email")
        return jsonify({"error": "Could not parse customer name"}), 400

    # Create in DRS
    result = create_crm_customer(lead, city_info)
    customer_id = (result.get("customer_id") or result.get("id")
                   or result.get("customerid") or "")

    success = result.get("status") == "Success" or result.get("success") or customer_id

    # Log it
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "type": "created" if success else "error",
        "name": f"{lead['firstname']} {lead['lastname']}",
        "email": lead["email"],
        "phone": lead["phone"],
        "city": lead["city"],
        "city_match": city_info["name"] if city_info else "No match",
        "job_number": lead["job_number"],
        "comments": lead["comments"][:80],
        "size": size or "default 20yd",
        "customer_id": str(customer_id),
        "drs_response": result.get("status", "Unknown"),
    }
    recent_leads.insert(0, log_entry)
    if len(recent_leads) > MAX_LOG:
        recent_leads.pop()

    if success:
        logging.info(f"Created CRM customer: {lead['firstname']} {lead['lastname']} (ID: {customer_id})")
        return jsonify({
            "status": "created",
            "customer_id": customer_id,
            "name": f"{lead['firstname']} {lead['lastname']}",
            "city": lead["city"],
            "size": size or "default 20yd",
        }), 201
    else:
        logging.error(f"Failed to create customer: {result}")
        return jsonify({"error": "DRS creation failed", "details": result}), 500


# ─── Status Dashboard ───────────────────────────────────────────────────────────

STATUS_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>86 Dumpster - Angi Lead Automation</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
               background: #f5f5f5; color: #333; padding: 20px; }
        .header { background: #1a5c2e; color: white; padding: 20px; border-radius: 8px;
                  margin-bottom: 20px; }
        .header h1 { font-size: 1.4em; }
        .header p { opacity: 0.8; margin-top: 5px; font-size: 0.9em; }
        .stats { display: flex; gap: 15px; margin-bottom: 20px; flex-wrap: wrap; }
        .stat { background: white; padding: 15px 20px; border-radius: 8px; flex: 1;
                min-width: 120px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
        .stat .number { font-size: 2em; font-weight: bold; color: #1a5c2e; }
        .stat .label { font-size: 0.85em; color: #666; }
        table { width: 100%; border-collapse: collapse; background: white;
                border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
        th { background: #e8e8e8; padding: 12px 15px; text-align: left; font-size: 0.85em;
             text-transform: uppercase; color: #555; }
        td { padding: 10px 15px; border-top: 1px solid #eee; font-size: 0.9em; }
        tr:hover td { background: #f9f9f9; }
        .badge { display: inline-block; padding: 3px 8px; border-radius: 12px;
                 font-size: 0.75em; font-weight: bold; }
        .badge-created { background: #d4edda; color: #155724; }
        .badge-skipped { background: #fff3cd; color: #856404; }
        .badge-error { background: #f8d7da; color: #721c24; }
        .empty { text-align: center; padding: 40px; color: #999; }
        .webhook-info { background: white; padding: 15px 20px; border-radius: 8px;
                       margin-bottom: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.1);
                       font-size: 0.9em; }
        .webhook-info code { background: #f0f0f0; padding: 2px 6px; border-radius: 3px;
                            font-size: 0.85em; }
        .refresh { float: right; color: white; text-decoration: none; opacity: 0.8; }
        .refresh:hover { opacity: 1; }
    </style>
</head>
<body>
    <div class="header">
        <a href="/status" class="refresh">↻ Refresh</a>
        <h1>🗑️ 86 Dumpster - Angi Lead Automation</h1>
        <p>Auto-creates CRM customers in DRS from Angi lead emails</p>
    </div>

    <div class="stats">
        <div class="stat">
            <div class="number">{{ total_created }}</div>
            <div class="label">Customers Created</div>
        </div>
        <div class="stat">
            <div class="number">{{ total_skipped }}</div>
            <div class="label">Skipped</div>
        </div>
        <div class="stat">
            <div class="number">{{ total_errors }}</div>
            <div class="label">Errors</div>
        </div>
        <div class="stat">
            <div class="number">{{ cities_loaded }}</div>
            <div class="label">Cities Loaded</div>
        </div>
    </div>

    <div class="webhook-info">
        <strong>Webhook URL:</strong> <code>POST {{ webhook_url }}/webhook/angi-lead</code><br>
        <strong>Header:</strong> <code>X-Webhook-Secret: [configured in environment]</code>
    </div>

    {% if leads %}
    <table>
        <thead>
            <tr>
                <th>Time</th>
                <th>Status</th>
                <th>Name</th>
                <th>City</th>
                <th>Job #</th>
                <th>Comments</th>
            </tr>
        </thead>
        <tbody>
        {% for lead in leads %}
            <tr>
                <td>{{ lead.timestamp[:16] }}</td>
                <td><span class="badge badge-{{ lead.type }}">{{ lead.type }}</span></td>
                <td>{{ lead.name }}</td>
                <td>{{ lead.get('city_match', '') }}</td>
                <td>{{ lead.get('job_number', '') }}</td>
                <td>{{ lead.get('comments', '')[:50] }}</td>
            </tr>
        {% endfor %}
        </tbody>
    </table>
    {% else %}
    <div class="empty">
        <p>No leads processed yet.</p>
        <p style="margin-top:10px">Waiting for Power Automate to send Angi lead emails...</p>
    </div>
    {% endif %}
</body>
</html>
"""


@app.route("/status")
def status():
    total_created = sum(1 for l in recent_leads if l["type"] == "created")
    total_skipped = sum(1 for l in recent_leads if l["type"] == "skipped")
    total_errors = sum(1 for l in recent_leads if l["type"] == "error")
    webhook_url = request.host_url.rstrip("/")

    return render_template_string(STATUS_HTML,
        leads=recent_leads,
        total_created=total_created,
        total_skipped=total_skipped,
        total_errors=total_errors,
        cities_loaded=len(city_cache),
        webhook_url=webhook_url,
    )


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "cities_loaded": len(city_cache),
        "leads_processed": len(recent_leads),
        "timestamp": datetime.now().isoformat(),
    })


@app.route("/")
def home():
    return '<meta http-equiv="refresh" content="0;url=/status">'


# ─── Startup ────────────────────────────────────────────────────────────────────

# Load cities on startup
load_cities()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    print(f"\n  86 Dumpster Angi Lead Automation")
    print(f"  Webhook: http://localhost:{port}/webhook/angi-lead")
    print(f"  Dashboard: http://localhost:{port}/status")
    print(f"  Cities loaded: {len(city_cache)}\n")
    app.run(host="0.0.0.0", port=port, debug=True)
