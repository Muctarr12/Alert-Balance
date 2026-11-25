# Balance Alerter

A small Python script that checks account balances through HTTP APIs on a cron schedule and sends a notification (email, SMS, or webhook) when a balance crosses a threshold.

## Setup

pip install -r requirements.txt

## Usage

python balance_alerter.py                  # run on schedule, using config.json
python balance_alerter.py --once           # check every alert once and exit
python balance_alerter.py --run-on-start   # check everything at startup, then keep the schedule
python balance_alerter.py -v               # debug logging

Logs are printed to the console and saved to `logs/alert_engine.log`. Press Ctrl+C to stop.

## Configuration

Create a `config.json` with `apis` (how to fetch a balance) and `alerts` (what to watch and who to notify).

```json
{
  "timezone": "UTC",
  "http_timeout": 10,

  "smtp": {
    "host": "smtp.example.com",
    "port": 587,
    "user": "me@example.com",
    "password": "secret",
    "from_addr": "alerts@example.com"
  },

  "apis": {
    "my_bank": {
      "url": "https://api.example.com/accounts/{account}/balance",
      "method": "GET",
      "headers": { "Authorization": "Bearer {token}" },
      "payload_template": {},
      "response_balance_field": "data.balance"
    }
  },

  "alerts": [
    {
      "id": "low-balance-main",
      "api_ref": "my_bank",
      "params": { "account": "12345", "token": "abc123" },
      "threshold": { "operator": "<", "value": 100 },
      "schedule_cron": "0 * * * *",
      "channels": [
        { "type": "smtp", "recipient": "me@example.com" },
        { "type": "webhook", "url": "https://hooks.example.com/alert" },
        {
          "type": "sms",
          "api_gateway": "https://sms.example.com/send",
          "recipient": "+1234567890"
        }
      ]
    }
  ]
}
```

### APIs

| Field | Required | Description |
| --- | --- | --- |
| `url` | yes | Request URL. `{placeholders}` are filled from the alert's `params`. |
| `method` | yes | HTTP method. GET/HEAD/DELETE send the payload as query params; other methods send it as JSON. |
| `response_balance_field` | yes | Dotted path to the balance in the JSON response, e.g. `data.accounts.0.balance`. |
| `headers` | no | Request headers (placeholders allowed). |
| `payload_template` | no | Query params or JSON body (placeholders allowed). |

### Alerts

| Field | Description |
| --- | --- |
| `id` | Unique name for the alert. |
| `api_ref` | Name of an entry in `apis`. |
| `params` | Values used to fill `{placeholders}` in the API config. |
| `threshold` | `operator` (`<`, `<=`, `==`, `!=`, `>`, `>=`) and a numeric `value`. The alert fires when `balance <operator>  value`. |
| `schedule_cron` | Standard 5-field cron expression, e.g. `*/15 * * * *` for every 15 minutes. |
| `channels` | List of notification channels (see below). |

### Channels

| Type | Required fields | Optional fields |
| --- | --- | --- |
| `smtp` | `recipient` (and a top-level `smtp` block with `host`, `port`, `from_addr`) | `subject`, `message` |
| `sms` | `api_gateway`, `recipient` | `message`, `payload_template`, `headers`, `method` |
| `webhook` | `url` | `payload_template`, `headers`, `method` |

SMTP options in the top-level `smtp` block: `user`, `password`, `use_ssl` (defaults to true on port 465), and `use_tls` (defaults to true when not using SSL).

For SMS payload templates, `{message}` and `{recipient}` are available as placeholders. If no template is given, `{"to": recipient, "message": message}` is sent.

## Notes

- Alerts fire on every scheduled run while the threshold is breached, so you will be notified repeatedly until the balance recovers. Choose a cron interval accordingly.
"# Alert-Balance" 
