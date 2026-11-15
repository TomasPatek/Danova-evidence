"""Mailbox processing with an in-memory mailbox (no network)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path
from types import TracebackType

import pytest

from app.banks.book import BankBook
from app.config import FirmaConfig, Settings
from app.contacts import KONTAKTY_SCHEMA
from app.invoices.service import DraftLine, InvoiceDraft, InvoiceService
from app.mail.fetch import MailProcessor
from app.mail.imap import MailError, RawMessage
from app.mail.scheduler import start_scheduler
from app.received import ReceivedDocs
from app.storage import Storage

AIRBANK = Path(__file__).parents[1] / "fixtures" / "airbank"
DOKLADY = Path(__file__).parents[1] / "fixtures" / "doklady"


class FakeMailbox:
    def __init__(self, messages: list[bytes], fail: bool = False) -> None:
        self.inbox = {str(i + 1): data for i, data in enumerate(messages)}
        self.moved: dict[str, list[str]] = {}
        self.fail = fail

    def __enter__(self) -> FakeMailbox:
        if self.fail:
            raise MailError("Připojení ke schránce selhalo (TimeoutError).")
        return self

    def __exit__(
        self, t: type[BaseException] | None, e: BaseException | None, tb: TracebackType | None
    ) -> None:
        pass

    def messages(self) -> Iterator[RawMessage]:
        for uid, data in list(self.inbox.items()):
            yield RawMessage(uid, data)

    def move(self, uid: str, folder: str) -> None:
        self.moved.setdefault(folder, []).append(uid)
        del self.inbox[uid]


@pytest.fixture
def firma() -> FirmaConfig:
    cfg = FirmaConfig()
    cfg.firma.ico = "99999994"
    cfg.banka.ucet = "1000000005/3030"
    cfg.evidence.zacatek = "2026-01-01"
    return cfg


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    s = Storage(tmp_path / "data")
    s.table(KONTAKTY_SCHEMA).insert(
        {
            "id": "K0001",
            "typ": "odběratel",
            "ico": "",
            "dic": "",
            "nazev": "Petr Zákazník",
            "ulice": "",
            "mesto": "",
            "psc": "",
            "email": "",
            "telefon": "",
            "poznamka": "",
            "ares_aktualizovano": None,
        }
    )
    return s


@pytest.fixture
def processor(storage: Storage, firma: FirmaConfig, tmp_path: Path) -> MailProcessor:
    settings = Settings(data_dir=storage.data_dir, config_dir=tmp_path / "config", secret_key="k")
    return MailProcessor(storage, firma, settings)


def other_mail(subject: str = "Pozvánka na veletrh") -> bytes:
    msg = EmailMessage()
    msg["From"] = "obchod@example.cz"
    msg["Subject"] = subject
    msg["Message-ID"] = f"<{abs(hash(subject))}@example.cz>"
    msg.set_content("Dobrý den, zveme vás.")
    return msg.as_bytes()


def doc_mail() -> bytes:
    msg = EmailMessage()
    msg["From"] = "faktury@dily-vzor.example"
    msg["Subject"] = "Faktura 2027000123"
    msg["Message-ID"] = "<faktura-1@dily-vzor.example>"
    msg.set_content("V příloze posíláme fakturu.")
    msg.add_attachment(
        (DOKLADY / "faktura-radky.pdf").read_bytes(),
        maintype="application",
        subtype="pdf",
        filename="faktura.pdf",
    )
    msg.add_attachment(
        b"MZ\x90\x00", maintype="application", subtype="octet-stream", filename="x.exe"
    )
    return msg.as_bytes()


def test_notification_is_imported_matched_and_moved(
    processor: MailProcessor, storage: Storage, firma: FirmaConfig
) -> None:
    svc = InvoiceService(storage, firma)
    inv = svc.issue(
        InvoiceDraft(
            "K0001",
            date(2026, 10, 1),
            date(2026, 10, 15),
            [DraftLine("Práce", Decimal("1"), "ks", Decimal("4100"))],
        )
    )
    assert inv["cislo"] == "20260001"
    box = FakeMailbox([(AIRBANK / "prichozi-uhrada.eml").read_bytes()])
    result = processor.run(box)
    assert (result.notifications, result.matched, result.other) == (1, 1, 0)
    assert box.moved == {"Zpracováno": ["1"]}
    assert svc.get("20260001")["stav"] == "uhrazena"  # type: ignore[index]


def test_all_samples_and_duplicates(
    processor: MailProcessor, storage: Storage, firma: FirmaConfig
) -> None:
    samples = [p.read_bytes() for p in sorted(AIRBANK.glob("*.eml"))]
    # The same notification delivered twice (different Message-ID) is stored once.
    duplicate = samples[0].replace(b"Message-ID: <n", b"Message-ID: <dup-n")
    result = processor.run(FakeMailbox([*samples, duplicate]))
    assert result.notifications == len(samples) + 1
    assert len(BankBook(storage, firma).all()) == len(samples)


def test_attachment_goes_to_review_queue(
    processor: MailProcessor, storage: Storage, firma: FirmaConfig
) -> None:
    box = FakeMailbox([doc_mail()])
    result = processor.run(box)
    assert result.documents == 1 and box.moved == {"Zpracováno": ["1"]}
    (doc,) = ReceivedDocs(storage, firma).queue()
    assert doc["zdroj"] == "e-mail" and doc["castka"] == Decimal("1500.00")


def test_other_mail_stays_and_is_not_read_twice(processor: MailProcessor) -> None:
    box = FakeMailbox([other_mail()])
    assert processor.run(box).other == 1
    assert box.inbox and not box.moved
    assert processor.run(box).other == 0  # remembered


def test_notification_for_other_account_stays(processor: MailProcessor, firma: FirmaConfig) -> None:
    firma.banka.ucet = "2000000018/3030"
    box = FakeMailbox([(AIRBANK / "prichozi-uhrada.eml").read_bytes()])
    result = processor.run(box)
    assert result.notifications == 0 and "jinému účtu" in result.errors[0]
    assert not box.moved


def test_connection_error_is_reported(processor: MailProcessor) -> None:
    result = processor.run(FakeMailbox([], fail=True))
    assert result.errors == ["Připojení ke schránce selhalo (TimeoutError)."]
    assert processor.state()["posledni_beh"]["chyby"] == result.errors


def test_one_broken_message_does_not_stop_the_run(
    processor: MailProcessor, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}
    original = processor._process

    def flaky(msg, result):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return original(msg, result)

    monkeypatch.setattr(processor, "_process", flaky)
    box = FakeMailbox([other_mail("a"), doc_mail()])
    result = processor.run(box)
    assert result.errors == ["Zpráva 1: RuntimeError"] and result.documents == 1


def test_scheduler_only_when_configured(storage: Storage, tmp_path: Path) -> None:
    base = Settings(data_dir=storage.data_dir, config_dir=tmp_path, secret_key="k")
    assert start_scheduler(base, storage, lambda: FakeMailbox([])) is None
    configured = Settings(
        data_dir=storage.data_dir,
        config_dir=tmp_path,
        secret_key="k",
        imap_host="imap.example.cz",
        imap_user="u",
        imap_password="p",
    )
    scheduler = start_scheduler(configured, storage, lambda: FakeMailbox([]))
    assert scheduler is not None
    try:
        job = scheduler.get_job("posta")
        assert job is not None and job.trigger.interval.total_seconds() == 15 * 60
    finally:
        scheduler.shutdown(wait=False)


def test_scheduler_with_backup_only(storage: Storage, tmp_path: Path) -> None:
    settings = Settings(
        data_dir=storage.data_dir,
        config_dir=tmp_path,
        secret_key="k",
        backup_repository=str(tmp_path / "repo"),
        restic_password="x",
        backup_time="03:15",
    )
    scheduler = start_scheduler(settings, storage, lambda: FakeMailbox([]))
    assert scheduler is not None
    try:
        assert scheduler.get_job("posta") is None
        job = scheduler.get_job("zaloha")
        assert (
            job is not None and "hour='3'" in str(job.trigger) and "minute='15'" in str(job.trigger)
        )
    finally:
        scheduler.shutdown(wait=False)


def apple_forward_mail() -> bytes:
    """Apple Mail forward: PDF inline, nested in multipart/alternative > multipart/mixed."""
    from email.mime.application import MIMEApplication
    from email.mime.image import MIMEImage
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    mixed = MIMEMultipart("mixed")
    mixed.attach(MIMEText("<p>Přeposlaná zpráva</p>", "html", "utf-8"))
    icon = MIMEImage(b"\x89PNG\r\n\x1a\n" + b"\0" * 64, "png")
    icon.add_header("Content-Disposition", "inline", filename="mime-attachment.png")
    mixed.attach(icon)
    mixed.attach(MIMEText("<p></p>", "html", "utf-8"))
    pdf = MIMEApplication((DOKLADY / "faktura-radky.pdf").read_bytes(), "pdf")
    pdf.add_header("Content-Disposition", "inline", filename="2636398.pdf")
    mixed.attach(pdf)
    mixed.attach(MIMEText("<p></p>", "html", "utf-8"))
    outer = MIMEMultipart("alternative")
    outer.attach(mixed)
    outer["From"] = "Tomáš <info@example.cz>"
    outer["Subject"] = "Fwd: Zálohová faktura"
    outer["Message-ID"] = "<fwd-1@example.cz>"
    return outer.as_bytes()


def test_inline_nested_pdf_is_found_but_inline_image_is_not(
    processor: MailProcessor, storage: Storage, firma: FirmaConfig
) -> None:
    box = FakeMailbox([apple_forward_mail()])
    result = processor.run(box)
    assert (result.documents, result.other) == (1, 0)
    assert box.moved == {"Zpracováno": ["1"]}
    (doc,) = ReceivedDocs(storage, firma).queue()
    assert doc["castka"] == Decimal("1500.00")
    assert processor.unknown() == []


def test_unknown_mail_is_listed_until_dismissed(processor: MailProcessor) -> None:
    processor.run(FakeMailbox([other_mail("a"), other_mail("b"), doc_mail()]))
    unknown = processor.unknown()
    assert [m["predmet"] for m in unknown] == ["b", "a"]  # newest first
    assert unknown[0]["od"] == "obchod@example.cz" and unknown[0]["klic"]
    processor.run(FakeMailbox([other_mail("a")]))  # already seen: not listed twice
    assert len(processor.unknown()) == 2
    processor.dismiss_unknown(unknown[0]["klic"])
    assert [m["predmet"] for m in processor.unknown()] == ["a"]
    processor.dismiss_unknown()
    assert processor.unknown() == []
    assert len(processor.state()["videne"]) == 3  # still remembered as seen
