"""Processing the mailbox: bank notifications, documents in attachments, the rest.

* Bank notification -> bank movement, automatic matching -> moved to "Zpracováno".
* Message with a PDF, photo or ISDOC attachment -> received documents "ke kontrole"
  -> moved to "Zpracováno". A PDF is also found when the mail client puts it inline or
  nests it (Apple Mail forwards), images only when they are real attachments.
* Anything else stays in the inbox for the user, is remembered, so it is not read
  again on the next run, and is listed as an unknown mail on the dashboard until the
  user dismisses it.
"""

from __future__ import annotations

import email
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from email import policy
from email.message import EmailMessage
from typing import Any

from app.banks import parser_for_email
from app.banks.book import BankBook, BankError, MatchState
from app.config import FirmaConfig, Settings
from app.ingest.files import UploadError, detect
from app.mail.imap import Mailbox, MailError
from app.received import DocSource, ReceivedDocs
from app.storage import Storage
from app.storage.atomic import atomic_write_text, file_lock

log = logging.getLogger(__name__)

STATE_FILE = "posta.json"
MAX_SEEN = 5000
MAX_UNKNOWN = 100


@dataclass
class FetchResult:
    started: str = ""
    notifications: int = 0
    matched: int = 0
    documents: int = 0
    other: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "cas": self.started,
            "notifikace": self.notifications,
            "sparovano": self.matched,
            "doklady": self.documents,
            "ostatni": self.other,
            "chyby": self.errors,
        }


class MailProcessor:
    def __init__(self, storage: Storage, firma: FirmaConfig, settings: Settings) -> None:
        self.storage = storage
        self.firma = firma
        self.settings = settings
        self.state_path = storage.data_dir / STATE_FILE

    # -- state ---------------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_state(
        self, seen: list[str], result: FetchResult, unknown: list[dict[str, str]]
    ) -> None:
        data = {
            "videne": seen[-MAX_SEEN:],
            "posledni_beh": result.as_dict(),
            "nezname": unknown[-MAX_UNKNOWN:],
        }
        atomic_write_text(self.state_path, json.dumps(data, ensure_ascii=False, indent=1) + "\n")

    def unknown(self) -> list[dict[str, str]]:
        """Mails in which nothing was recognised, newest first, until dismissed."""
        items = self.state().get("nezname", [])
        return [i for i in reversed(items) if isinstance(i, dict)]

    def dismiss_unknown(self, key: str | None = None) -> None:
        """Forget one unknown mail (by its key) or all of them; the mail stays in the inbox."""
        with file_lock(self.storage.data_dir / "posta"):
            data = self.state()
            if not data:
                return
            items = data.get("nezname", [])
            data["nezname"] = [] if key is None else [i for i in items if i.get("klic") != key]
            atomic_write_text(
                self.state_path, json.dumps(data, ensure_ascii=False, indent=1) + "\n"
            )

    # -- run -----------------------------------------------------------------------

    def run(self, mailbox: Mailbox) -> FetchResult:
        result = FetchResult(started=datetime.now().astimezone().isoformat(timespec="seconds"))
        self.storage.data_dir.mkdir(parents=True, exist_ok=True)
        with file_lock(self.storage.data_dir / "posta"):
            state = self.state()
            seen = list(state.get("videne", []))
            seen_set = set(seen)
            unknown = [i for i in state.get("nezname", []) if isinstance(i, dict)]
            try:
                with mailbox as box:
                    for raw in box.messages():
                        msg = email.message_from_bytes(raw.data, policy=policy.default)
                        assert isinstance(msg, EmailMessage)  # noqa: S101
                        key = self._key(msg, raw.data)
                        if key in seen_set:
                            continue
                        try:
                            handled = self._process(msg, result)
                        except Exception as exc:
                            log.warning("Mail processing failed: %s", type(exc).__name__)
                            result.errors.append(f"Zpráva {raw.uid}: {type(exc).__name__}")
                            handled = False
                        if handled:
                            box.move(raw.uid, self.settings.imap_done_folder)
                        else:
                            result.other += 1
                            unknown.append(self._describe(msg, key, result.started))
                        seen.append(key)
                        seen_set.add(key)
            except MailError as exc:
                result.errors.append(str(exc))
            self._save_state(seen, result, unknown)
        log.info(
            "Mail fetch: %d notifications (%d matched), %d documents, %d other, %d errors",
            result.notifications,
            result.matched,
            result.documents,
            result.other,
            len(result.errors),
        )
        return result

    @staticmethod
    def _key(msg: EmailMessage, data: bytes) -> str:
        message_id = str(msg.get("Message-ID", "")).strip()
        return message_id or "sha256:" + hashlib.sha256(data).hexdigest()

    @staticmethod
    def _describe(msg: EmailMessage, key: str, received: str) -> dict[str, str]:
        def header(name: str) -> str:
            try:
                return str(msg.get(name, "") or "").strip()[:200]
            except Exception:  # malformed header must not stop the run
                return ""

        return {
            "klic": key,
            "od": header("From"),
            "predmet": header("Subject"),
            "datum": header("Date"),
            "stazeno": received,
        }

    @staticmethod
    def _documents(msg: EmailMessage) -> list[bytes]:
        """Real attachments of any accepted type plus every PDF anywhere in the message.

        Apple Mail and others put a forwarded PDF inline and nest it in
        multipart/alternative, where ``iter_attachments`` does not look. Inline images
        are skipped on purpose (logos, signatures, icons).
        """
        found: list[bytes] = []
        for part in msg.iter_attachments():
            data = part.get_payload(decode=True)
            if isinstance(data, bytes):
                found.append(data)
        for part in msg.walk():
            if part.is_multipart() or part.get_content_maintype() == "text":
                continue
            data = part.get_payload(decode=True)
            if isinstance(data, bytes) and data.startswith(b"%PDF-") and data not in found:
                found.append(data)
        return found

    def _process(self, msg: EmailMessage, result: FetchResult) -> bool:
        parser = parser_for_email(msg)
        if parser is not None:
            movement = parser.parse_email(msg)
            if movement is None:
                return False
            book = BankBook(self.storage, self.firma)
            try:
                record, _ = book.import_movement(movement)
            except BankError as exc:
                result.errors.append(str(exc))
                return False
            result.notifications += 1
            if book.auto_match(record)["stav"] == MatchState.MATCHED:
                result.matched += 1
            return True
        docs = ReceivedDocs(self.storage, self.firma)
        created = 0
        for data in self._documents(msg):
            if len(data) > self.settings.max_upload_bytes:
                continue
            try:
                detect(data)
            except UploadError:
                continue
            docs.upload(data, DocSource.EMAIL)
            created += 1
        result.documents += created
        return created > 0
