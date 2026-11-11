"""Push notifications to your phone.

ntfy is the default: free, no account, install the ntfy app (iOS/Android),
subscribe to a long random topic name, done. Tapping the alert opens the deal.
"""

from __future__ import annotations

import base64
import json
import logging
from urllib.parse import urlencode
from ..models import Deal
from ..sources.base import Http

log = logging.getLogger("dealhawk.notify")


class Notifier:
    type = "base"

    def __init__(self, http: Http | None = None):
        self.http = http or Http(retries=2, backoff=1.0)

    def send(self, deal: Deal) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class ConsoleNotifier(Notifier):
    type = "console"

    def __init__(self, printer=print, **kw):
        super().__init__(**kw)
        self.printer = printer

    def send(self, deal: Deal) -> None:
        fb = f"\n  rate it: dealhawk feedback {deal.id} good|bad" if deal.id else ""
        self.printer(f"\n{deal.headline}\n{deal.body}\n→ {deal.offer.url}{fb}\n")


class NtfyNotifier(Notifier):
    type = "ntfy"

    def __init__(self, topic: str, server: str = "https://ntfy.sh", token: str | None = None, **kw):
        super().__init__(**kw)
        if not topic:
            raise ValueError("ntfy: topic is required (pick something long and random — topics are public)")
        self.topic, self.server, self.token = topic, server.rstrip("/"), token

    def payload(self, deal: Deal) -> dict:
        tier = deal.score.tier
        return {
            "topic": self.topic,
            "title": deal.headline,
            "message": deal.body,
            "click": deal.open_url,
            "priority": 5 if (tier == "steal" or deal.score.glitch_suspect) else 4 if tier == "great" else 3,
            "tags": ["rotating_light" if tier == "steal" else "fire" if tier == "great" else "moneybag"],
            "actions": self.actions(deal),
            **({"attach": deal.offer.image} if deal.offer.image else {}),
        }

    @staticmethod
    def actions(deal: Deal) -> list[dict]:
        acts = [{"action": "view", "label": "🛒 Open deal", "url": deal.open_url, "clear": True}]
        if deal.links:   # ntfy allows 3 buttons; these POST straight to DealHawk's feedback endpoint
            acts += [{"action": "http", "label": "👍 Good deal", "url": deal.links["good"], "method": "POST", "clear": True},
                     {"action": "http", "label": "👎 Not for me", "url": deal.links["bad"], "method": "POST", "clear": True}]
        return acts

    def send(self, deal: Deal) -> None:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        self.http.request(self.server, {"Content-Type": "application/json", **headers},
                          json.dumps(self.payload(deal)).encode())


class TelegramNotifier(Notifier):
    type = "telegram"

    def __init__(self, bot_token: str, chat_id: str | int, **kw):
        super().__init__(**kw)
        if not bot_token or not chat_id:
            raise ValueError("telegram: bot_token and chat_id are required")
        self.bot_token, self.chat_id = bot_token, chat_id

    def send(self, deal: Deal) -> None:
        self.http.post_json(f"https://api.telegram.org/bot{self.bot_token}/sendMessage", {
            "chat_id": self.chat_id,
            "text": f"{deal.headline}\n\n{deal.body}\n\n{deal.offer.url}",
            "disable_web_page_preview": False,
            "reply_markup": {"inline_keyboard": self.keyboard(deal)},
        })

    @staticmethod
    def keyboard(deal: Deal) -> list[list[dict]]:
        rows = [[{"text": "🛒 Open deal", "url": deal.open_url}]]
        if deal.links:
            rows.append([{"text": "👍 Good deal", "url": deal.links["good"]},
                         {"text": "👎 Not for me", "url": deal.links["bad"]}])
        return rows


class DiscordNotifier(Notifier):
    type = "discord"

    def __init__(self, webhook_url: str, **kw):
        super().__init__(**kw)
        if not webhook_url:
            raise ValueError("discord: webhook_url is required")
        self.webhook_url = webhook_url

    def send(self, deal: Deal) -> None:
        desc = deal.body
        if deal.links:
            desc += f"\n[👍 Good deal]({deal.links['good']}) · [👎 Not for me]({deal.links['bad']})"
        embed = {"title": deal.headline[:256], "description": desc[:4000], "url": deal.open_url}
        if deal.offer.image:
            embed["thumbnail"] = {"url": deal.offer.image}
        body = json.dumps({"embeds": [embed]}).encode()
        self.http.request(self.webhook_url, {"Content-Type": "application/json"}, body)


