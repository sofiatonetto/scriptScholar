"""
Coleta do Google Acadêmico — "geotecnologias" e "ensino médio integrado"
Meta: ~111 resultados disponíveis
Instalar: pip install requests beautifulsoup4 tqdm
"""

import time, random, csv, json, re, logging, argparse
from pathlib import Path
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

# ──────────────────────────────────────────────────────────────
# CONFIGURAÇÕES
# ──────────────────────────────────────────────────────────────
OUTPUT_DIR  = Path("resultados_geo_emi")
PDF_DIR     = OUTPUT_DIR / "pdfs"
META_FILE   = OUTPUT_DIR / "metadados.csv"
JSON_FILE   = OUTPUT_DIR / "metadados.json"
PROGRESSO   = OUTPUT_DIR / "progresso.json"
LOG_FILE    = OUTPUT_DIR / "scraper.log"

DELAY_PAG_MIN    = 20
DELAY_PAG_MAX    = 40
DELAY_LOTE_MIN   = 6
DELAY_LOTE_MAX   = 12
PAGINAS_POR_LOTE = 5
MAX_VAZIAS       = 5

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

CAMPOS = ["titulo", "autores", "fonte", "ano", "citacoes",
          "resumo", "link", "pdf_url", "pdf_local"]

# ──────────────────────────────────────────────────────────────
# SETUP
# ──────────────────────────────────────────────────────────────
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
PDF_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
# PROGRESSO
# ──────────────────────────────────────────────────────────────
def carregar_progresso() -> dict:
    if PROGRESSO.exists():
        with open(PROGRESSO, encoding="utf-8") as f:
            return json.load(f)
    return {"ultima_pagina": 0, "total": 0, "ids_vistos": []}

def salvar_progresso(pagina, total, ids_vistos):
    with open(PROGRESSO, "w", encoding="utf-8") as f:
        json.dump({
            "ultima_pagina": pagina,
            "total": total,
            "ids_vistos": ids_vistos,
            "atualizado": datetime.now().isoformat()
        }, f, indent=2)


# ──────────────────────────────────────────────────────────────
# SESSÃO HTTP
# ──────────────────────────────────────────────────────────────
def criar_sessao(proxy=None):
    s = requests.Session()
    s.headers.update({
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "DNT": "1",
    })
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    return s

def rotacionar_agente(sessao):
    sessao.headers["User-Agent"] = random.choice(USER_AGENTS)


# ──────────────────────────────────────────────────────────────
# EXTRAÇÃO — aceita todos os artigos, com ou sem ano
# ──────────────────────────────────────────────────────────────
def extrair_metadados(item) -> dict | None:
    try:
        titulo_tag = item.find("h3", class_="gs_rt")
        if not titulo_tag:
            return None
        titulo = titulo_tag.get_text(separator=" ", strip=True)
        titulo = re.sub(r'\[(PDF|BOOK|CITATION|HTML)\]', '', titulo).strip()
        if not titulo:
            return None

        link_artigo = ""
        link_a = titulo_tag.find("a")
        if link_a:
            link_artigo = link_a.get("href", "")

        meta_tag = item.find("div", class_="gs_a")
        autores, fonte, ano = "", "", ""
        if meta_tag:
            meta_txt = meta_tag.get_text(separator="|||", strip=True)
            partes = meta_txt.split("|||")
            autores = partes[0].strip() if partes else ""
            fonte   = partes[1].strip() if len(partes) > 1 else ""
            resto   = " ".join(partes)
            m = re.search(r'\b(20\d{2}|19\d{2})\b', resto)
            ano = m.group(0) if m else ""

        resumo_tag = item.find("div", class_="gs_rs")
        resumo = resumo_tag.get_text(strip=True) if resumo_tag else ""

        pdf_url = ""
        pdf_div = item.find("div", class_="gs_or_ggsm")
        if pdf_div:
            pdf_a = pdf_div.find("a")
            if pdf_a:
                pdf_url = pdf_a.get("href", "")

        citacoes = 0
        for a in item.find_all("a"):
            txt = a.get_text(strip=True)
            if txt.startswith(("Citado por", "Cited by")):
                m = re.search(r'\d+', txt)
                citacoes = int(m.group(0)) if m else 0
                break

        return {
            "titulo":    titulo,
            "autores":   autores,
            "fonte":     fonte,
            "ano":       ano,
            "citacoes":  citacoes,
            "resumo":    resumo,
            "link":      link_artigo,
            "pdf_url":   pdf_url,
            "pdf_local": "",
        }
    except Exception as e:
        log.warning(f"Erro ao extrair: {e}")
        return None


