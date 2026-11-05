"""Baixador: baixa uma lista de links (um por vez), descompacta e gera link temporario."""

import json
import logging
import os
import queue
import re
import secrets
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
from functools import wraps
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import requests
from flask import (Flask, Response, abort, jsonify, render_template, request,
                   send_from_directory, stream_with_context)

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
JOBS_DIR = DATA_DIR / "jobs"
STATE_FILE = DATA_DIR / "state.json"
APP_USER = os.environ.get("APP_USER", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
LINK_TTL_HOURS = float(os.environ.get("LINK_TTL_HOURS", "24"))
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
TENTATIVAS = int(os.environ.get("TENTATIVAS", "5"))
CHUNK = 1024 * 1024
UA = "Mozilla/5.0 (X11; Linux x86_64) Baixador/1.0"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("baixador")

app = Flask(__name__)
lock = threading.RLock()
jobs: dict = {}
fila: "queue.Queue[str]" = queue.Queue()


class Cancelado(Exception):
    pass


class LinkInvalido(Exception):
    pass


# --------------------------------------------------------------------------- estado

def salvar():
    with lock:
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(jobs, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(STATE_FILE)


def atualizar(job, **campos):
    with lock:
        job.update(campos)
        salvar()


def carregar():
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    if STATE_FILE.exists():
        try:
            jobs.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
        except Exception:
            log.exception("Nao foi possivel ler %s", STATE_FILE)
    for job in sorted(jobs.values(), key=lambda j: j["criado"]):
        job["cancelar"] = False
        if job["status"] in ("fila", "baixando", "extraindo"):
            job["status"] = "fila"
            job["mensagem"] = "Retomando após reinício"
            for item in job["itens"]:
                if item["status"] == "baixando":
                    item["status"] = "pendente"
            fila.put(job["id"])
    salvar()


# --------------------------------------------------------------------------- download

def nome_do_arquivo(resp, url_original):
    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r"filename\*\s*=\s*[^']*''([^;]+)", cd, re.I)
    if m:
        nome = unquote(m.group(1).strip().strip('"'))
    else:
        m = re.search(r'filename\s*=\s*"?([^";]+)"?', cd, re.I)
        nome = m.group(1).strip() if m else ""
    if not nome:
        candidatos = [unquote(Path(urlparse(u).path).name) for u in (url_original, resp.url)]
        candidatos = [c for c in candidatos if c]
        com_ext = [c for c in candidatos if "." in c]
        nome = (com_ext or candidatos or [""])[0]
    nome = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", nome).strip(" .")
    return nome or "arquivo"


def nome_unico(job, item, nome):
    usados = {i["nome"] for i in job["itens"] if i is not item and i["nome"]}
    if nome not in usados:
        return nome
    n = 2
    base, ext = os.path.splitext(nome)
    while f"{base}_{n}{ext}" in usados:
        n += 1
    return f"{base}_{n}{ext}"


def _baixar_uma_vez(job, item, destino: Path):
    item["status"] = "baixando"
    inicio = 0
    final = parcial = None
    if item["nome"]:
        final = destino / item["nome"]
        parcial = destino / (item["nome"] + ".incompleto")
        if final.exists():
            return
        if parcial.exists():
            inicio = parcial.stat().st_size

    headers = {"User-Agent": UA}
    if inicio:
        headers["Range"] = f"bytes={inicio}-"

    with requests.get(item["url"], headers=headers, stream=True,
                      timeout=(20, 120), allow_redirects=True) as r:
        if r.status_code == 416 and inicio:  # ja estava completo
            parcial.replace(final)
            return
        if r.status_code in (401, 403, 404, 410):
            raise LinkInvalido(f"HTTP {r.status_code} - link inválido, expirado ou sem permissão")
        r.raise_for_status()
        if "text/html" in r.headers.get("Content-Type", "").lower():
            raise LinkInvalido("O link abriu uma página (HTML), não um arquivo. Use um link direto.")

        if not item["nome"]:
            item["nome"] = nome_unico(job, item, nome_do_arquivo(r, item["url"]))
            final = destino / item["nome"]
            parcial = destino / (item["nome"] + ".incompleto")

        if inicio and r.status_code != 206:  # servidor nao aceita retomar
            inicio = 0
        tamanho = r.headers.get("Content-Length", "")
        item["total"] = inicio + int(tamanho) if tamanho.isdigit() else None
        item["baixado"] = inicio
        salvar()

        with open(parcial, "ab" if inicio else "wb") as f:
            for bloco in r.iter_content(CHUNK):
                if job["cancelar"]:
                    raise Cancelado()
                if bloco:
                    f.write(bloco)
                    item["baixado"] += len(bloco)

    if item["total"] and item["baixado"] < item["total"]:
        raise IOError("conexão caiu antes do fim")
    item["total"] = item["baixado"]
    parcial.replace(final)


def baixar(job, item, destino: Path):
    ultimo = None
    for tentativa in range(1, TENTATIVAS + 1):
        if job["cancelar"]:
            raise Cancelado()
        try:
            _baixar_uma_vez(job, item, destino)
            item["status"], item["erro"] = "ok", None
            salvar()
            return
        except Cancelado:
            raise
        except Exception as e:  # noqa: BLE001
            ultimo = str(e)
            log.warning("Falha em %s (tentativa %s): %s", item["url"], tentativa, e)
            if isinstance(e, LinkInvalido):
                break
            item["erro"] = f"tentativa {tentativa}/{TENTATIVAS} falhou ({e}), tentando de novo..."
            for _ in range(10 * tentativa):
                if job["cancelar"]:
                    raise Cancelado()
                time.sleep(1)
    item["status"], item["erro"] = "erro", ultimo
    salvar()


# --------------------------------------------------------------------------- descompactar

RE_PART_RAR = re.compile(r"\.part0*(\d+)\.rar$", re.I)
RE_SPLIT = re.compile(r"\.(\d{3})$")
RE_VOLUME_EXTRA = re.compile(r"\.(r\d{2}|z\d{2})$", re.I)
EXT_TAR = (".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz", ".tar")
EXT_7Z = (".zip", ".7z", ".gz", ".bz2", ".xz")


def classificar(nome: str):
    """Retorna rar | 7z | tar | parte (volume que nao e o primeiro) | None (nao compactado)."""
    n = nome.lower()
    m = RE_PART_RAR.search(n)
    if m:
        return "rar" if int(m.group(1)) == 1 else "parte"
    m = RE_SPLIT.search(n)
    if m:
        return "7z" if int(m.group(1)) == 1 else "parte"
    if RE_VOLUME_EXTRA.search(n):
        return "parte"
    if n.endswith(EXT_TAR):
        return "tar"
    if n.endswith(".rar"):
        return "rar"
    if n.endswith(EXT_7Z):
        return "7z"
    return None


def extrair(arquivo: Path, saida: Path, senha: str):
    tipo = classificar(arquivo.name)
    if tipo == "rar":
        cmd = ["unar", "-f", "-q", "-o", str(saida)] + (["-p", senha] if senha else []) + [str(arquivo)]
    elif tipo == "tar":
        cmd = ["tar", "-xf", str(arquivo), "-C", str(saida)]
    else:
        cmd = ["7z", "x", "-y", f"-o{saida}", f"-p{senha or ''}", str(arquivo)]
    r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if r.returncode != 0:
        linhas = [l for l in (r.stderr + "\n" + r.stdout).splitlines() if l.strip()]
        detalhe = " / ".join(linhas[-3:]) if linhas else f"código {r.returncode}"
        if "password" in detalhe.lower() or "senha" in detalhe.lower():
            detalhe = "senha incorreta ou ausente - " + detalhe
        raise RuntimeError(detalhe)


def extrair_tudo(job, download: Path, saida: Path):
    erros = []
    for arq in sorted(p for p in download.iterdir() if p.is_file()):
        if arq.name.endswith(".incompleto"):
            continue
        tipo = classificar(arq.name)
        if tipo is None:  # arquivo comum: vai direto para a saida
            shutil.move(str(arq), str(saida / arq.name))
            continue
        if tipo == "parte":
            continue
        atualizar(job, mensagem=f"Descompactando {arq.name}...")
        try:
            extrair(arq, saida, job["senha"])
        except Exception as e:  # noqa: BLE001
            erros.append(f"{arq.name}: {e}")
    if erros:
        raise RuntimeError("Falha ao descompactar: " + " | ".join(erros))


# --------------------------------------------------------------------------- fila

def tamanho_pasta(p: Path):
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def processar(job):
    pasta = JOBS_DIR / job["id"]
    download, saida = pasta / "download", pasta / "arquivos"
    download.mkdir(parents=True, exist_ok=True)
    saida.mkdir(parents=True, exist_ok=True)

    atualizar(job, status="baixando", mensagem="")
    for item in job["itens"]:
        if item["status"] != "ok":
            baixar(job, item, download)

    falhas = [i for i in job["itens"] if i["status"] != "ok"]
    if falhas:
        raise RuntimeError(f"{len(falhas)} link(s) falharam. Veja os erros abaixo e use 'Tentar de novo'.")

    atualizar(job, status="extraindo", mensagem="Descompactando...")
    extrair_tudo(job, download, saida)
    if job["apagar_partes"]:
        shutil.rmtree(download, ignore_errors=True)

    agora = time.time()
    atualizar(job, status="pronto", mensagem="", concluido=agora,
              expira=agora + LINK_TTL_HOURS * 3600,
              token=job.get("token") or secrets.token_urlsafe(18),
              tamanho_final=tamanho_pasta(saida))


def worker():
    while True:
        jid = fila.get()
        job = jobs.get(jid)
        if not job or job["status"] != "fila":
            continue
        try:
            processar(job)
        except Cancelado:
            atualizar(job, status="cancelado", mensagem="Cancelado")
        except Exception as e:  # noqa: BLE001
            log.exception("Erro no download %s", jid)
            atualizar(job, status="erro", mensagem=str(e))
        if job.get("excluir"):
            remover(job)


def remover(job):
    shutil.rmtree(JOBS_DIR / job["id"], ignore_errors=True)
    with lock:
        jobs.pop(job["id"], None)
        salvar()


def limpeza():
    while True:
        time.sleep(300)
        agora = time.time()
        for job in list(jobs.values()):
            if job["status"] == "pronto" and job.get("expira") and agora > job["expira"]:
                shutil.rmtree(JOBS_DIR / job["id"], ignore_errors=True)
                atualizar(job, status="expirado", token=None,
                          mensagem="O link expirou e os arquivos foram apagados.")


# --------------------------------------------------------------------------- web: painel

def protegido(f):
    @wraps(f)
    def wrapper(*a, **k):
        auth = request.authorization
        ok = auth and secrets.compare_digest((auth.username or "").encode(), APP_USER.encode()) \
            and secrets.compare_digest((auth.password or "").encode(), APP_PASSWORD.encode())
        if not ok:
            return Response("Login necessário", 401, {"WWW-Authenticate": 'Basic realm="Baixador"'})
        return f(*a, **k)
    return wrapper


def base_url():
    return PUBLIC_URL or request.host_url.rstrip("/")


@app.get("/")
@protegido
def painel():
    return render_template("index.html", ttl=LINK_TTL_HOURS)


@app.get("/api/jobs")
@protegido
def api_jobs():
    with lock:
        lista = []
        for j in sorted(jobs.values(), key=lambda j: j["criado"], reverse=True):
            d = {k: v for k, v in j.items() if k not in ("senha", "token")}
            d["link"] = f"{base_url()}/s/{j['token']}" if j.get("token") and j["status"] == "pronto" else None
            lista.append(d)
    return jsonify(jobs=lista, livre=shutil.disk_usage(DATA_DIR).free)


@app.post("/jobs")
@protegido
def criar_job():
    dados = request.get_json(force=True)
    links = [l.strip() for l in (dados.get("links") or "").splitlines()]
    links = [l for l in links if l and not l.startswith("#")]
    invalidos = [l for l in links if not l.lower().startswith(("http://", "https://"))]
    if not links:
        return jsonify(erro="Cole pelo menos um link."), 400
    if invalidos:
        return jsonify(erro=f"Linha inválida (precisa começar com http): {invalidos[0][:80]}"), 400
    job = {
        "id": uuid.uuid4().hex[:10],
        "nome": (dados.get("nome") or "").strip() or f"Download {time.strftime('%d/%m %H:%M')}",
        "senha": dados.get("senha") or "",
        "apagar_partes": bool(dados.get("apagar_partes", True)),
        "status": "fila", "mensagem": "", "criado": time.time(),
        "concluido": None, "expira": None, "token": None, "cancelar": False,
        "tamanho_final": None,
        "itens": [{"url": u, "nome": None, "status": "pendente", "baixado": 0,
                   "total": None, "erro": None} for u in links],
    }
    with lock:
        jobs[job["id"]] = job
        salvar()
    fila.put(job["id"])
    return jsonify(ok=True, id=job["id"])


def _job(jid):
    job = jobs.get(jid)
    if not job:
        abort(404)
    return job


@app.post("/jobs/<jid>/cancelar")
@protegido
def cancelar(jid):
    job = _job(jid)
    if job["status"] == "fila":
        atualizar(job, status="cancelado", mensagem="Cancelado")
    elif job["status"] in ("baixando", "extraindo"):
        atualizar(job, cancelar=True)
    return jsonify(ok=True)


@app.post("/jobs/<jid>/tentar")
@protegido
def tentar(jid):
    job = _job(jid)
    if job["status"] in ("erro", "cancelado"):
        for item in job["itens"]:
            if item["status"] != "ok":
                item.update(status="pendente", erro=None)
        atualizar(job, status="fila", mensagem="", cancelar=False)
        fila.put(jid)
    return jsonify(ok=True)


@app.post("/jobs/<jid>/renovar")
@protegido
def renovar(jid):
    job = _job(jid)
    if job["status"] == "pronto":
        atualizar(job, expira=time.time() + LINK_TTL_HOURS * 3600)
    return jsonify(ok=True)


@app.post("/jobs/<jid>/excluir")
@protegido
def excluir(jid):
    job = _job(jid)
    if job["status"] in ("baixando", "extraindo"):
        atualizar(job, cancelar=True, excluir=True)
    else:
        remover(job)
    return jsonify(ok=True)


# --------------------------------------------------------------------------- web: link publico

def job_do_token(token):
    for job in list(jobs.values()):
        t = job.get("token")
        if t and secrets.compare_digest(t.encode(), token.encode()):
            if job["status"] != "pronto" or time.time() > (job.get("expira") or 0):
                abort(410)
            return job
    abort(404)


def arquivos_de(job):
    saida = JOBS_DIR / job["id"] / "arquivos"
    itens = []
    for p in sorted(saida.rglob("*")):
        if p.is_file():
            rel = p.relative_to(saida).as_posix()
            itens.append({"rel": rel, "url": quote(rel), "tamanho": p.stat().st_size})
    return saida, itens


@app.get("/s/<token>")
def compartilhado(token):
    job = job_do_token(token)
    _, itens = arquivos_de(job)
    return render_template("share.html", job=job, itens=itens, token=token)


@app.get("/s/<token>/arquivo/<path:rel>")
def baixar_arquivo(token, rel):
    job = job_do_token(token)
    saida = JOBS_DIR / job["id"] / "arquivos"
    return send_from_directory(saida, rel, as_attachment=True)


class _Buffer:
    """Destino nao-pesquisavel para o zipfile: acumula bytes para irem sendo enviados."""

    def __init__(self):
        self.dados = bytearray()

    def write(self, b):
        self.dados += b
        return len(b)

    def flush(self):
        pass

    def retirar(self):
        out = bytes(self.dados)
        self.dados.clear()
        return out


@app.get("/s/<token>/tudo.zip")
def baixar_tudo(token):
    job = job_do_token(token)
    saida, itens = arquivos_de(job)

    def gerar():
        buf = _Buffer()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
            for it in itens:
                caminho = saida / it["rel"]
                info = zipfile.ZipInfo.from_file(caminho, it["rel"])
                info.compress_type = zipfile.ZIP_STORED
                with open(caminho, "rb") as src, z.open(info, "w", force_zip64=True) as dst:
                    while bloco := src.read(CHUNK):
                        dst.write(bloco)
                        if len(buf.dados) >= CHUNK:
                            yield buf.retirar()
                yield buf.retirar()
        yield buf.retirar()

    nome = re.sub(r"[^\w\- .]", "_", job["nome"]).strip() or "download"
    return Response(stream_with_context(gerar()), mimetype="application/zip",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(nome)}.zip"})


@app.errorhandler(404)
def nao_encontrado(_):
    return render_template("aviso.html", titulo="Não encontrado",
                           texto="Esse link não existe."), 404


@app.errorhandler(410)
def expirado(_):
    return render_template("aviso.html", titulo="Link expirado",
                           texto="Esse link não está mais disponível."), 410


@app.template_filter("tamanho")
def fmt_tamanho(b):
    b = float(b or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024 or u == "TB":
            return f"{b:.0f} {u}" if u == "B" else f"{b:.1f} {u}"
        b /= 1024


@app.template_filter("datahora")
def fmt_data(ts):
    return time.strftime("%d/%m/%Y %H:%M", time.localtime(ts)) if ts else "-"


# --------------------------------------------------------------------------- inicio

def iniciar():
    if not APP_PASSWORD:
        raise SystemExit("Defina a variável APP_PASSWORD (senha do painel) no docker-compose.yml")
    carregar()
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=limpeza, daemon=True).start()


if __name__ == "__main__":
    iniciar()
    from waitress import serve
    log.info("Painel em http://0.0.0.0:8080 (usuario: %s)", APP_USER)
    serve(app, host="0.0.0.0", port=8080, threads=16)
