# -*- coding: utf-8 -*-
"""
Created on Sun Jul 10 20:36:13 2022

@author: silence
"""

import wget
import os
import time
import urllib.request
import requests

start = 1
end = 103
howmanydidgets = 3
prefix = 'hq'
dlprefix = 'hq'

#url = 'https://natalieportmanbr.sosugary.com/albums/Appearances and Events/2022/07 05 Thor Love And Thunder UK Gala Screening/Inside/'
dirtyurl = 'https://www.reese-witherspoon.org/gallery/albums/Events/2022/WhereTheCrawdadsSingNYCPremiere/'
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
    for attempt in range(10):
        try:
            print("Downloading: " + url + prefix + imagenumber)
            print ("Attempt Nr " + str(attempt))
            #response = wget.download(url + prefix + imagenumber, dest + dlprefix + imagenumber)
            #response = urllib.request.urlretrieve(url + prefix + imagenumber, dest + dlprefix + imagenumber)
            #print (response)
            with open(dest + dlprefix + imagenumber, 'wb') as handle:
                response = requests.get(url + prefix + imagenumber, stream=True)

                if not response.ok:
                    print(response)
        except:
            time.sleep(5)
            continue