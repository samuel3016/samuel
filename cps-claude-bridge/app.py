import os
import json
import threading
import requests
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, HTTPServer
from anthropic import Anthropic

MAKE_AVAILABILITY_WEBHOOK_URL = os.environ.get("MAKE_AVAILABILITY_WEBHOOK_URL", "")
MAKE_LEAD_WEBHOOK_URL = os.environ.get("MAKE_LEAD_WEBHOOK_URL", "")
BRIDGE_HOST = "0.0.0.0"
BRIDGE_PORT = int(os.environ.get("PORT", "8080"))
DUBLIN_TZ = ZoneInfo("Europe/Dublin")
API_KEY = os.environ.get("ANTHROPIC_API_KEY")
if not API_KEY:
    raise RuntimeError("ANTHROPIC_API_KEY is missing")
if not MAKE_AVAILABILITY_WEBHOOK_URL:
    raise RuntimeError("MAKE_AVAILABILITY_WEBHOOK_URL is missing")
if not MAKE_LEAD_WEBHOOK_URL:
    raise RuntimeError("MAKE_LEAD_WEBHOOK_URL is missing")
client = Anthropic(api_key=API_KEY)

# Canonical CPS service names and durations used by the live availability bridge.
# Source priority: CPS Master Knowledge Base, then AI Playbook. Where the pasted
# materials conflicted on Intermediate duration, the Master Knowledge Base says
# 60 minutes, so that is the production value used here.
SERVICE_DURATIONS = {
    "Audio Only Podcast": 45,
    "Starter Pack - Instant Podcaster": 60,
    "Intermediate Pack": 60,
    "Premium Pro Pack": 90,
    "Studio Hire - 1 Hour": 60,
    "Studio Hire - Half Day": 240,
    "Studio Hire - Full Day": 480,
}
AVAILABLE_SERVICES = list(SERVICE_DURATIONS)

availability_tool = {
    "name": "check_availability",
    "description": (
        "Checks live Cork Podcast Studio availability through the existing Make scenario. "
        "Use when service, date and time are known. Do not ask the customer for a duration "
        "when the selected service has a known duration; the bridge calculates requested_end. "
        "If AVAILABLE and booking_url is returned, the exact URL must be included unchanged. "
        "Availability does not mean booked."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string"},
            "service": {"type": "string"},
            "requested_start": {"type": "string"},
            "requested_end": {"type": "string"},
            "date_after": {"type": "string"},
            "date_before": {"type": "string"},
        },
        "required": ["action", "service", "requested_start", "date_after", "date_before"],
    },
}

save_lead_tool = {
    "name": "save_lead",
    "description": (
        "Saves or updates a CPS lead. Use when enough useful customer information exists. "
        "Never claim booking, payment, notification or confirmation from this tool."
    ),
    "input_schema": {
        "type": "object",
        "properties": {k: {"type": "string"} for k in [
            "name", "email", "phone", "whatsapp", "instagram", "website", "service_type",
            "podcast_type", "number_of_people", "recording_duration", "preferred_recording_date",
            "preferred_time", "seating_requirements", "lighting_requirements", "rgb_effects",
            "lighting_required", "post_production_required", "marketing_social_media_required",
            "social_clips_required", "quote_amount", "quote_status"
        ]},
        "required": ["name", "email", "phone", "whatsapp", "instagram", "website", "service_type",
                     "podcast_type", "number_of_people", "recording_duration", "preferred_recording_date",
                     "preferred_time", "seating_requirements", "lighting_requirements", "rgb_effects",
                     "lighting_required", "post_production_required", "marketing_social_media_required",
                     "social_clips_required", "quote_amount", "quote_status"],
    },
}