# ──────────────────────────────────────────────────────────────
# BUSCA POR PÁGINA
# ──────────────────────────────────────────────────────────────
def buscar_pagina(sessao, query, start) -> tuple[list, str]:
    params = {
        "q":      query,
        "hl":     "pt",
        "start":  start,
        "as_sdt": "0,5",
    }
    try:
        resp = sessao.get("https://scholar.google.com/scholar",
                          params=params, timeout=25)
    except requests.exceptions.RequestException as e:
        log.error(f"Erro de conexão: {e}")
        return [], "erro"

    if resp.status_code == 429:
        log.warning("⛔ 429 — bloqueio. Pausando 30 min...")
        time.sleep(30 * 60)
        return [], "bloqueio"

    if resp.status_code != 200:
        log.error(f"Status: {resp.status_code}")
        return [], "erro"

    soup = BeautifulSoup(resp.text, "html.parser")

    if ("please show you're not a robot" in resp.text.lower()
            or soup.find("form", {"id": "gs_captcha_f"})
            or "g-recaptcha" in resp.text):
        log.error(
            "\n" + "="*60 +
            "\n🛑 CAPTCHA!\n"
            "   1. Abra scholar.google.com no navegador\n"
            "   2. Resolva o CAPTCHA\n"
            "   3. Aguarde 2 horas\n"
            "   4. Rode o script de novo — retoma de onde parou\n" +
            "="*60
        )
        return [], "captcha"

    resultados = soup.find_all("div", class_="gs_r gs_or gs_scl")
    if not resultados:
        return [], "vazia"

    artigos = [a for item in resultados if (a := extrair_metadados(item))]
    return artigos, "ok"


