"""
Ponto de entrada para automação cloud.

Chamado pelo Railway como cron job com um argumento:

    python run.py --email          → verifica email e corre sync se houver reserva nova,
                                     depois corre a pipeline de pós-sync (emails, contactos,
                                     notificações WhatsApp).
    python run.py --scheduled      → corre sync incondicionalmente (agendamento diário),
                                     depois corre a pipeline de pós-sync.
    python run.py --pre-check-in   → corre APENAS o envio de emails pré-check-in
                                     (sem tocar no sync; útil para testes manuais).
    python run.py --contacts       → corre APENAS a sync de Google Contacts.
    python run.py --whatsapp       → corre APENAS a notificação WhatsApp interna.

Ordem da pipeline pós-sync:
    1. pre_check_in_sender (email ao hóspede)
    2. google_contacts_sync (cria/atualiza contacto na conta ops@)
    3. whatsapp_notification (espera +2h após email; manda alerta interno para ops@)

Cada peça é independente e falha em silêncio (logs + estado na sheet).
"""

import sys
import os
from datetime import datetime, timezone


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}", flush=True)


def run_sync(trigger: str):
    log(f"A iniciar sync (trigger: {trigger})...")
    try:
        from yescapa_sheets import main
        main(trigger)
        log("Sync concluído com sucesso.")
    except Exception as e:
        log(f"Erro no sync: {e}")
        raise


def run_pre_check_in():
    """Envia emails de pré-check-in pendentes.

    Falhas aqui não interrompem o cron — o sync já correu com sucesso e os
    erros ficam registados na folha PreCheckIn para revisão manual.
    """
    log("A iniciar envio de emails pré-check-in...")
    try:
        from pre_check_in_sender import main as send_emails
        result = send_emails()
        log(f"Pré-check-in concluído: {result}")
    except Exception as e:
        log(f"Erro no pré-check-in: {e}")


def run_contacts_sync():
    """Sincroniza Google Contacts: cria/atualiza contacto para cada reserva confirmed.

    Independente do email — falha em silêncio (logs + estado em worksheet Contactos).
    """
    log("A iniciar sync Google Contacts...")
    try:
        from google_contacts_sync import main as sync_contacts
        result = sync_contacts()
        log(f"Contactos concluído: {result}")
    except Exception as e:
        log(f"Erro na sync de contactos: {e}")


def run_whatsapp_notification():
    """Envia notificação interna por email para ops@ com botão wa.me, para reservas
    cujo email pré-check-in já saiu há >= DELAY_APOS_EMAIL_MINUTES (default 120).

    Falha em silêncio (logs + estado em worksheet WhatsApp).
    """
    log("A iniciar notificação WhatsApp interna...")
    try:
        from whatsapp_notification import main as notify_whatsapp
        result = notify_whatsapp()
        log(f"WhatsApp notification concluído: {result}")
    except Exception as e:
        log(f"Erro na notificação WhatsApp: {e}")


def run_post_sync_pipeline():
    """Pipeline pós-sync: email → contactos → notificação WhatsApp.
    Cada peça é independente e regista o seu estado em worksheet própria."""
    run_pre_check_in()
    run_contacts_sync()
    run_whatsapp_notification()


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "--scheduled"

    if mode == "--email":
        log("Modo: verificação de email")
        from email_checker import has_new_booking_email, mark_booking_emails_read
        if has_new_booking_email():
            run_sync("email")
            mark_booking_emails_read()
        else:
            log("Sem emails novos do Yescapa.")
        # Em ambos os casos corremos a pipeline pós-sync, para que reservas
        # em backlog (por falha anterior) saiam.
        run_post_sync_pipeline()

    elif mode == "--scheduled":
        log("Modo: agendamento")
        run_sync("scheduled")
        run_post_sync_pipeline()

    elif mode == "--pre-check-in":
        log("Modo: enviar pré-check-in apenas (sem sync)")
        run_pre_check_in()

    elif mode == "--contacts":
        log("Modo: sync Google Contacts apenas")
        run_contacts_sync()

    elif mode == "--whatsapp":
        log("Modo: notificação WhatsApp apenas")
        run_whatsapp_notification()

    else:
        log(f"Argumento desconhecido: {mode}")
        log("Uso: python run.py --email | --scheduled | --pre-check-in | --contacts | --whatsapp")
        sys.exit(1)


if __name__ == "__main__":
    main()