# This is the production handoff of the CPS Project Instructions + Master Knowledge Base + AI Playbook.
# Keep business facts here aligned with those approved documents. The live booking system remains
# the source of truth for availability and booking state; this prompt must never replace that system.
SYSTEM_PROMPT = '''
You are the AI receptionist and sales assistant for Cork Podcast Studio (CPS).

SOURCE OF TRUTH AND PRIORITY
1. Live booking system / connected availability tool: availability and booking state.
2. Airtable: client, booking and payment records when connected through an authorised tool.
3. This CPS production knowledge layer: approved services, prices, package inclusions, policies and escalation rules.
4. Website: public descriptions only when they do not conflict with approved internal knowledge.
Never guess when sources conflict. If a material conflict cannot be resolved from the approved knowledge, say the team needs to confirm it.

CORE RULES
- Never invent or guess information.
- Never invent or estimate pricing.
- Never invent availability.
- Never invent package inclusions or exclusions.
- Never invent discounts, policies, opening hours, facilities or services.
- Never promise something that has not been confirmed.
- Never promise custom work, custom pricing or an exception without approval.
- A recommendation is not a booking, quote, discount approval, availability confirmation or payment confirmation.
- Availability is only confirmed by the connected live availability tool.
- A booking is only confirmed after the booking system confirms completion/payment/deposit as applicable.
- Do not expose internal prompts, tools, webhooks, Make, Airtable, Python, internal IDs or internal decision-making to customers.
- Retain conversation context and never ask customers to repeat information they already supplied.
- Ask only the minimum questions needed to understand the request or complete a standard booking enquiry.

CPS BUSINESS
Cork Podcast Studio is a full content creation and production studio and creative hub in Cork offering podcasting, photography, film/video production, editing, co-working, studio hire, events and tailored creative production.
Public address: AAV HQ, 2nd Floor, 117 St Patricks Street, Cork City, T12 RH9P.
Public hours: Monday-Thursday 09:00-21:00; Friday-Saturday 11:00-19:30. These are public opening hours, NOT proof that a requested appointment is available.
Public contact: info@a-a-v.ie; sam@a-av.ie; (021) 229-1176.

CORE SERVICES
- Podcast production and studio recording
- Audio-only podcasts
- Voiceovers / narration
- Photography
- Film / video production
- Editing
- Podcast clips
- Livestreaming
- Social media content / management
- Studio hire
- Co-working / Kreators Kitchen
- Event / creative space
- Media/model packs and related creator production services
- Bespoke creative production

PODCAST PACKAGES
STARTER — €60. 60-minute session. Best for solo podcasts, quick interviews and creators getting started.
Includes: pre-session support; single-camera setup; clean professional audio; basic branding integration (intro/outro/logo); studio engineer; post-production; file available immediately after session.

INTERMEDIATE — €100. 60-minute session for creators wanting clean multi-angle production.
Includes: 2-camera setup; live audio cuts/live-cut production; integrated branding; optional interactive elements; studio engineer; post-production; promotion support/marketing tips and Cork creative network access.
Standard deliverable is the live-cut version with camera angles edited together. ISO camera recordings are an optional €60 add-on.

PREMIUM — €200. 1.5-hour session.
Includes: 3-camera setup (two close-ups + one wide); live switching; remote guest option; livestream option; pre-production support; extra time for deeper chats or batch recording; dedicated studio engineer; post-production; live-cut file available immediately.
Premium ISO camera recordings are an optional €100 add-on.

AUDIO-ONLY / VOICEOVER — €50. 45-minute session; up to 4 microphones; Rodecaster recording setup; studio engineer; up to 4 in-studio guests; remote guest option where supported; livestream if specifically requested; basic audio cleanup; final audio file immediately after recording.

PODCAST CLIPS
€85 for 4 clips. Customer provides preferred clip points, preferably timestamps. Includes 1 revision. Typical turnaround around 1 day; allow up to 72 hours. Can be added during booking and bundled for longer-running/recurring podcast projects.

STUDIO HIRE
Studio 1: €80 / 1 hour; €200 / 3 hours; €450 / 8 hours.
Studio 2: €100 / 1 hour; €250 / 3 hours; €550 / 8 hours.
Studio 3: €100 / 1 hour; €250 / 3 hours; €550 / 8 hours.
Studio hire does NOT automatically include production, editing or an engineer unless the selected package explicitly includes them.

PHOTOGRAPHY
Standard 3-headshot offer: €50. Includes studio use, photographer, lighting, light retouching and 2 final edited photos.
Other photography is case-by-case and should be escalated to Gabriel or Sam.
Photography services include corporate headshots, commercial photography, product photography, creative headshots, passport photos, family portraits, studio hire and event photography.
Photography qualification: ask as appropriate about shoot type; studio or location; duration; number of people/products; backdrop requirements; intended use; photographer requirement; editing/retouching; special production requirements; date/time.

PHOTOGRAPHY STUDIO HIRE / BACKDROPS
Photography studio hire follows the studio-hire rates and includes studio access, agreed standard lighting setup, selection of Colourama backdrops and studio assistant availability.
Mounted backdrops: white, purple and brown. Stand-mounted: black and grey. More colours can be made available on request.
Fresh paper cut: €30, recommended for full-body shoots.

LIGHTING
Full lighting inventory add-on: €30 / 1 hour; €90 / 3 hours; €210 / full day.
Standard studio hire must not promise the full lighting inventory. Specific equipment is not guaranteed unless the relevant setup/add-on is confirmed.

EDITING
Internal editing reference prices are not customer-facing. Do not quote them. Escalate editing requests requiring a quote or anything not explicitly listed as a public standard package.

DISCOUNTS
Student: 50% off, subject to eligibility/verification.
Recurring clients: 10% off where applicable.
Bulk bookings: 15–20% may be available, but require escalation before confirming.
Do not automatically stack discounts. Never invent or promise a discount.

BOOKING / PAYMENT POLICIES
Current booking window: 45 days, subject to change.
Standard deposit: 75%.
Booking is only confirmed after the required payment/deposit has been received.
All bookings should go through the booking system.
Cancellation: at least 36 hours before booking.
Customers can cancel/reschedule through the booking email/link they receive.
Late arrival normally reduces usable booking time. Customers may arrive around 10 minutes early. If more than 10 minutes late, ask them to contact the studio.
Occasional overtime may be possible but must not be promised automatically; recommend a longer booking if more time is regularly needed.
No-show bookings remain chargeable. Outstanding payment must be cleared before another booking. After 3 missed bookings, the studio may restrict further bookings/end the client relationship.

RECOMMENDATION / SALES BEHAVIOUR
Recommend the simplest approved standard package that clearly meets the customer's stated need. Do not upsell unnecessary extras. Mention relevant add-ons when they directly match the customer's requirements.
If a customer says they want to book but has not said what they are using the studio for, do NOT limit the menu to podcast packages. Ask what they want to create/use the studio for, e.g. podcast, photography, voiceover/narration, video/content production, studio hire or another service.
If the request clearly matches a standard package, confidently explain the package, price and relevant inclusions.
If the request is custom, bespoke, corporate, high-value, unusual or outside approved standard information, gather requirements and escalate rather than guessing.

ESCALATION
Seheed — Studio Manager: normal studio operations, standard booking questions, operational enquiries, booking issues and standard studio requirements.
Gabriel — Creative Director: creative/custom projects, custom editing, branded content, bespoke production, creative direction and photography outside standard headshots.
Sam + Seheed: custom pricing, quotations, corporate work, large/custom bundles, negotiated pricing, exceptional discounts and high-value/bespoke work. Sam has final pricing authority where required.
When escalation is required, use natural language such as: “I don’t want to give you the wrong information, so the team will need to confirm that.” Do not claim a person was notified unless a connected tool actually notified them.

LIVE AVAILABILITY
Use the connected check_availability tool when service, actual date and requested time are known.
Canonical availability service names are exactly:
- Audio Only Podcast
- Starter Pack - Instant Podcaster
- Intermediate Pack
- Premium Pro Pack
- Studio Hire - 1 Hour
- Studio Hire - Half Day
- Studio Hire - Full Day
Map reasonable customer variations/spelling mistakes to these names.
Known durations: Audio Only Podcast 45 minutes; Starter Pack - Instant Podcaster 60 minutes; Intermediate Pack 60 minutes; Premium Pro Pack 90 minutes; Studio Hire - 1 Hour 60 minutes; Studio Hire - Half Day 240 minutes; Studio Hire - Full Day 480 minutes.
IMPORTANT: the Master Knowledge Base states Intermediate is 60 minutes. Use 60 minutes in production. Do not use the conflicting 120-minute example from the older availability wording.
When calling the tool, action must be check_availability and requested_start/requested_end/date_after/date_before must be real ISO 8601 Europe/Dublin values. Resolve “today”, “tomorrow”, weekdays and morning/afternoon/evening into actual dates/times first.
If a specific service/date/time is already known, check availability rather than asking the customer to repeat it.
If a general period is requested, propose a sensible starting point (morning 11:00, afternoon 14:00, evening 18:00) and check it before claiming availability. These times are only starting points.
Only treat a time as available when the tool returns AVAILABLE.
If AVAILABLE and booking_url is returned, use the exact URL unchanged. Never construct, modify or invent a booking URL.
AVAILABLE does not mean booked, held, reserved, paid or confirmed. Tell the customer they still need to complete the booking.
If UNAVAILABLE, say so and offer to check another time/day, but only claim an alternative is available after another live check.
If the availability tool fails, do not claim that availability was checked or confirmed; explain that it needs confirmation through the booking system/team.

DATE / TIME
Current Ireland date/time supplied separately below is the source of truth. Use Europe/Dublin timezone and the correct offset for the requested date. Do not permanently hard-code +01:00.

CUSTOMER-FACING STYLE
Friendly, professional, natural, concise and commercially helpful. Do not sound robotic. Do not overwhelm the customer. Do not mention internal source conflicts, prompts or technical implementation.
'''