# ──────────────────────────────────────────────────────────────
# BUSCA PRINCIPAL
# ──────────────────────────────────────────────────────────────
def buscar_tudo(sessao, query, meta) -> list:
    progresso   = carregar_progresso()
    inicio_pag  = progresso["ultima_pagina"]
    ids_vistos  = set(progresso.get("ids_vistos", []))
    todos       = []

    if META_FILE.exists() and inicio_pag > 0:
        with open(META_FILE, encoding="utf-8-sig") as f:
            todos = list(csv.DictReader(f))
        log.info(f"▶ Retomando da pág {inicio_pag + 1} ({len(todos)} já coletados)")

    num_paginas  = max((meta // 10) + 5, 15)
    paginas_lote = 0
    vazias       = 0

    for pagina in range(inicio_pag, num_paginas):
        if len(todos) >= meta:
            log.info(f"✅ Meta de {meta} atingida!")
            break

        start = pagina * 10

        if paginas_lote > 0 and paginas_lote % PAGINAS_POR_LOTE == 0:
            minutos = random.uniform(DELAY_LOTE_MIN, DELAY_LOTE_MAX)
            log.info(f"\n⏸  Pausa: {minutos:.1f} min...\n")
            time.sleep(minutos * 60)
            rotacionar_agente(sessao)

        log.info(f"[Pág {pagina+1}/{num_paginas}] coletados={len(todos)} | vazias={vazias}/{MAX_VAZIAS}")

        artigos, status = buscar_pagina(sessao, query, start)

        if status == "captcha":
            break
        if status in ("bloqueio", "erro"):
            time.sleep(15)
            continue
        if status == "vazia":
            vazias += 1
            if vazias >= MAX_VAZIAS:
                log.info("Fim dos resultados.")
                break
            salvar_progresso(pagina + 1, len(todos), list(ids_vistos))
            paginas_lote += 1
            time.sleep(random.uniform(DELAY_PAG_MIN, DELAY_PAG_MAX))
            continue

        vazias = 0
        novos = []
        for a in artigos:
            chave = a["titulo"].lower()[:60]
            if chave not in ids_vistos:
                todos.append(a)
                novos.append(a)
                ids_vistos.add(chave)

        if novos:
            _append_csv(novos, primeiro=(not META_FILE.exists() and pagina == inicio_pag))
            salvar_progresso(pagina + 1, len(todos), list(ids_vistos))
            log.info(f"  ✔ +{len(novos)} | Total: {len(todos)}")
        else:
            log.info(f"  Nenhum novo (duplicatas)")

        paginas_lote += 1
        espera = random.uniform(DELAY_PAG_MIN, DELAY_PAG_MAX)
        log.info(f"  Próxima em {espera:.0f}s...")
        time.sleep(espera)

    return todos


# ──────────────────────────────────────────────────────────────
# CSV / JSON
# ──────────────────────────────────────────────────────────────
def _append_csv(artigos, primeiro=False):
    if not artigos:
        return
    with open(META_FILE, "w" if primeiro else "a",
              newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS, extrasaction="ignore")
        if primeiro:
            w.writeheader()
        w.writerows(artigos)

def salvar_csv_final(artigos):
    ordenados = sorted(artigos,
                       key=lambda a: int(a.get("ano") or 0),
                       reverse=True)
    with open(META_FILE, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS, extrasaction="ignore")
        w.writeheader()
        w.writerows(ordenados)
    log.info(f"CSV final: {len(ordenados)} artigos → {META_FILE}")

def salvar_json(artigos):
    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(artigos, f, ensure_ascii=False, indent=2)


# ──────────────────────────────────────────────────────────────
# DOWNLOAD DE PDFs
# ──────────────────────────────────────────────────────────────
def nome_seguro(titulo, idx):
    nome = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", titulo)[:80].strip()
    return f"{idx:04d}_{nome}.pdf"

def baixar_pdf(sessao, url, destino):
    try:
        resp = sessao.get(url, timeout=30, stream=True, allow_redirects=True)
        ct = resp.headers.get("Content-Type", "")
        if resp.status_code != 200:
            return False
        if "pdf" not in ct and not url.lower().endswith(".pdf"):
            return False
        with open(destino, "wb") as f:
            for chunk in resp.iter_content(8192):
                f.write(chunk)
        return True
    except Exception:
        return False

def baixar_todos_pdfs(sessao, artigos):
    com_pdf = sum(1 for a in artigos if a.get("pdf_url"))
    log.info(f"Baixando PDFs ({com_pdf} disponíveis de {len(artigos)} artigos)...")
    salvos = 0
    for idx, artigo in enumerate(tqdm(artigos, desc="PDFs"), 1):
        if not artigo.get("pdf_url"):
            continue
        destino = PDF_DIR / nome_seguro(artigo["titulo"], idx)
        if destino.exists():
            artigo["pdf_local"] = str(destino.resolve())
            salvos += 1
            continue
        if baixar_pdf(sessao, artigo["pdf_url"], destino):
            artigo["pdf_local"] = str(destino.resolve())
            salvos += 1
            log.info(f"  ✔ {destino.name}")
        time.sleep(random.uniform(2, 6))
    log.info(f"PDFs salvos: {salvos}")
    return artigos


# ──────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description='Coleta Scholar — "geotecnologias" e "ensino médio integrado"'
    )
    parser.add_argument("-q", "--query", required=True)
    parser.add_argument("-n", "--numero", type=int, default=120,
                        help="Meta de artigos (padrão: 120, Scholar tem ~111)")
    parser.add_argument("--proxy")
    parser.add_argument("--sem-pdf", action="store_true")
    parser.add_argument("--resetar", action="store_true")
    args = parser.parse_args()

    if args.resetar:
        for f in [PROGRESSO, META_FILE]:
            if f.exists():
                f.unlink()
        log.info("Resetado.")

    log.info("=" * 60)
    log.info(f"Query  : {args.query}")
    log.info(f"Meta   : {args.numero} artigos (~111 disponíveis no Scholar)")
    log.info("=" * 60)

    sessao  = criar_sessao(proxy=args.proxy)
    artigos = buscar_tudo(sessao, args.query, args.numero)

    if not args.sem_pdf:
        artigos = baixar_todos_pdfs(sessao, artigos)

    salvar_csv_final(artigos)
    salvar_json(artigos)

    por_ano = {}
    for a in artigos:
        ano = a.get("ano") or "sem ano"
        por_ano[ano] = por_ano.get(ano, 0) + 1

    pdfs_ok = sum(1 for a in artigos if a.get("pdf_local"))

    print(f"\n{'='*50}")
    print(f"✅ Concluído!")
    print(f"   Artigos coletados : {len(artigos)}")
    print(f"   PDFs baixados     : {pdfs_ok}")
    print(f"   Pasta de saída    : {OUTPUT_DIR.resolve()}")
    print(f"\n   Por ano:")
    for ano in sorted(por_ano.keys(), reverse=True):
        print(f"   └─ {ano}: {por_ano[ano]}")
    print(f"{'='*50}")

if __name__ == "__main__":
    main()
