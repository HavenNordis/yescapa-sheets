# Imagem oficial do Playwright com Python — já inclui Chromium e todas as dependências
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

# Instalar dependências Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar código
COPY . .

# Em cloud, correr sempre em modo headless
ENV HEADLESS=true
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Comando padrão (substituído pelo cron job no Railway)
CMD ["python", "run.py", "--scheduled"]