class PushoverNotifier(Notifier):
    type = "pushover"

    def __init__(self, app_token: str, user_key: str, **kw):
        super().__init__(**kw)
        if not app_token or not user_key:
            raise ValueError("pushover: app_token and user_key are required")
        self.app_token, self.user_key = app_token, user_key

    def send(self, deal: Deal) -> None:
        self.http.post_json("https://api.pushover.net/1/messages.json", {
            "token": self.app_token, "user": self.user_key,
            "title": deal.headline, "message": deal.body,
            "url": deal.open_url, "url_title": "Open deal",
            "priority": 1 if deal.score.tier == "steal" else 0,
        })


def whatsapp_text(deal: Deal) -> str:
    lines = [f"*{deal.headline}*", deal.body, "", f"🛒 {deal.open_url}"]
    if deal.id:
        lines.append(f'_Reply "good {deal.id}" or "bad {deal.id}" to train me._')
    return "\n".join(lines)


class WhatsAppNotifier(Notifier):
    """WhatsApp alerts with the product link (link preview on).

    provider:
      callmebot — free, personal use. Send "I allow callmebot to send me messages" to the
                  CallMeBot number (see callmebot.com) to get an apikey. Outbound only.
      twilio    — Twilio WhatsApp (sandbox is free to try). Supports reply-to-train.
      cloud     — Meta's official WhatsApp Cloud API. Supports reply-to-train.

    Twilio/Meta only allow free-form messages within 24h of your last message to the bot
    (WhatsApp's rule). Send it any message (e.g. "hi") once a day, or set `template` to an
    approved template name for alerts outside that window (Cloud API).
    """

    type = "whatsapp"

    def __init__(self, provider: str, to: str, apikey: str | None = None,
                 account_sid: str | None = None, auth_token: str | None = None, from_number: str | None = None,
                 phone_number_id: str | None = None, access_token: str | None = None,
                 template: str | None = None, template_lang: str = "en_US", api_version: str = "v21.0", **kw):
        super().__init__(**kw)
        self.provider, self.to = provider, to.replace("whatsapp:", "").strip()
        need = {"callmebot": {"apikey": apikey},
                "twilio": {"account_sid": account_sid, "auth_token": auth_token, "from_number": from_number},
                "cloud": {"phone_number_id": phone_number_id, "access_token": access_token}}
        if provider not in need:
            raise ValueError(f"whatsapp: provider must be one of {sorted(need)}")
        missing = [k for k, v in need[provider].items() if not v]
        if missing or not self.to:
            raise ValueError(f"whatsapp ({provider}): missing {missing + ([] if self.to else ['to'])}")
        self.apikey, self.account_sid, self.auth_token = apikey, account_sid, auth_token
        self.from_number = (from_number or "").replace("whatsapp:", "")
        self.phone_number_id, self.access_token = phone_number_id, access_token
        self.template, self.template_lang, self.api_version = template, template_lang, api_version

    def request_for(self, deal: Deal) -> tuple[str, dict, bytes | None]:
        """(url, headers, body) — split out so it can be tested without sending."""
        text = whatsapp_text(deal)
        if self.provider == "callmebot":
            # CallMeBot's documented format keeps the country-code "+": phone=+34111111111
            q = urlencode({"phone": "+" + "".join(c for c in self.to if c.isdigit()), "text": text,
                           "apikey": self.apikey})
            return f"https://api.callmebot.com/whatsapp.php?{q}", {}, None
        if self.provider == "twilio":
            auth = base64.b64encode(f"{self.account_sid}:{self.auth_token}".encode()).decode()
            to = "+" + "".join(c for c in self.to if c.isdigit())
            body = urlencode({"From": f"whatsapp:{self.from_number}", "To": f"whatsapp:{to}", "Body": text})
            return (f"https://api.twilio.com/2010-04-01/Accounts/{self.account_sid}/Messages.json",
                    {"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
                    body.encode())
        if self.template:
            msg = {"type": "template", "template": {
                "name": self.template, "language": {"code": self.template_lang},
                "components": [{"type": "body", "parameters": [
                    {"type": "text", "text": deal.headline[:200]},
                    {"type": "text", "text": deal.offer.title[:200]},
                    {"type": "text", "text": deal.open_url}]}]}}
        else:
            msg = {"type": "text", "text": {"preview_url": True, "body": text[:4096]}}
        return (f"https://graph.facebook.com/{self.api_version}/{self.phone_number_id}/messages",
                {"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json"},
                json.dumps({"messaging_product": "whatsapp", "to": "".join(c for c in self.to if c.isdigit()),
                            **msg}).encode())

    def send(self, deal: Deal) -> None:
        url, headers, body = self.request_for(deal)
        self.http.request(url, headers, body)


NOTIFIER_TYPES: dict[str, type[Notifier]] = {
    c.type: c for c in (ConsoleNotifier, NtfyNotifier, TelegramNotifier, DiscordNotifier, PushoverNotifier,
                        WhatsAppNotifier)
}
