"""
Ponto de entrada para automação cloud.

Chamado pelo Railway como cron job com um argumento:

    python run.py --email          → verifica email e corre sync se houver reserva nova,
                                     depois pré-check-in + arquivo de docs + checklists.
    python run.py --scheduled      → corre sync incondicionalmente (agendamento),
                                     depois pré-check-in + arquivo de docs + checklists.
    python run.py --pre-check-in   → corre APENAS o envio de emails pré-check-in.
    python run.py --drive-archive  → corre APENAS o arquivo de documentos no Drive.
    python run.py --checklist      → corre APENAS a geração de checklists do Tally.
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


def run_drive_archive():
    """Arquiva no Google Drive os documentos das reservas (contrato, fatura).

    Falhas aqui não interrompem o cron — os erros ficam registados na folha
    Documentos para revisão manual.
    """
    log("A iniciar arquivo de documentos no Drive...")
    try:
        from drive_archiver import main as archive_docs
        result = archive_docs()
        log(f"Arquivo de documentos concluído: {result}")
    except Exception as e:
        log(f"Erro no arquivo de documentos: {e}")
        # NÃO relançar — falha no arquivo não deve abortar o cron job.


def run_checklist():
    """Gera as checklists de preparação a partir das respostas do Tally.

    Falhas aqui não interrompem o cron — os erros ficam registados na folha
    Checklists para revisão manual.
    """
    log("A iniciar geração de checklists...")
    try:
        from checklist_runner import main as gerar_checklists
        result = gerar_checklists()
        log(f"Checklists concluído: {result}")
    except Exception as e:
        log(f"Erro na geração de checklists: {e}")
        # NÃO relançar — falha aqui não deve abortar o cron job.


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "--scheduled"

    if mode == "--email":
        log("Modo: cron + verificação de email")
        from email_checker import has_new_booking_email, mark_booking_emails_read
        new_email = has_new_booking_email()
        if new_email:
            log("Email novo de reserva detectado.")
        else:
            log("Sem emails novos do Yescapa — sync na mesma (cron).")
        # Sync sempre — mantém a folha em dia com o Yescapa em cada run.
        run_sync("email" if new_email else "scheduled")
        if new_email:
            mark_booking_emails_read()
        run_pre_check_in()
        run_drive_archive()
        run_checklist()

    elif mode == "--scheduled":
        log("Modo: agendamento")
        run_sync("scheduled")
        run_pre_check_in()
        run_drive_archive()
        run_checklist()

    elif mode == "--pre-check-in":
        log("Modo: enviar pré-check-in apenas (sem sync)")
        run_pre_check_in()

    elif mode == "--drive-archive":
        log("Modo: arquivo de documentos apenas (sem sync)")
        run_drive_archive()

    elif mode == "--checklist":
        log("Modo: geração de checklists apenas (sem sync)")
        run_checklist()

    else:
        log(f"Argumento desconhecido: {mode}")
        log("Uso: python run.py --email | --scheduled | --pre-check-in | --drive-archive | --checklist")
        sys.exit(1)


if __name__ == "__main__":
    main()
