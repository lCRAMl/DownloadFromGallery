# -*- coding: utf-8 -*-
"""
Created on Sun Jul 10 20:36:13 2022

https://www.zenrows.com/blog/stealth-web-scraping-in-python-avoid-blocking-like-a-ninja#user-agent-header
https://curlconverter.com/python/
"""


import os
import random
import requests

start = 1
end = 28
howmanydidgets = 3
destprefix = ''
dlprefix = ''

#url = 'https://natalieportmanbr.sosugary.com/albums/Appearances and Events/2022/07 05 Thor Love And Thunder UK Gala Screening/Inside/'
dirtyurl = 'https://natalieportmanbr.sosugary.com/albums/Appearances%20and%20Events/2022/09%2027%20Dior%20SS23%20RTW/'
strtoremove = '%20'
if dirtyurl.find(strtoremove) != -1:
    url = dirtyurl.replace(strtoremove, " ")
else:
    url = dirtyurl   
#print (url)

dest = 'C:\\Users\\silence\\Desktop\\test\\'


isExist = os.path.exists(dest)
if not isExist:
  
  # Create a new directory because it does not exist 
  os.makedirs(dest)
  print(dest + "   <-- created!")

for i in range(end+1 - start):
    imagenumber = str(start+i).zfill(howmanydidgets) + ".jpg"
    completeURL = url + dlprefix + imagenumber
    user_agents = [ 
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:101.0) Gecko/20100101 Firefox/101.0'
	'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36', 
	'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/92.0.4515.107 Safari/537.36', 
	'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.212 Safari/537.36', 
	'Mozilla/5.0 (iPhone; CPU iPhone OS 12_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148', 
	'Mozilla/5.0 (Linux; Android 11; SM-G960U) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/89.0.4389.72 Mobile Safari/537.36' 
    ] 
    user_agent = random.choice(user_agents) 
    
    cookies = {
        'cpg143_data': 'YTo1OntzOjI6IklEIjtzOjMyOiI5ZjFmMDY5ZDcyZDE5OGU1ZDdkNzMyZjYzZGI1NzkwNiI7czoyOiJhbSI7aToxO3M6NDoibGFuZyI7czo2OiJnZXJtYW4iO3M6MzoibGl2IjthOjU6e2k6MDtzOjY6IjIzODUwOSI7aToxO3M6NjoiMjM4NTEwIjtpOjI7czo2OiIyMzczMjEiO2k6MztzOjY6IjIzNzI0MSI7aTo0O3M6NjoiMjM3MjQyIjt9czo1OiJsaXZfYSI7YTo1OntpOjA7aTozODQ7aToxO2k6MjYxMjtpOjI7aToyNzMzO2k6MztpOjI3MzA7aTo0O2k6MjcyNTt9fQ%3D%3D',
        'b865327a7e5d565bb86a7931e457f7d7': 'a2b37949e1a1d44e0abf2e382611ba9d',
    }

    headers = {
        'User-Agent': user_agent,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'de,en-US;q=0.7,en;q=0.3',
        # 'Accept-Encoding': 'gzip, deflate, br',
        'Referer': 'https://www.reese-witherspoon.org/gallery/displayimage.php?pid=237242&fullsize=1',
        'Connection': 'keep-alive',
        # Requests sorts cookies= alphabetically
        # 'Cookie': 'cpg143_data=YTo1OntzOjI6IklEIjtzOjMyOiI5ZjFmMDY5ZDcyZDE5OGU1ZDdkNzMyZjYzZGI1NzkwNiI7czoyOiJhbSI7aToxO3M6NDoibGFuZyI7czo2OiJnZXJtYW4iO3M6MzoibGl2IjthOjU6e2k6MDtzOjY6IjIzODUwOSI7aToxO3M6NjoiMjM4NTEwIjtpOjI7czo2OiIyMzczMjEiO2k6MztzOjY6IjIzNzI0MSI7aTo0O3M6NjoiMjM3MjQyIjt9czo1OiJsaXZfYSI7YTo1OntpOjA7aTozODQ7aToxO2k6MjYxMjtpOjI7aToyNzMzO2k6MztpOjI3MzA7aTo0O2k6MjcyNTt9fQ%3D%3D; b865327a7e5d565bb86a7931e457f7d7=a2b37949e1a1d44e0abf2e382611ba9d',
        'Upgrade-Insecure-Requests': '1',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'same-origin',
        'If-Modified-Since': 'Sun, 18 Sep 2022 21:14:12 GMT',
        # Requests doesn't support trailers
        # 'TE': 'trailers',
    }

    try:
        print("Downloading: " + completeURL)
        response = requests.get(completeURL, cookies=cookies, headers=headers)
        if response.status_code == 200:
            with open(dest + destprefix + imagenumber, 'wb') as f:
                f.write(response.content)
        print(response)
            
    except Exception as e: 
        print(e)
        continue