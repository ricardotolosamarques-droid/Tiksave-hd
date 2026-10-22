
import json
import urllib.request
import urllib.error
from urllib.parse import urlparse

URL = "https://ttviewer.org/api/highlights/"

dados = {
    "action": "posts",
    "highlightId": "793616302084",
    "cursor": "0"
}

requisicao = urllib.request.Request(
    URL,
    data=json.dumps(dados).encode("utf-8"),
    headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "TikSave-HD-Test/1.0"
    },
    method="POST"
)

print("TESTE DE LINKS DOS DESTAQUES")

try:
    with urllib.request.urlopen(requisicao, timeout=20) as resposta:
        print("HTTP API:", resposta.status)
        resultado = json.load(resposta)

    itens = resultado.get("items", [])
    print("Itens encontrados:", len(itens))

    for item in itens[:3]:
        video = item.get("videoUrl")
        capa = item.get("cover")

        print("ID:", item.get("id"))

        for nome, link in [("Video", video), ("Capa", capa)]:
            if not isinstance(link, str) or not link:
                print(nome + ": ausente")
                continue

            parsed = urlparse(link)

            if parsed.scheme != "https" or not parsed.hostname:
                print(nome + ": URL invalida")
                continue

            print(nome + " HTTPS: OK")
            print(nome + " dominio:", parsed.hostname)

    print("Tem mais paginas:", resultado.get("hasMore"))

except urllib.error.HTTPError as erro:
    print("HTTP API:", erro.code)

except urllib.error.URLError as erro:
    print("Erro:", erro.reason)

except (ValueError, TypeError) as erro:
    print("Resposta invalida:", erro)
