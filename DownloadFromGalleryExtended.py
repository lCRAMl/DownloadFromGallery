# -*- coding: utf-8 -*-
"""
Created on Fri Nov 11 17:37:50 2022

@author: silence
"""

import requests
from bs4 import BeautifulSoup
import random
import os
from clint.textui import progress
from tqdm import trange

def dlimage(dirtyimagepath, destination, prefix):
    
    if not os.path.exists(destination):
       os.makedirs(destination)
    
    strtoremove = '%20'
    if dirtyimagepath.find(strtoremove) != -1:
        imagepath = dirtyimagepath.replace(strtoremove, " ")
    else:
       imagepath = dirtyimagepath  

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
    
    try:
        response = requests.get(imagepath, headers=headers)
        
        if response.status_code == 200:
            filename = imagepath.split('/')
            filename = filename[len(filename) - 1]
            #with open(destination + prefix + filename, 'wb') as f:
            #    f.write(response.content)
            
            with open(destination + prefix + filename, 'wb') as f:
                #print("\033[H\033[1J", end="")
                print("Downloading: " + imagepath)
                total_length = int(response.headers.get('content-length'))
                #for chunk in progress.bar(response.iter_content(chunk_size=1024), expected_size=(total_length/1024) + 1):
                for chunk in trange(1024, expected_size=(total_length/1024) + 1): 
                    if chunk:
                        f.write(chunk)
                        f.flush()
                print("\033[H\033[1J", end="")
        else:
            print(imagepath)
            print(response.status_code)
            
    except Exception as e: 
        print(e)




"""----------------------------------------------------------------------------
        |||||        |||||
        vvvvv CONFIG vvvvv
-------------------------------------------------------------------------------
"""
URL = 'https://www.portmanphotos.com/thumbnails.php?album=79&page=1'
picurl = 'https://www.portmanphotos.com/albums/Appearances%20and%20Events/2020/5%200209%20%2092nd%20Annual%20Academy%20Awards%20in%20Los%20Angeles/arrivals/'  
dest = 'C:\\Users\\silence\\Desktop\\test\\'
picprefix = 'arrival'


filelist = []


website = requests.get(URL, headers={"User-Agent": "XY"})
print(website)
results = BeautifulSoup(website.content, 'html.parser')
blogbeitraege = results.find_all('td', class_='thumbnails')

#print(blogbeitraege)

for blogbeitrag in blogbeitraege:
    try:
        picname = blogbeitrag.find('img')['alt']
        filelist.append(picurl + picname)        
    except:
        pass
print("%s files found" % len(filelist))


for file in filelist:
    dlimage(file, dest, picprefix)