"""
Coleta do Google Acadêmico — 458 artigos
Modo: scraping cauteloso + scholarly como fallback
Saída: metadados (CSV + JSON) + PDFs disponíveis

Instalar:
    pip install requests beautifulsoup4 scholarly tqdm
"""

import os, time, random, csv, json, re, logging, argparse
from pathlib import Path
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

# ──────────────────────────────────────────────────────────────
# CONFIGURAÇÕES
# ──────────────────────────────────────────────────────────────
OUTPUT_DIR  = Path("resultados_scholar")
PDF_DIR     = OUTPUT_DIR / "pdfs"
META_FILE   = OUTPUT_DIR / "metadados.csv"
JSON_FILE   = OUTPUT_DIR / "metadados.json"
PROGRESSO   = OUTPUT_DIR / "progresso.json"
LOG_FILE    = OUTPUT_DIR / "scraper.log"

# Delays entre páginas (segundos)
DELAY_PAG_MIN   = 20
DELAY_PAG_MAX   = 40

# Pausa entre lotes
DELAY_LOTE_MIN  = 6    # minutos
DELAY_LOTE_MAX  = 12
PAGINAS_POR_LOTE = 5   # ~50 artigos por lote

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

CAMPOS = ["titulo", "autores", "fonte", "ano", "citacoes",
          "resumo", "link", "pdf_url", "pdf_local", "origem"]

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
# PROGRESSO — salva e retoma de onde parou
# ──────────────────────────────────────────────────────────────
def carregar_progresso() -> dict:
    if PROGRESSO.exists():
        with open(PROGRESSO, encoding="utf-8") as f:
            return json.load(f)
    return {"ultima_pagina": 0, "total": 0, "ids_vistos": []}

def salvar_progresso(pagina: int, total: int, ids_vistos: list):
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
def criar_sessao(proxy: str = None) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "DNT": "1",
    })
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
        log.info(f"Proxy configurado: {proxy}")
    return s

def rotacionar_agente(sessao: requests.Session):
    sessao.headers["User-Agent"] = random.choice(USER_AGENTS)


# ──────────────────────────────────────────────────────────────
# EXTRAÇÃO DE METADADOS (scraping)
# ──────────────────────────────────────────────────────────────
def extrair_metadados(item) -> dict | None:
    try:
        titulo_tag = item.find("h3", class_="gs_rt")
        if not titulo_tag:
            return None
        titulo = titulo_tag.get_text(separator=" ", strip=True)
        titulo = re.sub(r'\[(PDF|BOOK|CITATION|HTML)\]', '', titulo).strip()

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
            resto   = " ".join(partes[1:])
            m = re.search(r'\b(19|20)\d{2}\b', resto)
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
            "origem":    "scraping",
        }
    except Exception as e:
        log.warning(f"Erro ao extrair item: {e}")
        return None


# ──────────────────────────────────────────────────────────────
# MODO 1: SCRAPING DIRETO
# ──────────────────────────────────────────────────────────────
def buscar_pagina_scraping(sessao: requests.Session, query: str,
                           start: int) -> tuple[list, bool]:
    """Retorna (artigos, continuar). continuar=False em CAPTCHA ou fim."""
    params = {"q": query, "hl": "pt", "start": start, "as_sdt": "0,5"}
    try:
        resp = sessao.get("https://scholar.google.com/scholar",
                          params=params, timeout=25)
    except requests.exceptions.RequestException as e:
        log.error(f"Erro de conexão: {e}")
        return [], True

    if resp.status_code == 429:
        log.warning("⛔ Status 429 — bloqueio. Pausando 30 minutos...")
        time.sleep(30 * 60)
        return [], True

    if resp.status_code != 200:
        log.error(f"Status inesperado: {resp.status_code}")
        return [], False

    soup = BeautifulSoup(resp.text, "html.parser")

    captcha = (
        "please show you're not a robot" in resp.text.lower()
        or soup.find("form", {"id": "gs_captcha_f"}) is not None
        or "g-recaptcha" in resp.text
    )
    if captcha:
        log.error(
            "\n" + "="*60 +
            "\n🛑 CAPTCHA DETECTADO!\n"
            "   1. Abra scholar.google.com no navegador\n"
            "   2. Resolva o CAPTCHA manualmente\n"
            "   3. Aguarde 2 horas\n"
            "   4. Rode o script novamente — retoma de onde parou\n" +
            "="*60
        )
        return [], False

    resultados = soup.find_all("div", class_="gs_r gs_or gs_scl")
    if not resultados:
        log.info("Página vazia — fim dos resultados ou estrutura alterada.")
        return [], False

    artigos = [a for item in resultados if (a := extrair_metadados(item))]
    return artigos, True


