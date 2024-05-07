# -*- coding: utf-8 -*-
"""
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
"""
"""----------------------------------------------------------------------------
        |||||        |||||
        vvvvv CONFIG vvvvv
-------------------------------------------------------------------------------
"""
URL = 'https://anadearmas.net/photos/thumbnails.php?album=1803'
#dest = 'C:\\Users\\silence\\Desktop\\ja\\2014\\Jessica Alba - Samsung Hope For Children Gala in NYC 2014-06-10\\'
dest = 'C:\\Users\\silence\\Desktop\\Ana de Armas\\2022\\Ana de Armas - The Gray Man Screening in London 2022-07-19\\'
picprefix = ''
nbrOfParallelDL = 5

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

user_agents = [ 
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:101.0) Gecko/20100101 Firefox/101.0'
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
    'Accept-Language': 'en-US;q=0.7,en;q=0.3',
    # 'Accept-Encoding': 'gzip, deflate, br',
    'Referer': 'https://www.google.com',
    'Connection': 'keep-alive',
    'Cache-Control': 'no-cache',
    # Requests sorts cookies= alphabetically
    # 'Cookie': 'cpg143_data=YTo1OntzOjI6IklEIjtzOjMyOiI5ZjFmMDY5ZDcyZDE5OGU1ZDdkNzMyZjYzZGI1NzkwNiI7czoyOiJhbSI7aToxO3M6NDoibGFuZyI7czo2OiJnZXJtYW4iO3M6MzoibGl2IjthOjU6e2k6MDtzOjY6IjIzODUwOSI7aToxO3M6NjoiMjM4NTEwIjtpOjI7czo2OiIyMzczMjEiO2k6MztzOjY6IjIzNzI0MSI7aTo0O3M6NjoiMjM3MjQyIjt9czo1OiJsaXZfYSI7YTo1OntpOjA7aTozODQ7aToxO2k6MjYxMjtpOjI7aToyNzMzO2k6MztpOjI3MzA7aTo0O2k6MjcyNTt9fQ%3D%3D; b865327a7e5d565bb86a7931e457f7d7=a2b37949e1a1d44e0abf2e382611ba9d',
    'Upgrade-Insecure-Requests': '1',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'same-origin',
    # Requests doesn't support trailers
    # 'TE': 'trailers',
}

def verify_image(img_file):
    try:
        io.imread(img_file)
        return True
    except Exception:
        return False

async def download(dirtyimagepath: str, destination: str, prefix: str):
    
    strtoremove = '%20'
    if dirtyimagepath.find(strtoremove) != -1:
        imagepath = dirtyimagepath.replace(strtoremove, " ")
    else:
       imagepath = dirtyimagepath

    filename = imagepath.split('/')
    filename = filename[len(filename) - 1]
    fulldestinationname = destination + prefix + filename
    
    # wenn das file nicht da oder kaputt ist wirds geladen
    if not os.path.exists(fulldestinationname) or (os.path.exists(fulldestinationname) and not verify_image(fulldestinationname)):
        #with requests.get(imagepath, stream=True, headers=headers) as r:
        async with httpx.AsyncClient() as client:
            async with client.stream('GET', imagepath) as r:
                if r.status_code == 200:
                    with open(fulldestinationname, 'wb') as f:
                        r.raise_for_status()
                        total = int(r.headers.get('content-length', 0))
            
                        # tqdm has many interesting parameters. Feel free to experiment!
                        tqdm_params = {
                            'desc': imagepath,
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
                    print(imagepath + ' Response:' + str(r.status_code))

def getImageUrlfromSite(siteURL: str):
    website = requests.get(siteURL, headers={"User-Agent": "XY"})
    results = BeautifulSoup(website.content, 'html.parser')
    images = results.find_all('td', class_='thumbnails')
    #urlbasepath = urlparse(siteURL).netloc
    urlbasepath = siteURL.rsplit('/', 1)[0]
    print("Searching Images on: " + siteURL + " " + str(len(images)) + " files found.")
    
    for image in images:
        try:
            picname = image.find('img')['alt']
            picurl = image.find('img')['src']
            picurl = picurl.rsplit('/', 1)
            picturepath = (urlbasepath + "/" + picurl[0] + "/" + picname)
            filelist.append(picturepath) if picturepath not in filelist else filelist     # nur hinzufügen, wenn noch nicht drin
        except:
            pass

async def main():

    tasks = [asyncio.ensure_future(safe_download(file, dest, picprefix)) for file in filelist]
    await asyncio.gather(*tasks, return_exceptions=True)  # await moment all downloads done


filelist = []

#website = requests.get(URL, headers={"User-Agent": "XY"})
website = requests.get(URL, headers)

if website.status_code == 200:
    soup = BeautifulSoup(website.content, 'html.parser')
    counttd = soup.find_all('td', class_='navmenu')
    data = []
    for sites in counttd:
        number = sites.find_all(['a'])
        number = [ele.text.strip() for ele in number]
        data.append([int(ele) for ele in number if ele])
    sites = max(data)[0]
    print(sites)
    if (sites):
        for i in range(1, sites+1):
            getImageUrlfromSite(URL + "&page=" + str(i))
    else:
        getImageUrlfromSite(URL + "&page=" + str(1))
    print("%s files found" % len(filelist))
    try:
        if not os.path.exists(dest):
            os.makedirs(dest)
    except:
        print("ERROR: Cannot create directory %s" % dest)

    sem = asyncio.Semaphore(nbrOfParallelDL)

    async def safe_download(file, dest, picprefix):
        async with sem:  # semaphore limits num of simultaneous downloads
            return await download(file, dest, picprefix)


    if __name__ ==  '__main__':
        loop = asyncio.get_event_loop()
        try:
            loop.run_until_complete(main())
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

else:
    print("ERROR: bad answer from Website: " + website.status_code)
