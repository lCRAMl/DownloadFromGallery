# -*- coding: utf-8 -*-
"""
v0.10  Coppermine full-res downloader
 - sammelt displayimage.php Links von allen Seiten
 - folgt zu fullsize-Pages (fullsize=1 / popup)
 - extrahiert <img id="fullsize_image"> oder albums/ src
 - lädt Full-Res Bilder asynchron mit begrenzter Parallelität
 
 Kurze Anleitung & Debugging-Tipps

do_download = False — läuft schneller, nur URLs sammeln. Prüfe Ausgabe: len(displaypage_list) und len(filelist).

Wenn displaypage_list leer ist → Problem beim Parsen der Thumbnail-Seite (User-Agent, Captcha, Bot-Block). Probier headers['User-Agent'] manuell auf einen Browser UA.

Wenn displaypage_list gefüllt, aber filelist leer → extract_fullres_from_displaypage findet kein fullsize=1 oder img#fullsize_image. Poste dann eine konkrete displayimage.php?...-URL, ich passe die Suche an.

Bei Zertifikat-Fehlern: SSL = False (nur temporär).

Für sehr große Galerien kannst du concurrency in gather_all_fullres_urls reduzieren/erhöhen.
 
"""

URL = 'https://www.reese-witherspoon.org/gallery/thumbnails.php?album=1378'
dest = 'Z:\\Downloads\\Reese Witherspoon - 2014 Vanity Fair Oscars Party in West Hollywood 03_02_14\\'
picprefix = 'fansite_'
nbrOfParallelDL = 5
SSL = True  # Set False to ignore cert errors (not recommended)
do_download = True  # Set False to only collect URLs (debug)

import asyncio
import httpx
from bs4 import BeautifulSoup
from urllib.parse import urljoin, unquote
import re
import os
import random
import tqdm
from tenacity import retry, stop_after_attempt, wait_fixed, wait_random
from skimage import io

# ---------------------------
# headers / UA
# ---------------------------
user_agents = [ 
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:101.0) Gecko/20100101 Firefox/101.0',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36', 
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/92.0.4515.107 Safari/537.36', 
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.212 Safari/537.36', 
    'Mozilla/5.0 (iPhone; CPU iPhone OS 12_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148', 
    'Mozilla/5.0 (Linux; Android 11; SM-G960U) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/89.0.4389.72 Mobile Safari/537.36' 
]
user_agent = random.choice(user_agents)

headers = {
    'User-Agent': user_agent,
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
    'Referer': URL,
    'Connection': 'keep-alive',
    'Cache-Control': 'no-cache',
    'Upgrade-Insecure-Requests': '1',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'same-origin',
    'Sec-Fetch-User': '?1',
    'TE': 'trailers',
}

# ---------------------------
# state
# ---------------------------
filelist: list[str] = []
displaypage_list: list[str] = []

# ---------------------------
# utilities
# ---------------------------
def createdirectory(path):
    try:
        if not os.path.exists(path):
            os.makedirs(path)
    except Exception as e:
        print("ERROR creating directory:", e)

def verify_image(img_file):
    try:
        io.imread(img_file)
        return True
    except Exception:
        return False

# ---------------------------
# network helpers
# ---------------------------

async def fetch_text(client: httpx.AsyncClient, url: str, *, raise_on_error=True) -> str | None:
    try:
        r = await client.get(url, headers=headers)
        if raise_on_error:
            r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"[fetch_text] Failed {url}: {e}")
        return None

# ---------------------------
# pagination: count pages robust
# ---------------------------
def countnumberofsites(siteURL: str) -> int:
    try:
        import requests
        r = requests.get(siteURL, headers=headers, verify=SSL, timeout=20)
        r.raise_for_status()
    except Exception as e:
        print("countnumberofsites: request failed:", e)
        return 1

    soup = BeautifulSoup(r.content, "html.parser")
    pages = []
    for a in soup.find_all("a", href=True):
        href = str(a.get("href", ""))
        m = re.search(r"[?&]page=(\d+)", href)
        if m:
            try:
                pages.append(int(m.group(1)))
            except:
                pass
    if pages:
        mx = max(pages)
        print(f"{mx} pages found")
        return mx
    else:
        print("Only 1 page found")
        return 1

