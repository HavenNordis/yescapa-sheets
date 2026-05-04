"""
Ponto de entrada para automação cloud.
Chamado pelo Railway como cron job com um argumento:

  python run.py --email      → verifica email e corre sync se houver reserva nova
  python run.py --scheduled  → corre sync incondicionalmente (agendamento diário)
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
        main()
        log("Sync concluído com sucesso.")
    except Exception as e:
        log(f"Erro no sync: {e}")
        raise


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

    elif mode == "--scheduled":
        log("Modo: agendamento")
        run_sync("scheduled")

    else:
        log(f"Argumento desconhecido: {mode}")
        log("Uso: python run.py --email | --scheduled")
        sys.exit(1)


if __name__ == "__main__":
    main()
