FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Fuentes base del sistema (siempre presentes en cualquier Debian).
# fonts-liberation provee Liberation Sans/Mono/Serif — sustitutos
# métricamente idénticos de Arial, Helvetica, Courier, Times. Libass los usa.
# nodejs es necesario para que yt-dlp resuelva el n-signature JS challenge
# de YouTube; yt-dlp auto-detecta `node` en el PATH. Sin esto, /download
# falla con "n challenge solving failed: Some formats may be missing".
RUN apt-get update && apt-get install -y --no-install-recommends \
        fontconfig \
        fonts-liberation \
        fonts-dejavu-core \
        fonts-noto-core \
        curl \
        nodejs \
    && rm -rf /var/lib/apt/lists/*

# Google Fonts descargadas desde github.com/google/fonts.
# Cada curl corre aislado: si uno falla, los demás continúan.
# Las URLs de fuentes variables usan %5B/%5D (encoding de [ y ]).
RUN mkdir -p /usr/share/fonts/truetype/app && \
    cd /usr/share/fonts/truetype/app && \
    BASE="https://raw.githubusercontent.com/google/fonts/main" && \
    echo "=== Descargando Google Fonts (los fallos se loguean, no matan el build) ===" && \
    ( \
      curl -fsSL "$BASE/ofl/montserrat/Montserrat%5Bwght%5D.ttf"            -o Montserrat.ttf           || echo "[WARN] Montserrat"; \
      curl -fsSL "$BASE/ofl/roboto/Roboto%5Bwdth,wght%5D.ttf"               -o Roboto.ttf               || echo "[WARN] Roboto"; \
      curl -fsSL "$BASE/ofl/oswald/Oswald%5Bwght%5D.ttf"                    -o Oswald.ttf               || echo "[WARN] Oswald"; \
      curl -fsSL "$BASE/ofl/raleway/Raleway%5Bwght%5D.ttf"                  -o Raleway.ttf              || echo "[WARN] Raleway"; \
      curl -fsSL "$BASE/ofl/nunito/Nunito%5Bwght%5D.ttf"                    -o Nunito.ttf               || echo "[WARN] Nunito"; \
      curl -fsSL "$BASE/ofl/playfairdisplay/PlayfairDisplay%5Bwght%5D.ttf"  -o PlayfairDisplay.ttf      || echo "[WARN] PlayfairDisplay"; \
      curl -fsSL "$BASE/ofl/opensans/OpenSans%5Bwdth,wght%5D.ttf"           -o OpenSans.ttf             || echo "[WARN] OpenSans"; \
      curl -fsSL "$BASE/ofl/poppins/Poppins-Regular.ttf"                    -o Poppins-Regular.ttf      || echo "[WARN] Poppins-Regular"; \
      curl -fsSL "$BASE/ofl/poppins/Poppins-Bold.ttf"                       -o Poppins-Bold.ttf         || echo "[WARN] Poppins-Bold"; \
      curl -fsSL "$BASE/ofl/poppins/Poppins-Black.ttf"                      -o Poppins-Black.ttf        || echo "[WARN] Poppins-Black"; \
      curl -fsSL "$BASE/ofl/lato/Lato-Regular.ttf"                          -o Lato-Regular.ttf         || echo "[WARN] Lato-Regular"; \
      curl -fsSL "$BASE/ofl/lato/Lato-Bold.ttf"                             -o Lato-Bold.ttf            || echo "[WARN] Lato-Bold"; \
      curl -fsSL "$BASE/ofl/bebasneue/BebasNeue-Regular.ttf"                -o BebasNeue-Regular.ttf    || echo "[WARN] BebasNeue"; \
      curl -fsSL "$BASE/ofl/anton/Anton-Regular.ttf"                        -o Anton-Regular.ttf        || echo "[WARN] Anton"; \
      curl -fsSL "$BASE/ofl/bangers/Bangers-Regular.ttf"                    -o Bangers-Regular.ttf      || echo "[WARN] Bangers"; \
      curl -fsSL "$BASE/apache/permanentmarker/PermanentMarker-Regular.ttf" -o PermanentMarker-Regular.ttf || echo "[WARN] PermanentMarker"; \
      curl -fsSL "$BASE/ofl/lobster/Lobster-Regular.ttf"                    -o Lobster-Regular.ttf      || echo "[WARN] Lobster"; \
    ) && \
    echo "=== Fuentes instaladas: ===" && \
    ls -la /usr/share/fonts/truetype/app/ && \
    fc-cache -fv

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY start.py .
COPY templates/ templates/

EXPOSE 5000

CMD ["python", "start.py"]