# ---------------------------
# extract displayimage links from thumbnail page
# ---------------------------
async def collect_display_pages(thumbnail_page_url: str, client: httpx.AsyncClient) -> None:
    html = await fetch_text(client, thumbnail_page_url)
    if not html:
        return

    soup = BeautifulSoup(html, "html.parser")
    # prefer td.thumbnails a[href]
    for td in soup.find_all("td", class_=lambda c: c and "thumbnails" in c):
        a = td.find("a", href=True)
        if not a:
            continue
        href = a["href"].strip()
        full = urljoin(thumbnail_page_url, href)
        if full not in displaypage_list:
            displaypage_list.append(full)

    # fallback: any a[href*='displayimage.php?pid=']
    for a in soup.select("a[href*='displayimage.php?pid=']"):
        full = urljoin(thumbnail_page_url, a["href"])
        if full not in displaypage_list:
            displaypage_list.append(full)

# ---------------------------
# extract fullres von display page (erweitert, Coppermine & Varianten)
# ---------------------------
@retry(stop=stop_after_attempt(5), wait=wait_random(1, 3))
async def extract_fullres_from_displaypage(display_url: str, client: httpx.AsyncClient) -> str | None:
    """
    Versucht mehrere Strategien, um das beste Bild zu finden.
    Fallback: Wenn kein Fullsize gefunden, wird Thumbnail/Medium genutzt.
    """
    def looks_like_full_image(src: str) -> bool:
        if not src:
            return False
        lower = src.lower()
        # Ausschließen klarer Thumbs/Platzhalter
        if any(x in lower for x in ("placeholder", "missing", "avatar")):
            return False
        if not lower.endswith(('.jpg', '.jpeg', '.png', '.webp')):
            return False
        return True

    async def head_ok(url: str) -> bool:
        try:
            r = await client.head(url, headers=headers, timeout=10, follow_redirects=True)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
                cl = int(r.headers.get("content-length", 0) or 0)
                if cl < 512:
                    return False
                return True
            return False
        except Exception:
            # HEAD kann blockiert sein, GET-Fallback
            try:
                r = await client.get(url, headers=headers, timeout=10, follow_redirects=True)
                if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
                    cl = int(r.headers.get("content-length", 0) or 0)
                    if cl < 512:
                        return False
                    return True
            except Exception:
                return False
        return False

    try:
        r = await client.get(display_url, headers=headers, timeout=30)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        print(f"[extract] failed to load display page {display_url}: {e}")
        return None

    soup = BeautifulSoup(html, "html.parser")

    # ---------------------------
    # Helper: Kandidaten prüfen
    # ---------------------------
    async def check_img_candidate(src: str, base_url: str = "") -> str | None:
        """
        Prüft, ob ein Bild-URL-Kandidat existiert und vermutlich ein echtes Bild ist.
        Akzeptiert auch Medium/Thumbnail als Fallback, solange es kein winziges Placeholder ist.
        """
        if not src:
            return None

        # absoluten URL erstellen
        if base_url:
            src = urljoin(base_url, src.strip())
        else:
            src = src.strip()

        lower = src.lower()

        # sofort ausschließen, wenn eindeutiger Platzhalter
        if any(x in lower for x in ("placeholder", "missing", "avatar")):
            return None
        if not lower.endswith(('.jpg', '.jpeg', '.png', '.webp')):
            return None

        # HEAD GET check
        try:
            async with httpx.AsyncClient() as client:
                r = await client.head(src, timeout=10, follow_redirects=True)
                if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
                    cl = int(r.headers.get("content-length", 0) or 0)
                    if cl < 512:  # sehr kleines Bild → wahrscheinlich Platzhalter
                        return None
                    return src
                # Fallback GET, falls HEAD blockiert
                r = await client.get(src, timeout=10, follow_redirects=True)
                if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
                    cl = int(r.headers.get("content-length", 0) or 0)
                    if cl < 512:
                        return None
                    return src
        except Exception:
            return None

        return None

    # ---------------------------
    # 1) Fullsize Links / Popups
    # ---------------------------
    for a in soup.find_all("a", onclick=True):
        onclick = a.get("onclick", "")
        m = re.search(r"MM_openBrWindow\(\s*['\"]([^'\"]*fullsize=1[^'\"]*)['\"]", onclick)
        if not m:
            m = re.search(r"window\.open\(\s*['\"]([^'\"]*fullsize=1[^'\"]*)['\"]", onclick)
        if m:
            fullsize_page = urljoin(display_url, m.group(1))
            img = await _get_img_from_candidate_page(fullsize_page, client)
            if img:
                return img

    # anchor href mit fullsize param
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if any(x in href for x in ("fullsize=1", "view=popup", "size=original")):
            candidate = urljoin(display_url, href)
            img = await _get_img_from_candidate_page(candidate, client)
            if img:
                return img

    # ---------------------------
    # 2) id="fullsize_image"
    # ---------------------------
    img = soup.find("img", id="fullsize_image")
    if img:
        src = img.get("src") or img.get("data-src")
        candidate = await check_img_candidate(src)
        if candidate:
            return candidate

    # ---------------------------
    # 3) class="image"
    # ---------------------------
    img = soup.find("img", class_=lambda c: c and "image" in c.split())
    if img:
        src = img.get("src") or img.get("data-src")
        candidate = await check_img_candidate(src)
        if candidate:
            return candidate

    # ---------------------------
    # 4) srcset / meta og:image
    # ---------------------------
    img_srcset = soup.find("img", srcset=True)
    if img_srcset:
        srcset_urls = [u.split()[0] for u in img_srcset.get("srcset", "").split(",")]
        for s in srcset_urls:
            candidate = await check_img_candidate(s)
            if candidate:
                return candidate

    meta_og = soup.find("meta", property="og:image")
    if meta_og and meta_og.get("content"):
        candidate = await check_img_candidate(meta_og["content"])
        if candidate:
            return candidate

    # ---------------------------
    # 5) any /albums/ image conservative
    # ---------------------------
    any_img = soup.find("img", src=re.compile(r"/?albums/"))
    if any_img:
        src = any_img.get("src") or any_img.get("data-src")
        candidate = await check_img_candidate(urljoin(display_url, src))
        if candidate:
            return candidate

    # ---------------------------
    # 6) Letzter Fallback: größtes <img> auf Seite
    # ---------------------------
    imgs = []
    for tag in soup.find_all("img"):
        src = tag.get("src") or tag.get("data-src")
        if src:
            imgs.append(src)
    if imgs:
        # Wähle die erste akzeptable URL als Fallback
        for src in imgs:
            candidate = await check_img_candidate(src)
            if candidate:
                return candidate

    # nichts gefunden
    return None




