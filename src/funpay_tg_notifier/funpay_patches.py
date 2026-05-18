"""Runtime monkey-patches for upstream FunPayAPI bugs.

FunPay periodically changes the HTML structure of their chat messages. The
upstream library's ``Account.__parse_messages`` blindly does
``parser.find("div", {"class": "message-text"}).text`` (and similar for system
alerts). When FunPay omits these divs for certain message variants (image-only
messages, some new system notifications, ...) the call crashes with::

    AttributeError: 'NoneType' object has no attribute 'text'

This propagates up to ``Account.get_chat_history`` and ``Runner.listen``,
producing the runner error ``Не удалось получить истории чатов [...]: превышено
кол-во попыток.`` After 4 retries the runner gives up on that batch and the
corresponding ``NewMessageEvent``s are silently lost — so the bot never tells
the user "you got a new message".

To stay resilient against such upstream regressions we replace the
name-mangled private method ``_Account__parse_messages`` with a version that:

  * wraps each per-message parsing in ``try / except`` so a single bad message
    no longer kills the whole batch, and
  * uses defensive ``is not None`` checks on every ``parser.find`` result and
    falls back to ``parser.get_text(...)`` when neither the canonical
    ``message-text`` nor the ``alert ... alert-info`` div is present.

The patch is applied once at bot startup via :func:`apply`.
"""

from __future__ import annotations

import logging

from bs4 import BeautifulSoup
from FunPayAPI import Account, types

log = logging.getLogger(__name__)


def _patched_parse_messages(
    self,
    json_messages,
    chat_id,
    interlocutor_id=None,
    interlocutor_username=None,
    from_id: int = 0,
):
    messages: list = []
    ids = {self.id: self.username, 0: "FunPay"}
    badges: dict = {}
    if interlocutor_id is not None:
        ids[interlocutor_id] = interlocutor_username

    bot_character = getattr(self, "_Account__bot_character", "")

    for raw in json_messages:
        try:
            if raw["id"] < from_id:
                continue
            author_id = raw["author"]
            parser = BeautifulSoup(raw["html"], "html.parser")

            author_div = parser.find("div", {"class": "media-user-name"})
            if None in [ids.get(author_id), badges.get(author_id)] and author_div is not None:
                if badges.get(author_id) is None:
                    badge = author_div.find("span")
                    badges[author_id] = badge.text if badge else 0
                if ids.get(author_id) is None:
                    a_tag = author_div.find("a")
                    if a_tag is not None:
                        author = a_tag.text.strip()
                        ids[author_id] = author
                        if (
                            self.chat_id_private(chat_id)
                            and author_id == interlocutor_id
                            and not interlocutor_username
                        ):
                            interlocutor_username = author
                            ids[interlocutor_id] = interlocutor_username

            image_link = None
            message_text: str | None = None

            img_a = parser.find("a", {"class": "chat-img-link"})
            if self.chat_id_private(chat_id) and img_a is not None:
                image_link = img_a.get("href")
            else:
                if author_id == 0:
                    alert_div = parser.find(
                        "div", {"class": "alert alert-with-icon alert-info"}
                    )
                    if alert_div is not None:
                        message_text = alert_div.text.strip()
                else:
                    # FunPay has used several class names for the message body
                    # over time. The upstream library only knows about the
                    # oldest one ("message-text"); check newer variants too.
                    for cls in ("message-text", "chat-msg-text", "chat-message-text"):
                        msg_div = parser.find("div", {"class": cls})
                        if msg_div is not None:
                            message_text = msg_div.text
                            break

            if image_link is None and message_text is None:
                # Truly unknown variant — fall back to the visible text of the
                # message body so the user still sees something instead of us
                # dropping the message entirely. Strip author / timestamp lines
                # that are part of the wrapper (chat-msg-author-link,
                # chat-msg-date) so we don't surface them as the text.
                body = parser.find("div", {"class": "chat-msg-body"}) or parser
                msg_text = body.get_text(separator=" ", strip=True)
                message_text = msg_text or ""

            by_bot = False
            if (
                not image_link
                and message_text
                and bot_character
                and message_text.startswith(bot_character)
            ):
                message_text = message_text.replace(bot_character, "", 1)
                by_bot = True

            message_obj = types.Message(
                raw["id"],
                message_text,
                chat_id,
                interlocutor_username,
                None,
                author_id,
                raw["html"],
                image_link,
                determine_msg_type=False,
            )
            message_obj.by_bot = by_bot
            message_obj.type = (
                types.MessageTypes.NON_SYSTEM
                if author_id != 0
                else message_obj.get_message_type()
            )
            messages.append(message_obj)
        except Exception as exc:  # noqa: BLE001 — best-effort per-message
            log.warning(
                "Skipping unparseable FunPay message (chat=%s, id=%s): %s: %s",
                chat_id,
                raw.get("id"),
                type(exc).__name__,
                exc,
            )
            continue

    for m in messages:
        m.author = ids.get(m.author_id)
        m.chat_name = interlocutor_username
        m.badge = (
            badges.get(m.author_id)
            if badges.get(m.author_id) != 0
            else None
        )
    return messages


def apply() -> None:
    """Apply monkey patches to FunPayAPI. Idempotent."""
    if getattr(Account, "_funpay_tg_notifier_patched", False):
        return
    Account._Account__parse_messages = _patched_parse_messages  # type: ignore[attr-defined]
    Account._funpay_tg_notifier_patched = True  # type: ignore[attr-defined]
    log.info("FunPayAPI patches applied: _Account__parse_messages")
