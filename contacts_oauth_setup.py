"""
contacts_oauth_setup.py — utilitário para gerar o refresh_token da People API
(Google Contacts) uma vez.

CORRER UMA ÚNICA VEZ NA MÁQUINA LOCAL, com browser disponível.
Gera um refresh_token a guardar em env var no Railway como CONTACTS_REFRESH_TOKEN.

Pré-requisitos:
  1. No Google Cloud Console:
     - Ativar a API "People API" no mesmo projeto do Gmail/Sheets.
     - Adicionar o scope https://www.googleapis.com/auth/contacts ao consent screen
       (Edit App → Scopes → Add or remove scopes).
     - Garantir que ops@havennordis.com está em Test Users (ou em Production).
  2. Usar o MESMO credentials.json do Gmail (OAuth 2.0 Client tipo "Desktop app").
     Reutiliza o ficheiro que já tens na pasta para o gmail_oauth_setup.py.

Uso:
    python contacts_oauth_setup.py

O script abre o browser, pede para fazer login com ops@havennordis.com, e
imprime no terminal o CONTACTS_REFRESH_TOKEN a copiar para o .env / Railway.
"""

from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/contacts"]
CLIENT_SECRETS = Path(__file__).parent / "credentials.json"


def main():
    if not CLIENT_SECRETS.exists():
        raise SystemExit(
            f"Falta o ficheiro {CLIENT_SECRETS}. É o mesmo do gmail_oauth_setup.py — "
            "descarrega-o do Google Cloud Console (OAuth 2.0 Client → Download JSON) "
            "e renomeia para 'credentials.json'."
        )

    flow = InstalledAppFlow.from_client_secrets_file(
        str(CLIENT_SECRETS), SCOPES
    )
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")

    print("\n" + "=" * 60)
    print("OAuth Contacts concluído. Copia estes valores para o .env e Railway:")
    print("=" * 60)
    print(f"CONTACTS_CLIENT_ID={creds.client_id}")
    print(f"CONTACTS_CLIENT_SECRET={creds.client_secret}")
    print(f"CONTACTS_REFRESH_TOKEN={creds.refresh_token}")
    print("=" * 60)
    print("\nNOTA: o refresh_token NÃO expira (a menos que seja revogado em "
          "account.google.com → Segurança → Acessos de terceiros).")
    print()
    print("Próximo passo: criar grupo 'Hóspedes Yescapa' em contacts.google.com")
    print("(logada em ops@havennordis.com), copiar o ID do grupo do URL")
    print("(formato: 'contactGroups/abc123...') e pôr na env var")
    print("CONTACTS_GROUP_RESOURCE_NAME do Railway.")


if __name__ == "__main__":
    main()
