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
    """Corre o sync Yescapa.

    Falhas NÃO interrompem o cron — desde 2026-05-16 a Yescapa bloqueia o
    Railway (anti-bot/IP de datacenter), pelo que o sync pode rebentar com
    HTTP 403. Quando isso acontece, ainda assim queremos correr o
    pre_check_in_sender (que processa reservas inseridas manualmente).
    """
    log(f"A iniciar sync (trigger: {trigger})...")
    try:
        from yescapa_sheets import main
        main(trigger)
        log("Sync concluído com sucesso.")
    except SystemExit as e:
        log(f"Sync abortado (Yescapa bloqueado?): {e}")
        # NÃO relançar — continua para pre_check_in + downloader
    except Exception as e:
        log(f"Erro no sync: {e}")
        # NÃO relançar — falha no sync não deve abortar o resto do cron


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


def run_docs_downloader():
    """Descarrega PDFs Yescapa (Contrato/Seguro/Fatura) para Drive.

    Idempotente: salta documentos já baixados. Falhas não interrompem o cron.
    Salta totalmente se DRIVE_DOCS_FOLDER_ID não estiver definida (config
    incompleta — ex: ainda não criámos a pasta Drive partilhada com a SA).
    """
    log("A iniciar download de documentos Yescapa...")
    try:
        from yescapa_docs_downloader import main as download_docs
        result = download_docs()
        log(f"Downloader concluído: {result}")
    except Exception as e:
        log(f"Erro no downloader: {e}")
        # NÃO relançar.


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
        run_docs_downloader()

    elif mode == "--scheduled":
        log("Modo: agendamento")
        run_sync("scheduled")
        run_pre_check_in()
        run_docs_downloader()

    elif mode == "--pre-check-in":
        log("Modo: enviar pré-check-in apenas (sem sync)")
        run_pre_check_in()

    elif mode == "--docs":
        log("Modo: downloader de documentos apenas")
        run_docs_downloader()

    else:
        log(f"Argumento desconhecido: {mode}")
        log("Uso: python run.py --email | --scheduled | --pre-check-in")
        sys.exit(1)


if __name__ == "__main__":
    main()
