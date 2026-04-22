FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Fontes do sistema que o libass usa pra renderizar legendas ASS.
# fonts-liberation cobre o Arial / Helvetica / Courier / Times (substitutos
# equivalentes). O resto cobre fontes adicionais que o app expõe no picker.
RUN apt-get update && apt-get install -y --no-install-recommends \
        fontconfig \
        fonts-liberation \
        fonts-liberation2 \
        fonts-dejavu \
        fonts-dejavu-core \
        fonts-noto-core \
        fonts-roboto \
        fonts-open-sans \
        fonts-lato \
        fonts-lobster \
        fonts-oswald \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Google Fonts que não estão empacotadas no apt — baixo os TTFs estáticos
# direto do repositório oficial github.com/google/fonts e dropo em
# /usr/share/fonts/truetype/app. Depois atualizo o cache do fontconfig.
RUN mkdir -p /usr/share/fonts/truetype/app && \
    cd /usr/share/fonts/truetype/app && \
    BASE="https://raw.githubusercontent.com/google/fonts/main" && \
    curl -fsSL "$BASE/ofl/montserrat/static/Montserrat-Regular.ttf"  -o Montserrat-Regular.ttf  && \
    curl -fsSL "$BASE/ofl/montserrat/static/Montserrat-Bold.ttf"     -o Montserrat-Bold.ttf     && \
    curl -fsSL "$BASE/ofl/montserrat/static/Montserrat-Black.ttf"    -o Montserrat-Black.ttf    && \
    curl -fsSL "$BASE/ofl/poppins/Poppins-Regular.ttf"               -o Poppins-Regular.ttf     && \
    curl -fsSL "$BASE/ofl/poppins/Poppins-Bold.ttf"                  -o Poppins-Bold.ttf        && \
    curl -fsSL "$BASE/ofl/raleway/static/Raleway-Regular.ttf"        -o Raleway-Regular.ttf     && \
    curl -fsSL "$BASE/ofl/raleway/static/Raleway-Bold.ttf"           -o Raleway-Bold.ttf        && \
    curl -fsSL "$BASE/ofl/nunito/static/Nunito-Regular.ttf"          -o Nunito-Regular.ttf      && \
    curl -fsSL "$BASE/ofl/nunito/static/Nunito-Bold.ttf"             -o Nunito-Bold.ttf         && \
    curl -fsSL "$BASE/ofl/playfairdisplay/static/PlayfairDisplay-Regular.ttf" -o PlayfairDisplay-Regular.ttf && \
    curl -fsSL "$BASE/ofl/playfairdisplay/static/PlayfairDisplay-Bold.ttf"    -o PlayfairDisplay-Bold.ttf    && \
    curl -fsSL "$BASE/ofl/bebasneue/BebasNeue-Regular.ttf"           -o BebasNeue-Regular.ttf   && \
    curl -fsSL "$BASE/ofl/anton/Anton-Regular.ttf"                   -o Anton-Regular.ttf       && \
    curl -fsSL "$BASE/ofl/bangers/Bangers-Regular.ttf"               -o Bangers-Regular.ttf     && \
    curl -fsSL "$BASE/ofl/permanentmarker/PermanentMarker-Regular.ttf" -o PermanentMarker-Regular.ttf && \
    fc-cache -fv

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY start.py .
COPY templates/ templates/

EXPOSE 5000

CMD ["python", "start.py"]
