# -*- coding: utf-8 -*-
r"""
          _____                    _____                    _____                    _____          
         /\    \                  /\    \                  /\    \                  /\    \         
        /::\    \                /::\    \                /::\    \                /::\____\        
       /::::\    \              /::::\    \              /::::\    \              /::::|   |        
      /::::::\    \            /::::::\    \            /::::::\    \            /:::::|   |        
     /:::/\:::\    \          /:::/\:::\    \          /:::/\:::\    \          /::::::|   |        
    /:::/  \:::\    \        /:::/__\:::\    \        /:::/__\:::\    \        /:::/|::|   |        
   /:::/    \:::\    \      /::::\   \:::\    \      /::::\   \:::\    \      /:::/ |::|   |        
  /:::/    / \:::\    \    /::::::\   \:::\    \    /::::::\   \:::\    \    /:::/  |::|___|______  
 /:::/    /   \:::\    \  /:::/\:::\   \:::\____\  /:::/\:::\   \:::\    \  /:::/   |::::::::\    \ 
/:::/____/     \:::\____\/:::/  \:::\   \:::|    |/:::/  \:::\   \:::\____\/:::/    |:::::::::\____\
\:::\    \      \::/    /\::/   |::::\  /:::|____|\::/    \:::\  /:::/    /\::/    / ~~~~~/:::/    /
 \:::\    \      \/____/  \/____|:::::\/:::/    /  \/____/ \:::\/:::/    /  \/____/      /:::/    / 
  \:::\    \                    |:::::::::/    /            \::::::/    /               /:::/    /  
   \:::\    \                   |::|\::::/    /              \::::/    /               /:::/    /   
    \:::\    \                  |::| \::/____/               /:::/    /               /:::/    /    
     \:::\    \                 |::|  ~|                    /:::/    /               /:::/    /     
      \:::\    \                |::|   |                   /:::/    /               /:::/    /      
       \:::\____\               \::|   |                  /:::/    /               /:::/    /       
        \::/    /                \:|   |                  \::/    /                \::/    /        
         \/____/                  \|___|                   \/____/                  \/____/         
                                                                                             
v0.01   2022-11-10   First running version
v0.02   2023-12-28   Added progressbar
v0.03   2024-05-04   Code finds picurl and number of sites automatically
v0.04   2024-05-05   fixed urlbasepath and only download when file not exists or is corrupt
v0.05   2024-05-05   working async download WITHOUT limit asyncio.Semaphore()
v0.06   2024-05-06   limit parallel downloads
testing github actions
v0.07   2024-08-10   checking if site index can be converted to int
v0.08   2025-03-12   added conncetion retry and send headers in every request
v0.09   2025-07-27   added SSL verification option, added more info when file already exists 
"""
"""----------------------------------------------------------------------------
        |||||        |||||
        vvvvv CONFIG vvvvv
-------------------------------------------------------------------------------
"""
URL = 'https://monicabarbaro.org/thumbnails.php?album=173'
#dest = 'C:\\Users\\silence\\Desktop\\ja\\2014\\Jessica Alba - Samsung Hope For Children Gala in NYC 2014-06-10\\'
#dest = 'C:\\Users\\silence\\Desktop\\Ana de Armas\\2021\\Ana de Armas - No Time To Die Premiere in London 2021-09-28\\'
dest = 'Z:\\Downloads\\Monica Barbaro - Attends Christian Dior Womenswear Spring_Summer 2026 Fashion Show at Paris Fashion Week in Paris, France. October 01, 2025\\'
picprefix = 'site_'
nbrOfParallelDL = 5
SSL = True  # Set to False if you want to verify SSL certificates

"""----------------------------------------------------------------------------
        ^^^^^        ^^^^^
        ||||| CONFIG ||||| 
-------------------------------------------------------------------------------
"""

import requests
from bs4 import BeautifulSoup
import random
import os
import tqdm
from skimage import io
import asyncio
import httpx
from tenacity import retry, stop_after_attempt, wait_fixed
import re
from urllib.parse import urljoin

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

global filelist
filelist = []

def verify_image(img_file):
    try:
        io.imread(img_file)
        return True
    except Exception:
        return False
    
    
def createdirectory(dest):
    try:
        if not os.path.exists(dest):
            os.makedirs(dest)
    except:
        print("ERROR: Cannot create directory %s" % dest)


@retry(stop=stop_after_attempt(3), wait=wait_fixed(2))
async def download(dirtyimagepath: str, destination: str, prefix: str):
    strtoremove = '%20'
    if strtoremove in dirtyimagepath:
        imagepath = dirtyimagepath.replace(strtoremove, " ")
    else:
        imagepath = dirtyimagepath

    filename = imagepath.split('/')[-1]
    fulldestinationname = os.path.join(destination, prefix + filename)

    if not os.path.exists(fulldestinationname) or (os.path.exists(fulldestinationname) and not verify_image(fulldestinationname)):
        async with httpx.AsyncClient(timeout=30, verify=SSL) as client:
            try:
                async with client.stream('GET', imagepath, headers=headers) as r:
                    if r.status_code == 200:
                        with open(fulldestinationname, 'wb') as f:
                            r.raise_for_status()
                            total = int(r.headers.get('content-length', 0))
                            tqdm_params = {
                                'desc': filename + " -> " + prefix + filename,
                                'total': total,
                                'miniters': 1,
                                'unit': 'B',
                                'unit_scale': True,
                                'unit_divisor': 1024,
                            }
                            with tqdm.tqdm(**tqdm_params) as pb:
                                downloaded = r.num_bytes_downloaded
                                async for chunk in r.aiter_bytes():
                                    pb.update(r.num_bytes_downloaded - downloaded)
                                    f.write(chunk)
                                    downloaded = r.num_bytes_downloaded
                    else:
                        print(f"{imagepath} Response: {r.status_code}")
            except httpx.RequestError as exc:
                print(f"An error occurred while requesting {exc.request.url!r}: {exc}")
            except httpx.HTTPStatusError as exc:
                print(f"Error response {exc.response.status_code} while requesting {exc.request.url!r}: {exc}")

    else:
        print(f"File {fulldestinationname} already exists and is valid, skipping download.")
        