# ──────────────────────────────────────────────────────────────
# MODO 2: FALLBACK via biblioteca scholarly
# ──────────────────────────────────────────────────────────────
def buscar_scholarly(query: str, num_resultados: int = 50) -> list:
    """
    Usa a biblioteca scholarly como fallback quando o scraping falha.
    Mais lenta, mas mais resistente a bloqueios.
    """
    try:
        from scholarly import scholarly as sch, ProxyGenerator
    except ImportError:
        log.error("scholarly não instalada. Execute: pip install scholarly")
        return []

    log.info(f"[scholarly] Buscando '{query}' ({num_resultados} resultados)...")
    artigos = []
    try:
        busca = sch.search_pubs(query)
        for i, pub in enumerate(busca):
            if i >= num_resultados:
                break
            try:
                bib = pub.get("bib", {})
                artigo = {
                    "titulo":    bib.get("title", ""),
                    "autores":   ", ".join(bib.get("author", [])),
                    "fonte":     bib.get("venue", ""),
                    "ano":       str(bib.get("pub_year", "")),
                    "citacoes":  pub.get("num_citations", 0),
                    "resumo":    bib.get("abstract", ""),
                    "link":      pub.get("pub_url", ""),
                    "pdf_url":   pub.get("eprint_url", ""),
                    "pdf_local": "",
                    "origem":    "scholarly",
                }
                artigos.append(artigo)
                log.info(f"  [scholarly] {i+1}: {artigo['titulo'][:70]}")
                time.sleep(random.uniform(5, 10))
            except Exception as e:
                log.warning(f"  [scholarly] Erro no item {i}: {e}")
                continue
    except Exception as e:
        log.error(f"[scholarly] Erro na busca: {e}")

    return artigos


