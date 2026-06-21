FROM python:3.12-slim

# Outils système requis par le moteur : mkvmerge (mkvtoolnix) et ffprobe (ffmpeg)
RUN apt-get update \
 && apt-get install -y --no-install-recommends mkvtoolnix ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY langmux.py webapp.py audio_sync.py /app/

# Rend la commande `langmux` disponible dans le shell du container
RUN install -m 0755 /app/langmux.py /usr/local/bin/langmux

# Racine autorisée pour la navigation depuis l'interface web
ENV LANGMUX_ROOT=/media
VOLUME ["/media"]
EXPOSE 8080

# Serveur WSGI mono-processus (le suivi des jobs est en mémoire)
CMD ["waitress-serve", "--listen=0.0.0.0:8080", "webapp:app"]
