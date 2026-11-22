"""Sube a la rama datos-base los últimos paneles diarios que GitHub no tiene (GitHub recibe 403 del
portal de SEPA, así que depende de lo que procesa la PC). Lo llama control.ps1 -Subir."""
import base64, subprocess, sys
from pathlib import Path
import requests

REPO, RAMA = "tadeobessega/ipceer", "datos-base"
tok = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True).stdout.strip()
if not tok:
    sys.exit("gh no está conectado")
H = {"Authorization": f"token {tok}", "Accept": "application/vnd.github+json"}
for f in sorted(Path("data/panel/diario").glob("*.parquet"))[-5:]:
    url = f"https://api.github.com/repos/{REPO}/contents/panel/diario/{f.name}"
    if requests.get(url, headers=H, params={"ref": RAMA}, timeout=60).status_code == 200:
        continue
    r = requests.put(url, headers=H, timeout=300, json={"message": f"Panel diario {f.stem} de la PC", "branch": RAMA,
                     "content": base64.b64encode(f.read_bytes()).decode()})
    print(f.name, r.status_code)
