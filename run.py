"""
Ponto de entrada para automação cloud.

Chamado pelo Railway como cron job com um argumento:

    python run.py --email          → verifica email e corre sync se houver reserva nova,
                                     depois tenta enviar emails de pré-check-in pendentes.
    python run.py --scheduled      → corre sync incondicionalmente (agendamento diário),
                                     depois tenta enviar emails de pré-check-in pendentes.
    python run.py --pre-check-in   → corre APENAS o envio de emails pré-check-in
                                     (sem tocar no sync; útil para testes manuais).
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
        # NÃO relançar — falha no envio não deve abortar o cron job.


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
        # Em ambos os casos (com ou sem sync) tentamos enviar pré-check-in pendentes,
        # para garantir que reservas que ficaram em backlog (por falha anterior) saem.
        run_pre_check_in()

    elif mode == "--scheduled":
        log("Modo: agendamento")
        run_sync("scheduled")
        run_pre_check_in()

    elif mode == "--pre-check-in":
        log("Modo: enviar pré-check-in apenas (sem sync)")
        run_pre_check_in()

    else:
        log(f"Argumento desconhecido: {mode}")
        log("Uso: python run.py --email | --scheduled | --pre-check-in")
        sys.exit(1)


if __name__ == "__main__":
    main()
