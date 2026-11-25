import argparse
import json
import logging
import operator
import signal
import smtplib
import ssl
import sys
import time
from email.message import EmailMessage
from pathlib import Path

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

log = logging.getLogger("alerter")

OPERATORS = {
    "<": operator.lt, "<=": operator.le, "==": operator.eq,
    "!=": operator.ne, ">": operator.gt, ">=": operator.ge,
}


class AlertError(Exception):
    """Any config, API, or notification problem."""




def load_config(path):
    try:
        with open(path, encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise AlertError(f"Cannot load {path}: {exc}") from exc

    apis, alerts = config.get("apis"), config.get("alerts")
    if not isinstance(apis, dict) or not isinstance(alerts, list):
        raise AlertError("Config needs an 'apis' object and an 'alerts' array")

    for name, api in apis.items():
        for field in ("url", "method", "response_balance_field"):
            if field not in api:
                raise AlertError(f"API '{name}' is missing '{field}'")

    for alert in alerts:
        for field in ("id", "api_ref", "params", "threshold", "schedule_cron", "channels"):
            if field not in alert:
                raise AlertError(f"Alert {alert.get('id', '?')} is missing '{field}'")
        if alert["api_ref"] not in apis:
            raise AlertError(f"Alert '{alert['id']}' uses unknown api_ref '{alert['api_ref']}'")
        threshold = alert["threshold"]
        if threshold.get("operator") not in OPERATORS:
            raise AlertError(f"Alert '{alert['id']}': operator must be one of {list(OPERATORS)}")
        if not isinstance(threshold.get("value"), (int, float)):
            raise AlertError(f"Alert '{alert['id']}': threshold value must be a number")

    return config



def fill(value, params):
   
    if isinstance(value, str):
        try:
            return value.format(**params)
        except KeyError as exc:
            raise AlertError(f"Missing parameter {exc} in {value!r}") from exc
    if isinstance(value, dict):
        return {k: fill(v, params) for k, v in value.items()}
    if isinstance(value, list):
        return [fill(v, params) for v in value]
    return value


def dig(data, path):
    
    for key in path.split("."):
        try:
            data = data[int(key)] if isinstance(data, list) else data[key]
        except (KeyError, IndexError, ValueError, TypeError):
            return None
    return data


def fetch_balance(api, params, timeout):
    method = api["method"].upper()
    url = fill(api["url"], params)
    headers = fill(api.get("headers", {}), params)
    payload = fill(api.get("payload_template", {}), params)
    body = {"params": payload} if method in ("GET", "HEAD", "DELETE") else {"json": payload}

    try:
        response = requests.request(method, url, headers=headers, timeout=timeout, **body)
        response.raise_for_status()
        return float(dig(response.json(), api["response_balance_field"]))
    except (requests.RequestException, ValueError, TypeError) as exc:
        raise AlertError(f"Could not get a balance from {url}: {exc}") from exc


def default_message(alert, balance):
    t = alert["threshold"]
    account = alert["params"].get("account", "n/a")
    return (f"[{alert['id']}] Threshold breached for '{alert['api_ref']}'. "
            f"Balance {balance} {t['operator']} {t['value']} (account: {account}).")


def send_email(alert, channel, balance, smtp):
    if not channel.get("recipient"):
        raise AlertError("Email channel needs 'recipient'")
    missing = {"host", "port", "from_addr"} - smtp.keys()
    if missing:
        raise AlertError(f"SMTP config missing: {sorted(missing)}")

    msg = EmailMessage()
    msg["From"] = smtp["from_addr"]
    msg["To"] = channel["recipient"]
    msg["Subject"] = channel.get("subject") or f"[ALERT] {alert['id']} threshold breached"
    msg.set_content(channel.get("message") or default_message(alert, balance))

    host, port = smtp["host"], int(smtp["port"])
    use_ssl = smtp.get("use_ssl", port == 465)
    use_tls = smtp.get("use_tls", not use_ssl)
    context = ssl.create_default_context()

    if use_ssl:
        server = smtplib.SMTP_SSL(host, port, timeout=10, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=10)

    with server:
        if not use_ssl:
            server.ehlo()
            if use_tls:
                server.starttls(context=context)
                server.ehlo()
        if smtp.get("user") and smtp.get("password"):
            server.login(smtp["user"], smtp["password"])
        server.send_message(msg)


def send_sms(alert, channel, balance, smtp):
    if not channel.get("api_gateway") or not channel.get("recipient"):
        raise AlertError("SMS channel needs 'api_gateway' and 'recipient'")

    message = channel.get("message") or default_message(alert, balance)
    template = channel.get("payload_template") or {"to": "{recipient}", "message": "{message}"}
    payload = fill(template, {"message": message, "recipient": channel["recipient"]})
    post_json(channel["api_gateway"], payload, channel)


def send_webhook(alert, channel, balance):
    """Send a webhook notification for the alert."""
    if not channel.get("url"):
        raise AlertError("Webhook channel needs 'url'")

    payload = channel.get("payload_template") or {
        "alert_id": alert["id"],
        "api_ref": alert["api_ref"],
        "params": alert["params"],
        "threshold": alert["threshold"],
        "observed_balance": balance,
        "message": default_message(alert, balance),
    }
    post_json(channel["url"], payload, channel)


def post_json(url, payload, channel):
    response = requests.request(
        channel.get("method", "POST").upper(), url, json=payload,
        headers=channel.get("headers", {"Content-Type": "application/json"}), timeout=10,
    )
    response.raise_for_status()


SENDERS = {
    "smtp": send_email,
    "sms": send_sms,
    "webhook": lambda alert, channel, balance, smtp: send_webhook(alert, channel, balance)
}
SEND_ERRORS = (AlertError, requests.RequestException, smtplib.SMTPException, OSError)


def run_alert(config, alert):
    alert_id, t = alert["id"], alert["threshold"]

    try:
        balance = fetch_balance(config["apis"][alert["api_ref"]], alert["params"],
                                config.get("http_timeout", 10))
    except AlertError as exc:
        log.error("[%s] %s", alert_id, exc)
        return

    log.info("[%s] balance = %s", alert_id, balance)
    if not OPERATORS[t["operator"]](balance, float(t["value"])):
        return

    log.warning("[%s] THRESHOLD BREACHED (%s %s %s)", alert_id, balance, t["operator"], t["value"])

    for channel in alert["channels"]:
        kind = channel.get("type")
        try:
            if kind not in SENDERS:
                raise AlertError(f"Unsupported channel type: {kind}")
            SENDERS[kind](alert, channel, balance, config.get("smtp", {}))
            log.info("[%s] %s: sent", alert_id, kind)
        except SEND_ERRORS as exc:
            log.error("[%s] %s: FAILED (%s)", alert_id, kind, exc)


def start_scheduler(config, run_on_start):
    scheduler = BackgroundScheduler(timezone=config.get("timezone", "UTC"))

    for alert in config["alerts"]:
        try:
            trigger = CronTrigger.from_crontab(alert["schedule_cron"], timezone=scheduler.timezone)
        except ValueError as exc:
            raise AlertError(f"Alert '{alert['id']}' has an invalid cron: {exc}") from exc

        scheduler.add_job(run_alert, trigger, args=[config, alert], id=alert["id"],
                          max_instances=1, coalesce=True, misfire_grace_time=60)
        log.info("Scheduled '%s' (%s)", alert["id"], alert["schedule_cron"])

    scheduler.start()
    if run_on_start:
        for alert in config["alerts"]:
            run_alert(config, alert)
    return scheduler



def main():
    parser = argparse.ArgumentParser(description="Balance alert system")
    parser.add_argument("-c", "--config", default="config.json", help="Path to JSON config")
    parser.add_argument("--once", action="store_true", help="Run all alerts once and exit")
    parser.add_argument("--run-on-start", action="store_true", help="Also run all alerts at startup")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    Path("logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("logs/alert_engine.log")],
    )

    try:
        config = load_config(args.config)
        if args.once:
            for alert in config["alerts"]:
                run_alert(config, alert)
            return
        scheduler = start_scheduler(config, args.run_on_start)
    except AlertError as exc:
        log.error("%s", exc)
        sys.exit(1)

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    log.info("Running. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        log.info("Stopped.")


if __name__ == "__main__":
    main()