# ---------------------------
# helper mit retry für candidate page
# ---------------------------
@retry(stop=stop_after_attempt(5), wait=wait_random(1, 3))
async def _get_img_from_candidate_page(page_url: str, client: httpx.AsyncClient) -> str | None:
    """
    Lade candidate page (popup/fullpage) und extrahiere erstes valides img.
    Prüft zuerst alle <img> direkt auf der Seite, danach data-src, srcset, meta og:image, any albums/.
    """
    def looks_like_full_image(src: str) -> bool:
        if not src:
            return False
        lower = src.lower()
        if "thumb" in lower or "normal" in lower or "placeholder" in lower or "missing" in lower:
            return False
        if not lower.endswith(('.jpg', '.jpeg', '.png', '.webp')):
            return False
        return True

    try:
        r = await client.get(page_url, headers=headers, timeout=30)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        print(f"[candidate] failed to load {page_url}: {e}")
        return None

    soup = BeautifulSoup(html, "html.parser")

    # ---------------------------
    # 1) Prüfe alle img-Tags auf der Seite
    # ---------------------------
    for img_tag in soup.find_all("img"):
        src = img_tag.get("src") or img_tag.get("data-src")
        if src:
            full_url = urljoin(page_url, src.strip())
            if looks_like_full_image(full_url):
                return full_url

    # ---------------------------
    # 2) data-src explizit prüfen
    # ---------------------------
    img = soup.find("img", attrs={"data-src": True})
    if img:
        src = img.get("data-src")
        if src:
            full_url = urljoin(page_url, src.strip())
            if looks_like_full_image(full_url):
                return full_url

    # ---------------------------
    # 3) srcset fallback
    # ---------------------------
    img_srcset = soup.find("img", srcset=True)
    if img_srcset:
        srcset_urls = [url.split()[0] for url in img_srcset["srcset"].split(",")]
        for src in srcset_urls:
            full_url = urljoin(page_url, src.strip())
            if looks_like_full_image(full_url):
                return full_url

    # ---------------------------
    # 4) Meta og:image fallback
    # ---------------------------
    meta_og = soup.find("meta", property="og:image")
    if meta_og and meta_og.get("content"):
        full_url = urljoin(page_url, meta_og["content"].strip())
        if looks_like_full_image(full_url):
            return full_url

    # ---------------------------
    # 5) Letzter Fallback: any albums/ src
    # ---------------------------
    any_img = soup.find("img", src=re.compile(r"/?albums/"))
    if any_img:
        src = any_img.get("src", "").strip()
        full_url = urljoin(page_url, src)
        if looks_like_full_image(full_url):
            return full_url

    return None