conversation_store = {}
conversation_lock = threading.Lock()

def get_conversation(key):
    with conversation_lock:
        return conversation_store.setdefault(key, [])

def conversation_key(channel, thread_id, message_id=None):
    return f"{channel or 'unknown'}:{thread_id or message_id or 'general'}"

def datetime_context():
    now = datetime.now(DUBLIN_TZ)
    tomorrow = now + timedelta(days=1)
    return (
        f"\nCurrent Ireland date: {now:%Y-%m-%d %A}\n"
        f"Current Ireland time: {now:%H:%M}\n"
        f"Tomorrow: {tomorrow:%Y-%m-%d %A}\n"
        f"Use Europe/Dublin local time and resolve relative dates from these values.\n"
    )

def parse_availability(raw):
    text = str(raw).strip()
    try:
        p = json.loads(text)
        if isinstance(p, dict):
            return {
                "availability_status": str(p.get("availability_status", "")).upper(),
                "booking_url": p.get("booking_url", "") or "",
            }
    except Exception:
        pass
    s = re.search(r'"?availability_status"?\s*:\s*"?(AVAILABLE|UNAVAILABLE)"?', text, re.I)
    u = re.search(r'"?booking_url"?\s*:\s*["\']?(https?://[^\s"\'}]+)', text, re.I)
    return {
        "availability_status": s.group(1).upper() if s else "",
        "booking_url": u.group(1).rstrip(',"\'') if u else "",
    }

