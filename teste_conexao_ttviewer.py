
import json
import urllib.request
import urllib.error
from urllib.parse import urlparse

API = "https://ttviewer.org/api/highlights/"

dados = {
    "action": "posts",
    "highlightId": "793616302084",
    "cursor": "0"
}

def testar_midia(nome, url):
    if not isinstance(url, str) or not url:
        print(nome, ": URL ausente")
        return

    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        print(nome, ": URL invalida")
        return

    # Testa somente a resposta, sem baixar o arquivo.
    try:
        req = urllib.request.Request(
            url,
            method="HEAD",
            headers={"User-Agent": "TikSave-HD-Test/1.0"}
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            print(nome, "HTTP:", r.status)
            print(nome, "Tipo:", r.headers.get("Content-Type"))
    except urllib.error.HTTPError as e:
        print(nome, "HTTP:", e.code)
    except urllib.error.URLError as e:
        print(nome, "Erro de conexao:", e.reason)

req = urllib.request.Request(
    API,
    data=json.dumps(dados).encode("utf-8"),
    method="POST",
    headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "TikSave-HD-Test/1.0"
    }
)

try:
    with urllib.request.urlopen(req, timeout=20) as r:
        resultado = json.load(r)

    itens = resultado.get("items", [])
    print("Itens encontrados:", len(itens))

    if itens:
        primeiro = itens[0]
        testar_midia("Video", primeiro.get("videoUrl"))
        testar_midia("Capa", primeiro.get("cover"))

except urllib.error.HTTPError as e:
    print("API HTTP:", e.code)
except urllib.error.URLError as e:
    print("API erro:", e.reason)
