
import json
import urllib.request
import urllib.error

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

print("Testando itens de um Destaque publico...")

try:
    with urllib.request.urlopen(requisicao, timeout=20) as resposta:
        print("HTTP:", resposta.status)
        resultado = json.load(resposta)

        if isinstance(resultado, dict):
            print("Campos:", list(resultado.keys()))

            itens = resultado.get("items", [])
            print("Itens encontrados:", len(itens))

            for item in itens[:5]:
                print(
                    "ID:", item.get("id"),
                    "Video:", bool(item.get("videoUrl")),
                    "Fotos:", len(item.get("images") or [])
                )

            print("Tem mais paginas:", resultado.get("hasMore"))

except urllib.error.HTTPError as erro:
    print("HTTP:", erro.code)
    print("Consulta recusada pelo servidor.")

except urllib.error.URLError as erro:
    print("Erro de conexao:", erro.reason)
