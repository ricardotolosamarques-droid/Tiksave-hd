
import json
import urllib.error
import urllib.request

URL = "https://ttviewer.org/api/highlights/"

print("TESTE DE CONECTIVIDADE TTViewer")
print("Verificando acesso HTTPS ao endpoint...")

request = urllib.request.Request(
    URL,
    method="OPTIONS",
    headers={
        "User-Agent": "TikSave-HD-Connectivity-Test/1.0"
    }
)

try:
    with urllib.request.urlopen(request, timeout=15) as response:
        print("HTTP:", response.status)
        print("Allow:", response.headers.get("Allow"))
        print("Content-Type:", response.headers.get("Content-Type"))
        print("Conectividade HTTPS confirmada.")

except urllib.error.HTTPError as error:
    print("HTTP:", error.code)
    print("O servidor respondeu, mas recusou este metodo.")
    print("Isso nao confirma permissao para integrar a API.")

except urllib.error.URLError as error:
    print("Falha de conexao:", str(error.reason))
    raise SystemExit(1)
