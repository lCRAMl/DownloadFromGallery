# -*- coding: utf-8 -*-
"""
Created on Sun Jul 10 19:52:28 2022

@author: silence
"""

import wget
import os

numberoffiles = '015'
howmanydidgets = '3'

url = 'https://natalieportmanbr.sosugary.com/albums/Appearances and Events/2022/07 05 Thor Love And Thunder UK Gala Screening/Inside/'
dest = 'C:\\Users\\silence\\Desktop\\test\\'


isExist = os.path.exists(dest)
if not isExist:
  
  # Create a new directory because it does not exist 
  os.makedirs(dest)
  print(dest + "   <-- created!")

for i in range(int(numberoffiles)):
    imagenumber = str(i+1).zfill(int(howmanydidgets)) + ".jpg"
    
    response = wget.download(url + imagenumber, dest + imagenumber)
    print (response)