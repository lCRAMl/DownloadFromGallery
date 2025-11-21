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

URL = 'https://kerirussellweb.com/gallery/thumbnails.php?album=1754'
dest = 'Z:\\Downloads\\Keri Russell - Netflixs The Diplomat FYC Event - November 8 2025\\'
picprefix = 'Photoshoot'
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
    'Referer': 'https://www.google.com',
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
        m = re.search(r"[?&]page=(\d+)", a["href"])
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
# extract fullres von display page
# ---------------------------
@retry(stop=stop_after_attempt(5), wait=wait_random(1, 3))
async def extract_fullres_from_displaypage(display_url: str, client: httpx.AsyncClient) -> str | None:
    """
    Von displayimage.php -> popup/fullsize page -> final image URL.
    Erweiterte Fallbacks: <meta property="og:image">, img[srcset]
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
        r = await client.get(display_url, headers=headers, timeout=30)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        print(f"[extract] failed to load display page {display_url}: {e}")
        return None

    soup = BeautifulSoup(html, "html.parser")

    # 1) Suche nach <a href="...fullsize=1...">
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "fullsize=1" in href or "fullsize=true" in href:
            fullpage = urljoin(display_url, href)
            img = await _get_img_from_candidate_page(fullpage, client)
            if img and looks_like_full_image(img):
                return img

    # 2) Suche JS open pattern window.open('...fullsize=1...')
    m = re.search(r"window\.open\(\s*['\"]([^'\"]*fullsize=1[^'\"]*)['\"]", html)
    if m:
        fullpage = urljoin(display_url, m.group(1))
        img = await _get_img_from_candidate_page(fullpage, client)
        if img and looks_like_full_image(img):
            return img

    # 3) Prüfe display page selbst nach Fullsize <img>
    img_tag = soup.find("img", id="fullsize_image") or soup.find("img", attrs={"data-src": True})
    if img_tag:
        src = img_tag.get("data-src") or img_tag.get("src")
        if src:
            final = urljoin(display_url, src.strip())
            if looks_like_full_image(final):
                return final

    # 4) Meta og:image fallback
    meta_og = soup.find("meta", property="og:image")
    if meta_og and meta_og.get("content"):
        final = urljoin(display_url, meta_og["content"].strip())
        if looks_like_full_image(final):
            return final

    # 5) srcset fallback
    img_srcset = soup.find("img", srcset=True)
    if img_srcset:
        srcset_urls = [url.split()[0] for url in img_srcset["srcset"].split(",")]
        for src in srcset_urls:
            final = urljoin(display_url, src.strip())
            if looks_like_full_image(final):
                return final

    # 6) Letzter Fallback: any albums/ src
    any_img = soup.find("img", src=re.compile(r"/?albums/"))
    if any_img:
        src = any_img.get("src", "").strip()
        final = urljoin(display_url, src)
        if looks_like_full_image(final):
            return final
    return None

# ---------------------------
# helper mit retry für candidate page
# ---------------------------
@retry(stop=stop_after_attempt(5), wait=wait_random(1, 3))
async def _get_img_from_candidate_page(page_url: str, client: httpx.AsyncClient) -> str | None:
    """
    Lade candidate page (popup/fullpage) und extrahiere erstes valides img:
    id="fullsize_image", data-src, srcset, meta og:image, any albums/
    """
    try:
        r2 = await client.get(page_url, headers=headers, timeout=30)
        r2.raise_for_status()
        html2 = r2.text
    except Exception as e:
        return None

    soup2 = BeautifulSoup(html2, "html.parser")

    def looks_like_full_image(src: str) -> bool:
        if not src:
            return False
        lower = src.lower()
        if "thumb" in lower or "normal" in lower or "placeholder" in lower or "missing" in lower:
            return False
        if not lower.endswith(('.jpg', '.jpeg', '.png', '.webp')):
            return False
        return True

    # Priorität: id=fullsize_image
    img = soup2.find("img", id="fullsize_image")
    if img and img.get("src"):
        src = urljoin(page_url, img["src"].strip())
        if looks_like_full_image(src):
            return src

    # next: data-src
    img = soup2.find("img", attrs={"data-src": True})
    if img:
        src = urljoin(page_url, img["data-src"].strip())
        if looks_like_full_image(src):
            return src

    # next: srcset
    img_srcset = soup2.find("img", srcset=True)
    if img_srcset:
        srcset_urls = [url.split()[0] for url in img_srcset["srcset"].split(",")]
        for src in srcset_urls:
            final = urljoin(page_url, src.strip())
            if looks_like_full_image(final):
                return final

    # meta og:image
    meta_og = soup2.find("meta", property="og:image")
    if meta_og and meta_og.get("content"):
        final = urljoin(page_url, meta_og["content"].strip())
        if looks_like_full_image(final):
            return final

    # any albums/ src
    img = soup2.find("img", src=re.compile(r"/?albums/"))
    if img and img.get("src"):
        final = urljoin(page_url, img["src"].strip())
        if looks_like_full_image(final):
            return final

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

        @retry(stop=stop_after_attempt(5), wait=wait_fixed(1))
        async def worker(display_url):
            async with sem:
                # kleines Delay zwischen Requests
                await asyncio.sleep(random.uniform(0.3, 1.0))
                full = await extract_fullres_from_displaypage(display_url, client)
                if full and full not in filelist:
                    filelist.append(full)
                if not full:
                    print(f"[WARNING] No fullsize found (retry may help): {display_url}")

        tasks = [worker(d) for d in displaypage_list]
        await asyncio.gather(*tasks)

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