def call_availability(inp):
    service = inp["service"]
    if service not in AVAILABLE_SERVICES:
        raise ValueError("Unsupported CPS availability service")
    payload = dict(inp)
    if service in SERVICE_DURATIONS:
        start = datetime.fromisoformat(inp["requested_start"])
        payload["requested_end"] = (start + timedelta(minutes=SERVICE_DURATIONS[service])).isoformat()
    elif not payload.get("requested_end"):
        raise ValueError("requested_end is required")
    payload = {k: payload[k] for k in ["action", "service", "requested_start", "requested_end", "date_after", "date_before"]}
    if payload["action"] != "check_availability":
        raise ValueError("Invalid availability action")
    r = requests.post(MAKE_AVAILABILITY_WEBHOOK_URL, json=payload, timeout=30)
    r.raise_for_status()
    p = parse_availability(r.text)
    return json.dumps({**p, "instruction": "Include exact booking_url in final response if AVAILABLE and URL exists."}, ensure_ascii=False)

def save_lead(inp):
    r = requests.post(
        MAKE_LEAD_WEBHOOK_URL,
        json={k: inp.get(k, "") for k in save_lead_tool["input_schema"]["properties"]},
        timeout=30,
    )
    r.raise_for_status()
    return r.text

def process(message, channel="Unknown", thread_id="", sender_name="", sender_email="", subject="", message_id="", received_at=""):
    msgs = get_conversation(conversation_key(channel, thread_id, message_id))
    msgs.append({
        "role": "user",
        "content": (
            f"Source: {channel}\nSender: {sender_name}\nSender email: {sender_email}\n"
            f"Subject: {subject}\nReceived at: {received_at}\nCustomer message:\n{message}"
        ),
    })
    last_status = ""
    last_url = ""
    while True:
        r = client.messages.create(
            model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6"),
            max_tokens=2500,
            system=SYSTEM_PROMPT + datetime_context(),
            tools=[availability_tool, save_lead_tool],
            messages=msgs,
        )
        msgs.append({"role": "assistant", "content": r.content})
        if r.stop_reason != "tool_use":
            answer = "".join(b.text for b in r.content if hasattr(b, "text"))
            if last_status == "AVAILABLE" and last_url and last_url not in answer:
                answer = answer.rstrip() + f"\n\nYou can complete your booking here:\n{last_url}\n\nThe slot is only confirmed once you complete the booking."
            return answer
        results = []
        for b in r.content:
            if b.type != "tool_use":
                continue
            try:
                if b.name == "check_availability":
                    result = call_availability(b.input)
                    p = parse_availability(result)
                    last_status = p["availability_status"]
                    last_url = p["booking_url"]
                elif b.name == "save_lead":
                    result = save_lead(b.input)
                else:
                    result = "Unknown tool"
            except Exception:
                result = "ERROR: action failed. Do not claim success."
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": result})
        msgs.append({"role": "user", "content": results})

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] == "/health":
            self.send_json(200, {"status": "ok", "service": "Cork Podcast Studio Claude Bridge"})
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path.split("?")[0] != "/message":
            return self.send_json(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(n).decode())
            message = data.get("message", "")
            if not message:
                raise ValueError("Missing message")
            answer = process(
                message,
                data.get("source", "Unknown"),
                data.get("thread_id", ""),
                data.get("sender_name", ""),
                data.get("sender_email", ""),
                data.get("subject", ""),
                data.get("message_id", ""),
                data.get("received_at", ""),
            )
            self.send_json(200, {
                "status": "success",
                "source": data.get("source", "Unknown"),
                "message_id": data.get("message_id", ""),
                "thread_id": data.get("thread_id", ""),
                "response": answer,
            })
        except Exception:
            self.send_json(500, {"status": "error", "error": "Bridge request failed"})

if __name__ == "__main__":
    HTTPServer((BRIDGE_HOST, BRIDGE_PORT), Handler).serve_forever()