# ---------------------------
# downloader (with retry)
# ---------------------------
@retry(stop=stop_after_attempt(3), wait=wait_fixed(2))
async def download_image(url: str, destination_dir: str, prefix: str, client: httpx.AsyncClient):
    # clean url encoding
    url = url.strip()
    url = url.replace(" ", "%20")  # ensure spaces encoded
    filename = unquote(url.split("/")[-1])
    target = os.path.join(destination_dir, prefix + filename)

    if os.path.exists(target) and verify_image(target):
        print(f"SKIP exists: {target}")
        return

    try:
        async with client.stream("GET", url, headers=headers) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            with open(target, "wb") as f:
                with tqdm.tqdm(desc=filename, total=total, unit="B", unit_scale=True, miniters=1) as pb:
                    downloaded = r.num_bytes_downloaded
                    async for chunk in r.aiter_bytes():
                        f.write(chunk)
                        pb.update(r.num_bytes_downloaded - downloaded)
                        downloaded = r.num_bytes_downloaded
        # quick verify, optional
        if not verify_image(target):
            print("Downloaded but verify failed:", target)
    except Exception as e:
        print("Download failed", url, e)
        # allow tenacity to retry by raising
        raise

# ---------------------------
# main flow
# ---------------------------
async def gather_all_display_pages(base_url: str, pages: int = 1):
    async with httpx.AsyncClient(timeout=30, verify=SSL) as client:
        tasks = []
        for p in range(1, pages + 1):
            page_url = base_url + (f"&page={p}" if "page=" not in base_url else f"&page={p}")
            tasks.append(collect_display_pages(page_url, client))
        await asyncio.gather(*tasks)

async def gather_all_fullres_urls(concurrency: int = 10):
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(timeout=30, verify=SSL) as client:
        pbar = tqdm.tqdm(total=len(displaypage_list),
                         desc="Resolving URLs",
                         unit="page",
                         ncols=120,
                         leave=True)  # leave=True hält die Bar am Ende sichtbar

        @retry(stop=stop_after_attempt(5), wait=wait_fixed(1))
        async def worker(display_url):
            async with sem:
                #await asyncio.sleep(random.uniform(0.3, 1.0))
                full = await extract_fullres_from_displaypage(display_url, client)
                if full and full not in filelist:
                    filelist.append(full)

                # Letzten Bildnamen in der tqdm Bar anzeigen
                filename = unquote(full.split("/")[-1]) if full else "None"
                pbar.set_postfix(file=filename)

                if not full:
                    print(f"[WARNING] No fullsize found (retry may help): {display_url}")
                
                pbar.update(1)

        tasks = [worker(d) for d in displaypage_list]
        await asyncio.gather(*tasks)
        pbar.close()

async def download_all_images(destination: str, prefix: str, parallel: int = 5):
    sem = asyncio.Semaphore(parallel)
    async with httpx.AsyncClient(timeout=60, verify=SSL) as client:
        async def dl_worker(url):
            async with sem:
                try:
                    await download_image(url, destination, prefix, client)
                except Exception as e:
                    print("download worker exception:", e)
        tasks = [dl_worker(u) for u in filelist]
        await asyncio.gather(*tasks)

async def main():
    createdirectory(dest)
    pages = countnumberofsites(URL)
    print("Collecting display pages from thumbnail pages...")
    await gather_all_display_pages(URL, pages)
    print(f"{len(displaypage_list)} display pages found")

    print("Resolving full-resolution image URLs (this may take a while)...")
    await gather_all_fullres_urls(concurrency=20)
    print(f"{len(filelist)} full-resolution image URLs collected (sample first 10):")
    for i, u in enumerate(filelist[:10], start=1):
        print(f"{i:02d}. {u}")

    if do_download and filelist:
        print("Starting downloads...")
        await download_all_images(dest, picprefix, nbrOfParallelDL)
        print("Downloads finished.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Interrupted by user.")
