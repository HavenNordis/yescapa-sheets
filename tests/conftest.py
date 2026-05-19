"""Configuracao partilhada dos testes - fixtures e env vars dummy."""
import os
import sys
from pathlib import Path

# Garante que o repo root esta no path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Define env vars dummy para os modulos importarem sem rebentar.
# Os testes que precisarem de credenciais reais terao de fazer mock.
os.environ.setdefault("YESCAPA_EMAIL", "test@test.com")
os.environ.setdefault("YESCAPA_PASSWORD", "test")
os.environ.setdefault("GOOGLE_CREDENTIALS_JSON", '{"type":"service_account","project_id":"x","private_key_id":"x","private_key":"x","client_email":"x@x.com","client_id":"x","auth_uri":"x","token_uri":"x","auth_provider_x509_cert_url":"x","client_x509_cert_url":"x"}')
os.environ.setdefault("GMAIL_CLIENT_ID", "x")
os.environ.setdefault("GMAIL_CLIENT_SECRET", "x")
os.environ.setdefault("GMAIL_REFRESH_TOKEN", "x")
