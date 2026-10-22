
import json
import urllib.request
import urllib.error

URL = "https://ttviewer.org/api/highlights/"

dados = {
    "username": "brunaapetrucci"
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

print("Testando consulta publica de Destaques...")

try:
    with urllib.request.urlopen(
        requisicao, timeout=20
    ) as resposta:
        print("HTTP:", resposta.status)

        resultado = json.load(resposta)

        if isinstance(resultado, dict):
            print("Campos recebidos:", list(resultado.keys()))

            colecoes = resultado.get("highlights")

            if isinstance(colecoes, list):
                print("Colecoes encontradas:", len(colecoes))
                for colecao in colecoes[:5]:
                    print(
                        "ID:", colecao.get("id"),
                        "Titulo:", colecao.get("title")
                    )
            else:
                print("Nenhuma lista de colecoes recebida.")

except urllib.error.HTTPError as erro:
    print("HTTP:", erro.code)
    print("A consulta nao foi aceita.")
    print("Nao tentaremos contornar controles de acesso.")

except urllib.error.URLError as erro:
    print("Falha de conexao:", erro.reason)