# ──────────────────────────────────────────────────────────────
# BUSCA PRINCIPAL — scraping + fallback scholarly
# ──────────────────────────────────────────────────────────────
def buscar_tudo(sessao: requests.Session, query: str,
                meta: int = 458, proxy: str = None) -> list:

    progresso  = carregar_progresso()
    inicio_pag = progresso["ultima_pagina"]
    ids_vistos = set(progresso.get("ids_vistos", []))
    todos      = []

    # Recarrega dados já salvos
    if META_FILE.exists() and inicio_pag > 0:
        with open(META_FILE, encoding="utf-8-sig") as f:
            todos = list(csv.DictReader(f))
        log.info(f"▶ Retomando da página {inicio_pag + 1} "
                 f"({len(todos)} artigos já coletados)")

    num_paginas    = (meta // 10) + 2
    paginas_lote   = 0
    falhas_seguidas = 0

    for pagina in range(inicio_pag, num_paginas):
        if len(todos) >= meta:
            log.info(f"Meta de {meta} artigos atingida!")
            break

        start = pagina * 10

        # Pausa entre lotes
        if paginas_lote > 0 and paginas_lote % PAGINAS_POR_LOTE == 0:
            minutos = random.uniform(DELAY_LOTE_MIN, DELAY_LOTE_MAX)
            log.info(f"\n⏸  Pausa de lote: {minutos:.1f} min...\n")
            time.sleep(minutos * 60)
            rotacionar_agente(sessao)

        log.info(f"[Scraping] Pág {pagina + 1} | coletados={len(todos)}/{meta}")
        artigos, continuar = buscar_pagina_scraping(sessao, query, start)

        # ── Fallback para scholarly se scraping falhar
        if not artigos and not continuar:
            log.warning("Scraping bloqueado. Ativando fallback scholarly...")
            faltam = meta - len(todos)
            artigos_fb = buscar_scholarly(query, num_resultados=min(faltam, 100))
            # Deduplica por título
            for a in artigos_fb:
                chave = a["titulo"].lower()[:60]
                if chave not in ids_vistos:
                    todos.append(a)
                    ids_vistos.add(chave)
            salvar_progresso(pagina, len(todos), list(ids_vistos))
            _append_csv(artigos_fb, primeiro=(not META_FILE.exists()))
            log.info(f"  scholarly adicionou {len(artigos_fb)} artigos")
            break

        if not continuar:
            break

        # Deduplica por título
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
            falhas_seguidas = 0
            log.info(f"  +{len(novos)} novos | Total: {len(todos)}")
        else:
            falhas_seguidas += 1
            if falhas_seguidas >= 3:
                log.warning("3 páginas sem resultados novos. Encerrando.")
                break

        paginas_lote += 1
        espera = random.uniform(DELAY_PAG_MIN, DELAY_PAG_MAX)
        log.info(f"  Próxima página em {espera:.0f}s...")
        time.sleep(espera)

    return todos


# ──────────────────────────────────────────────────────────────
# CSV INCREMENTAL
# ──────────────────────────────────────────────────────────────
def _append_csv(artigos: list, primeiro: bool = False):
    if not artigos:
        return
    modo = "w" if primeiro else "a"
    with open(META_FILE, modo, newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CAMPOS, extrasaction="ignore")
        if primeiro:
            writer.writeheader()
        writer.writerows(artigos)


# ──────────────────────────────────────────────────────────────
# DOWNLOAD DE PDFs
# ──────────────────────────────────────────────────────────────
def nome_seguro(titulo: str, idx: int) -> str:
    nome = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", titulo)[:80].strip()
    return f"{idx:04d}_{nome}.pdf"

def baixar_pdf(sessao: requests.Session, url: str, destino: Path) -> bool:
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

def baixar_todos_pdfs(sessao: requests.Session, artigos: list) -> list:
    log.info(f"Iniciando downloads de PDF ({sum(1 for a in artigos if a.get('pdf_url'))} disponíveis)...")
    salvos = 0
    for idx, artigo in enumerate(tqdm(artigos, desc="PDFs"), 1):
        if not artigo.get("pdf_url"):
            continue
        destino = PDF_DIR / nome_seguro(artigo["titulo"], idx)
        if destino.exists():
            artigo["pdf_local"] = str(destino)
            salvos += 1
            continue
        if baixar_pdf(sessao, artigo["pdf_url"], destino):
            artigo["pdf_local"] = str(destino)
            salvos += 1
            log.info(f"  ✔ {destino.name}")
        time.sleep(random.uniform(2, 6))
    log.info(f"PDFs baixados: {salvos}")
    return artigos


# ──────────────────────────────────────────────────────────────
# SALVAR JSON FINAL
# ──────────────────────────────────────────────────────────────
def salvar_json(artigos: list):
    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(artigos, f, ensure_ascii=False, indent=2)
    log.info(f"JSON salvo: {JSON_FILE}")


# ──────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Coleta Scholar — scraping cauteloso + fallback scholarly"
    )
    parser.add_argument("-q", "--query", required=True,
                        help='Descritores. Ex: "machine learning" AND "diagnosis"')
    parser.add_argument("-n", "--numero", type=int, default=458,
                        help="Nº de artigos desejados (padrão: 458)")
    parser.add_argument("--proxy",
                        help="Proxy institucional: http://user:pass@host:porta")
    parser.add_argument("--sem-pdf", action="store_true",
                        help="Pula download de PDFs")
    parser.add_argument("--so-scholarly", action="store_true",
                        help="Usa apenas scholarly (ignora scraping direto)")
    parser.add_argument("--resetar", action="store_true",
                        help="Começa do zero ignorando progresso salvo")
    args = parser.parse_args()

    if args.resetar and PROGRESSO.exists():
        PROGRESSO.unlink()
        log.info("Progresso resetado.")

    log.info("=" * 60)
    log.info(f"Query  : {args.query}")
    log.info(f"Meta   : {args.numero} artigos")
    log.info(f"Modo   : {'só scholarly' if args.so_scholarly else 'scraping + fallback scholarly'}")
    log.info("=" * 60)

    sessao = criar_sessao(proxy=args.proxy)

    if args.so_scholarly:
        artigos = buscar_scholarly(args.query, num_resultados=args.numero)
        _append_csv(artigos, primeiro=True)
    else:
        artigos = buscar_tudo(sessao, args.query, meta=args.numero, proxy=args.proxy)

    if not args.sem_pdf:
        artigos = baixar_todos_pdfs(sessao, artigos)
        # Reescreve CSV com pdf_local preenchido
        _append_csv(artigos, primeiro=True)

    salvar_json(artigos)

    por_origem = {}
    for a in artigos:
        o = a.get("origem", "?")
        por_origem[o] = por_origem.get(o, 0) + 1
    pdfs_ok = sum(1 for a in artigos if a.get("pdf_local"))

    print(f"\n{'='*50}")
    print(f"✅ Concluído!")
    print(f"   Artigos coletados : {len(artigos)}")
    for orig, qtd in por_origem.items():
        print(f"   └─ {orig:12s}: {qtd}")
    print(f"   PDFs baixados     : {pdfs_ok}")
    print(f"   Pasta de saída    : {OUTPUT_DIR.resolve()}")
    print(f"   Para retomar      : rode o mesmo comando novamente")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
