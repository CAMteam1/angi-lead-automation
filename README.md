# 86 Dumpster - Angi Lead Automation

Automatically creates CRM customers in DRS from incoming Angi lead emails.

## How It Works

1. **Angi sends a "New Lead" email** to office@86dumpsters.com
2. **Power Automate** detects the email and POSTs the body to this app's webhook
3. **This app** parses the lead (name, phone, email, address, city, comments)
4. **Creates a CRM customer** in DRS with Company = "Angi Lead"
5. **Your team** searches "Angi Lead" in DRS → sees all new leads → creates quotes

## Endpoints

- `POST /webhook/angi-lead` — Receives email from Power Automate
- `GET /status` — Dashboard showing recent leads processed
- `GET /health` — Health check

## Webhook Format

Power Automate sends:
```json
{
  "body": "<email body text>"
}
```

Header: `X-Webhook-Secret: <configured secret>`

## Deploy to Render

1. Push this repo to GitHub
2. Create new Web Service on Render
3. Connect the GitHub repo
4. Set environment variables (DRS_API_TOKEN, DRS_DEV_KEY, WEBHOOK_SECRET)
5. Deploy

## CRM Customer Fields Created

| DRS Field | Source |
|-----------|--------|
| First Name | Parsed from email |
| Last Name | Parsed from email |
| Company | "Angi Lead" |
| Email | Parsed from email |
| Phone | Parsed from email |
| Address | Parsed from email |
| City | Matched to DRS city list |
| State | MD |
| Zip | Parsed from email |
| Reference | "from Angi" (ID 28) |
| Alternate Contact | Angi Job # |
