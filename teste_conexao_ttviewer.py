
import urllib.request
import urllib.error

URL = "https://ttviewer.org/api/highlights/"

print("Testando conectividade com TTViewer...")

request = urllib.request.Request(
    URL,
    method="OPTIONS",
    headers={"User-Agent": "TikSave-HD-Test/1.0"}
)

try:
    with urllib.request.urlopen(request, timeout=15) as response:
        print("HTTP:", response.status)
        print("Servidor acessivel.")

except urllib.error.HTTPError as error:
    print("HTTP:", error.code)
    print("Servidor respondeu, mas recusou o metodo.")

except urllib.error.URLError as error:
    print("Erro de conexao:", error.reason)
    raise SystemExit(1)
