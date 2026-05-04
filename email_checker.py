"""
Verifica a caixa de entrada via IMAP à procura de emails não lidos do Yescapa
que indiquem uma nova reserva ou pedido de reserva.
"""

import imaplib
import os
import email as email_lib
from email.header import decode_header

IMAP_HOST   = os.getenv("IMAP_HOST", "imap.gmail.com")
IMAP_PORT   = int(os.getenv("IMAP_PORT", "993"))
IMAP_USER   = os.environ["IMAP_USER"]
IMAP_PASS   = os.environ["IMAP_PASS"]

# Remetentes conhecidos do Yescapa (separados por vírgula no .env)
YESCAPA_SENDERS = [
    s.strip()
    for s in os.getenv(
        "YESCAPA_SENDERS",
        "no-reply@yescapa.com,noreply@yescapa.com,reservations@yescapa.com,"
        "no-reply@yescapa.pt,contact@yescapa.com",
    ).split(",")
]

# Palavras-chave no assunto que indicam nova reserva (case-insensitive)
BOOKING_KEYWORDS = [
    s.strip()
    for s in os.getenv(
        "BOOKING_KEYWORDS",
        "nouvelle réservation,new booking,nueva reserva,nova reserva,"
        "booking request,demande de réservation,pedido de reserva",
    ).split(",")
]


def _connect() -> imaplib.IMAP4_SSL:
    mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    mail.login(IMAP_USER, IMAP_PASS)
    return mail


def _decode_subject(msg) -> str:
    raw = msg.get("Subject", "")
    parts = decode_header(raw)
    subject = ""
    for part, enc in parts:
        if isinstance(part, bytes):
            subject += part.decode(enc or "utf-8", errors="replace")
        else:
            subject += part
    return subject.lower()


def has_new_booking_email() -> bool:
    """
    Devolve True se existir pelo menos um email não lido do Yescapa
    sobre uma nova reserva.
    """
    try:
        mail = _connect()
        mail.select("INBOX")

        # Pesquisar emails não lidos de qualquer remetente Yescapa
        msg_ids = set()
        for sender in YESCAPA_SENDERS:
            _, data = mail.search(None, f'(UNSEEN FROM "{sender}")')
            if data and data[0]:
                msg_ids.update(data[0].split())

        if not msg_ids:
            mail.logout()
            return False

        # Verificar assunto para confirmar que é sobre reserva
        for mid in msg_ids:
            _, data = mail.fetch(mid, "(RFC822.HEADER)")
            msg = email_lib.message_from_bytes(data[0][1])
            subject = _decode_subject(msg)
            sender  = msg.get("From", "").lower()

            # Se o assunto contém palavra-chave de reserva → disparar
            if any(kw in subject for kw in BOOKING_KEYWORDS):
                print(f"  Email de reserva detectado: '{subject}' de '{sender}'")
                mail.logout()
                return True

            # Se não há palavras-chave mas o remetente é Yescapa → disparar na mesma
            # (alguns emails de confirmação têm assuntos genéricos)
            if any(s in sender for s in YESCAPA_SENDERS):
                print(f"  Email Yescapa detectado (sem keyword): '{subject}'")
                mail.logout()
                return True

        mail.logout()
        return False

    except Exception as e:
        print(f"Erro ao verificar email: {e}")
        return False


def mark_booking_emails_read() -> None:
    """Marca como lidos os emails não lidos do Yescapa."""
    try:
        mail = _connect()
        mail.select("INBOX")

        for sender in YESCAPA_SENDERS:
            _, data = mail.search(None, f'(UNSEEN FROM "{sender}")')
            if data and data[0]:
                for mid in data[0].split():
                    mail.store(mid, "+FLAGS", "\\Seen")

        mail.logout()
    except Exception as e:
        print(f"Erro ao marcar emails: {e}")