async def getImageUrlfromSite(siteURL: str) -> None:
    """Sammelt alle Full-Res Bilder auf einer Thumbnail-Seite."""
    async with httpx.AsyncClient(timeout=30, verify=SSL) as client:
        try:
            r = await client.get(siteURL, headers=headers)
            r.raise_for_status()
        except Exception as e:
            print(f"Failed to fetch {siteURL}: {e}")
            return

        soup = BeautifulSoup(r.content, "html.parser")

        for td in soup.find_all("td", class_="thumbnails"):
            a_tag = td.find("a", href=True)
            if not a_tag:
                continue
            display_url = urljoin(siteURL, a_tag["href"])
            fullres_url = await extract_fullres_url(display_url, client)
            if fullres_url and fullres_url not in filelist:
                filelist.append(fullres_url)


async def extract_fullres_url(image_page_url: str, client: httpx.AsyncClient) -> str | None:
    """Ruft displayimage.php und die fullsize-Seite auf und findet das Fullsize-Bild."""

    # 1) displayimage.php laden
    try:
        r = await client.get(image_page_url, headers=headers)
        r.raise_for_status()
    except:
        print(f"Error loading image page {image_page_url}")
        return None

    soup = BeautifulSoup(r.content, "html.parser")

    # 2) Fullsize-Link finden
    # meistens: displayimage.php?...&fullsize=1
    fullsize_link = None

    # a) suche direkten Link
    for a in soup.find_all("a", href=True):
        if "fullsize=1" in a["href"]:
            fullsize_link = urljoin(image_page_url, a["href"])
            break

    # b) fallback: Coppermine JS Opening
    if not fullsize_link:
        # suche JS-Command: window.open('displayimage.php?...&fullsize=1');
        match = re.search(r"window\.open\('([^']+fullsize=1[^']*)'", r.text)
        if match:
            fullsize_link = urljoin(image_page_url, match.group(1))

    if not fullsize_link:
        print(f"No fullsize link on {image_page_url}")
        return None

    # 3) Fullsize-Seite laden
    try:
        r2 = await client.get(fullsize_link, headers=headers)
        r2.raise_for_status()
    except:
        print(f"Error loading fullsize page {fullsize_link}")
        return None

    soup2 = BeautifulSoup(r2.content, "html.parser")

    # 4) Jetzt das echte Full-HD Bild finden
    img = soup2.find("img", id="fullsize_image")
    if not img:
        print(f"No fullsize image tag on {fullsize_link}")
        return None

    final_url = urljoin(fullsize_link, img["src"])
    return final_url

    
    
def is_valid_int(s):
    try:
        int(s)
        return True
    except ValueError:
        return False

def countnumberofsites(siteURL):
    website = requests.get(siteURL, headers=headers, verify=SSL)

    if website.status_code != 200:
        print(f"ERROR: bad answer from Website: {website.status_code}")
        return 0

    soup = BeautifulSoup(website.content, 'html.parser')

    # Alle Links mit ?page=Zahl suchen
    pages = []
    for a in soup.find_all("a", href=True):
        match = re.search(r"page=(\d+)", a["href"])
        if match:
            pages.append(int(match.group(1)))
    if pages:
        print(f"{max(pages)} pages found")
        return max(pages)
    else:
        print("Only 1 page found")
        return 1


# Step 1: Count number of sites
sites = countnumberofsites(URL)

# Step 2: Gather image URLs asynchronously for each site
async def gather_image_urls():
    tasks = []
    if sites:
        for i in range(1, sites + 1):
            tasks.append(getImageUrlfromSite(URL + "&page=" + str(i)))
    else:
        tasks.append(getImageUrlfromSite(URL + "&page=1"))
    await asyncio.gather(*tasks)

# Step 3: Create destination directory if it doesn't exist
createdirectory(dest)

# Step 4: Download images asynchronously with limited parallelism
async def safe_download(file, dest, picprefix, sem):
    async with sem:  # semaphore limits num of simultaneous downloads
        return await download(file, dest, picprefix)

async def main():
    await gather_image_urls()
    print(f"{len(filelist)} files found")
    sem = asyncio.Semaphore(nbrOfParallelDL)
    tasks = [asyncio.ensure_future(safe_download(file, dest, picprefix, sem)) for file in filelist]
    await asyncio.gather(*tasks, return_exceptions=True)  # await moment all downloads done

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Download interrupted by user.")



