FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Fontes base do sistema + fontconfig (pra libass resolver nomes).
# fonts-liberation cobre o Arial / Helvetica / Courier / Times quando o
# ASS pede esses nomes clássicos.
RUN apt-get update && apt-get install -y --no-install-recommends \
        fontconfig \
        fonts-liberation \
        fonts-dejavu-core \
        fonts-noto-core \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Google Fonts baixadas direto do repositório oficial github.com/google/fonts.
# Uso static TTFs (não variable fonts) porque libass trabalha melhor com eles.
RUN mkdir -p /usr/share/fonts/truetype/app && \
    cd /usr/share/fonts/truetype/app && \
    BASE="https://raw.githubusercontent.com/google/fonts/main" && \
    # Sans-serif principais
    curl -fsSL "$BASE/apache/roboto/static/Roboto-Regular.ttf"             -o Roboto-Regular.ttf             && \
    curl -fsSL "$BASE/apache/roboto/static/Roboto-Bold.ttf"                -o Roboto-Bold.ttf                && \
    curl -fsSL "$BASE/ofl/opensans/static/OpenSans-Regular.ttf"            -o OpenSans-Regular.ttf           && \
    curl -fsSL "$BASE/ofl/opensans/static/OpenSans-Bold.ttf"               -o OpenSans-Bold.ttf              && \
    curl -fsSL "$BASE/ofl/lato/Lato-Regular.ttf"                           -o Lato-Regular.ttf               && \
    curl -fsSL "$BASE/ofl/lato/Lato-Bold.ttf"                              -o Lato-Bold.ttf                  && \
    curl -fsSL "$BASE/ofl/montserrat/static/Montserrat-Regular.ttf"        -o Montserrat-Regular.ttf         && \
    curl -fsSL "$BASE/ofl/montserrat/static/Montserrat-Bold.ttf"           -o Montserrat-Bold.ttf            && \
    curl -fsSL "$BASE/ofl/montserrat/static/Montserrat-Black.ttf"          -o Montserrat-Black.ttf           && \
    curl -fsSL "$BASE/ofl/poppins/Poppins-Regular.ttf"                     -o Poppins-Regular.ttf            && \
    curl -fsSL "$BASE/ofl/poppins/Poppins-Bold.ttf"                        -o Poppins-Bold.ttf               && \
    curl -fsSL "$BASE/ofl/raleway/static/Raleway-Regular.ttf"              -o Raleway-Regular.ttf            && \
    curl -fsSL "$BASE/ofl/raleway/static/Raleway-Bold.ttf"                 -o Raleway-Bold.ttf               && \
    curl -fsSL "$BASE/ofl/nunito/static/Nunito-Regular.ttf"                -o Nunito-Regular.ttf             && \
    curl -fsSL "$BASE/ofl/nunito/static/Nunito-Bold.ttf"                   -o Nunito-Bold.ttf                && \
    curl -fsSL "$BASE/ofl/oswald/static/Oswald-Regular.ttf"                -o Oswald-Regular.ttf             && \
    curl -fsSL "$BASE/ofl/oswald/static/Oswald-Bold.ttf"                   -o Oswald-Bold.ttf                && \
    # Display / decorativas
    curl -fsSL "$BASE/ofl/playfairdisplay/static/PlayfairDisplay-Regular.ttf" -o PlayfairDisplay-Regular.ttf && \
    curl -fsSL "$BASE/ofl/playfairdisplay/static/PlayfairDisplay-Bold.ttf"    -o PlayfairDisplay-Bold.ttf    && \
    curl -fsSL "$BASE/ofl/bebasneue/BebasNeue-Regular.ttf"                 -o BebasNeue-Regular.ttf          && \
    curl -fsSL "$BASE/ofl/anton/Anton-Regular.ttf"                         -o Anton-Regular.ttf              && \
    curl -fsSL "$BASE/ofl/bangers/Bangers-Regular.ttf"                     -o Bangers-Regular.ttf            && \
    curl -fsSL "$BASE/ofl/permanentmarker/PermanentMarker-Regular.ttf"     -o PermanentMarker-Regular.ttf    && \
    curl -fsSL "$BASE/ofl/lobster/Lobster-Regular.ttf"                     -o Lobster-Regular.ttf            && \
    # Refresh fontconfig cache
    fc-cache -fv

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY start.py .
COPY templates/ templates/

EXPOSE 5000

CMD ["python", "start.py"]
